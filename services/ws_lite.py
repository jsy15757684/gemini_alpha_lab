"""표준 라이브러리만으로 만든 최소 웹소켓 클라이언트 (RFC 6455, 클라이언트 쪽).

나무증권 실시간 시세 하나만 받으면 된다. 운영 서버에 패키지를 새로 깔지 않으려고
직접 둔다 — 텍스트 프레임 송수신 · 조각(continuation) 합치기 · ping 에 pong ·
close 까지만 한다. 압축(permessage-deflate)은 요청하지 않는다.
"""

import base64
import os
import socket
import ssl
import struct
from typing import Optional, Tuple

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsClosed(Exception):
    """연결이 닫혔다 (서버 close · EOF · 오류)."""


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: Optional[bytes] = None) -> bytes:
    """클라이언트 프레임. 클라이언트는 반드시 마스킹한다."""
    mask = mask or os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class FrameReader:
    """바이트를 받아 (opcode, payload) 프레임으로 끊는다. 서버 프레임은 마스킹이 없다."""

    def __init__(self):
        self.buf = b""
        self._frag_op: Optional[int] = None
        self._frag = b""

    def feed(self, data: bytes) -> None:
        self.buf += data

    def next(self) -> Optional[Tuple[int, bytes]]:
        """완성된 메시지 하나. 모자라면 None."""
        while True:
            b = self.buf
            if len(b) < 2:
                return None
            fin, op, masked, n = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x80, b[1] & 0x7F
            off = 2
            if n == 126:
                if len(b) < 4:
                    return None
                n, off = struct.unpack(">H", b[2:4])[0], 4
            elif n == 127:
                if len(b) < 10:
                    return None
                n, off = struct.unpack(">Q", b[2:10])[0], 10
            mkey = b""
            if masked:
                if len(b) < off + 4:
                    return None
                mkey, off = b[off:off + 4], off + 4
            if len(b) < off + n:
                return None
            payload = b[off:off + n]
            if masked:
                payload = bytes(x ^ mkey[i % 4] for i, x in enumerate(payload))
            self.buf = b[off + n:]
            if op >= 0x8:                       # 제어 프레임은 조각나지 않는다
                return op, payload
            if op != OP_CONT:
                self._frag_op, self._frag = op, payload
            else:
                self._frag += payload
            if fin:
                out = (self._frag_op or OP_TEXT, self._frag)
                self._frag_op, self._frag = None, b""
                return out


class WsClient:
    def __init__(self, host: str, port: int, path: str = "/websocket", timeout: float = 10.0):
        self.host, self.port, self.path, self.timeout = host, port, path, timeout
        self.sock: Optional[ssl.SSLSocket] = None
        self.reader = FrameReader()

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise WsClosed("핸드셰이크 중 연결이 닫혔습니다")
            resp += chunk
            if len(resp) > 65536:
                raise WsClosed("핸드셰이크 응답이 너무 깁니다")
        head, rest = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            raise WsClosed(f"웹소켓 전환 거부: {status}")
        self.sock = s
        self.reader = FrameReader()
        self.reader.feed(rest)

    def send_text(self, text: str) -> None:
        self._send(encode_frame(text.encode("utf-8"), OP_TEXT))

    def _send(self, data: bytes) -> None:
        if not self.sock:
            raise WsClosed("연결되어 있지 않습니다")
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise WsClosed(f"보내기 실패: {e}")

    def recv(self, timeout: float = 1.0) -> Optional[str]:
        """텍스트 메시지 하나. timeout 동안 없으면 None. ping 은 알아서 pong 한다."""
        if not self.sock:
            raise WsClosed("연결되어 있지 않습니다")
        self.sock.settimeout(timeout)
        while True:
            msg = self.reader.next()
            if msg:
                op, payload = msg
                if op == OP_PING:
                    self._send(encode_frame(payload, OP_PONG))
                    continue
                if op == OP_PONG:
                    continue
                if op == OP_CLOSE:
                    code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                    raise WsClosed(f"서버가 연결을 닫았습니다 (code {code}: {payload[2:].decode('utf-8', 'replace')})")
                return payload.decode("utf-8", "replace")
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as e:
                raise WsClosed(f"받기 실패: {e}")
            if not chunk:
                raise WsClosed("연결이 끊겼습니다 (EOF)")
            self.reader.feed(chunk)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.sendall(encode_frame(struct.pack(">H", 1000), OP_CLOSE))
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
