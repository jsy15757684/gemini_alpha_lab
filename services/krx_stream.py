"""나무증권 국내주식 실시간 체결 스트림 (웹소켓 · KRX 전용 채널 oc).

  wss://api.nhplug.com:7070/websocket
  구독   {"header": {"token", "tr_type": "1"}, "body": {"tr_cd": "oc", "tr_key": 종목코드}}
  받음   {"header": {"tr_cd", "tr_key"}, "body": {price, high, low, open, volume,
          avgprice(VWAP), offer, bid, time, kospigb(1 코스피 · 2 코스닥), ...}}

서버 한도 (nhplug-sdk docs/realtime_channels.md): 앱키당 동시 연결 2 · 연결당
구독 30 · 구독 전송 초당 10. 연결 하나로 30종목을 받는다. 나머지 연결 하나는
재접속이 겹칠 때를 위해 남겨 둔다.

통합 채널(mc)이 아니라 KRX 채널(oc)을 쓰는 이유: 통합은 NXT 프리마켓
(08:00~08:50) 체결까지 섞여 09:00 오프닝 레인지 고저와 09:05 누적 거래량(RVOL)이
틀어진다.

토큰은 REST 와 같은 것(NamuhAccount.get_token)을 쓴다. 새로 발급하지 않는다.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, Optional

from services import ws_lite

logger = logging.getLogger(__name__)

WS_HOST = (os.getenv("NAMUH_WS_HOST") or "api.nhplug.com").strip()
WS_PORT = int(os.getenv("NAMUH_WS_PORT") or "7070")
TR_CD = "oc"
MAX_KEYS = 30
SUB_GAP_SEC = 0.12          # 초당 10건 한도 아래로
BACKOFF = (2, 5, 10, 30, 60)


def _i(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_tick(body: Dict[str, Any]) -> Dict[str, Any]:
    """oc 체결 메시지 → krx.quote 와 같은 모양의 스냅샷."""
    return {
        "code": str(body.get("code") or "").strip(),
        "price": _i(body.get("price")),
        "high": _i(body.get("high")),
        "low": _i(body.get("low")),
        "open": _i(body.get("open")),
        "volume": _i(body.get("volume")),
        "vwap": _f(body.get("avgprice")),
        "ask": _i(body.get("offer")),
        "bid": _i(body.get("bid")),
        "hogaTime": str(body.get("time") or "").strip(),
        "changePct": _f(body.get("chrate")),
        "market": {"1": "kospi", "2": "kosdaq"}.get(str(body.get("kospigb") or ""), ""),
        "receivedAt": time.time(),
        "source": "stream",
    }


class KrxStream:
    def __init__(self):
        self._lock = threading.Lock()
        self._codes: set = set()
        self._subscribed: set = set()
        self._snap: Dict[str, Dict[str, Any]] = {}
        self._acc = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.connected = False
        self.error = ""
        self.last_msg_at = 0.0
        self.reconnects = 0
        self.rejected: Dict[str, str] = {}

    # ── 바깥에서 부르는 것 ──
    def ensure(self, acc, codes: Iterable[str]) -> None:
        """이 종목들을 받도록 한다. 스레드가 없으면 띄운다. 구독은 스레드가 맞춘다."""
        codes = {str(c).strip() for c in codes if str(c).strip()}
        if len(codes) > MAX_KEYS:
            raise ValueError(f"실시간 구독은 연결당 {MAX_KEYS}종목까지입니다 ({len(codes)}종목 요청)")
        with self._lock:
            self._acc = acc
            self._codes = codes
        if not (self._thread and self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="krx-stream", daemon=True)
            self._thread.start()

    def release(self) -> None:
        """연결을 닫는다 (장 밖 · 감시할 봇이 없을 때 — 앱키당 연결 한도를 비운다)."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        self.connected = False

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            s = self._snap.get(code)
            return dict(s) if s else None

    def age(self, code: str) -> Optional[float]:
        s = self.get(code)
        return time.time() - s["receivedAt"] if s else None

    def status(self) -> Dict[str, Any]:
        return {"connected": self.connected, "codes": sorted(self._codes),
                "subscribed": len(self._subscribed), "error": self.error,
                "lastMsgAgoSec": round(time.time() - self.last_msg_at, 1) if self.last_msg_at else None,
                "reconnects": self.reconnects, "rejected": dict(self.rejected)}

    def kick(self, why: str) -> None:
        """조용해진 연결을 끊고 다시 붙게 한다 (시세가 멈췄을 때)."""
        logger.warning(f"실시간 시세 재접속 — {why}")
        c = getattr(self, "_client", None)
        if c:
            c.close()

    # ── 스레드 ──
    def _send(self, client, tr_type: str, code: str, token: str) -> None:
        client.send_text(json.dumps({"header": {"token": token, "tr_type": tr_type},
                                     "body": {"tr_cd": TR_CD, "tr_key": code}}))
        time.sleep(SUB_GAP_SEC)

    def _sync(self, client, token: str) -> None:
        with self._lock:
            want = set(self._codes)
        for code in sorted(self._subscribed - want):
            self._send(client, "2", code, token)
            self._subscribed.discard(code)
        for code in sorted(want - self._subscribed):
            self._send(client, "1", code, token)
            self._subscribed.add(code)

    def _run(self) -> None:
        fails = 0
        while not self._stop.is_set():
            client = ws_lite.WsClient(WS_HOST, WS_PORT)
            self._client = client
            self._subscribed = set()
            try:
                token = self._acc.get_token()
                client.connect()
                self.connected, self.error = True, ""
                logger.info(f"실시간 시세 연결 ({WS_HOST}:{WS_PORT} · {TR_CD})")
                self._sync(client, token)
                fails = 0
                while not self._stop.is_set():
                    with self._lock:
                        changed = self._codes != self._subscribed
                    if changed:
                        self._sync(client, token)
                    text = client.recv(timeout=1.0)
                    if text is None:
                        continue
                    self._on_message(text)
            except Exception as e:
                self.error = str(e)
                if not self._stop.is_set():
                    logger.warning(f"실시간 시세 끊김: {e}")
            finally:
                self.connected = False
                client.close()
            if self._stop.is_set():
                break
            wait = BACKOFF[min(fails, len(BACKOFF) - 1)]
            fails += 1
            self.reconnects += 1
            self._stop.wait(wait)

    def _on_message(self, text: str) -> None:
        try:
            m = json.loads(text)
        except ValueError:
            return
        h, b = m.get("header") or {}, m.get("body") or {}
        if "rsp_cd" in h:                       # 구독 응답
            if str(h.get("rsp_cd")) != "00000":
                keys = b.get("tr_key") or []
                msg = f"{h.get('rsp_cd')} {h.get('rsp_msg')}"
                for k in (keys if isinstance(keys, list) else [keys]):
                    self.rejected[str(k)] = msg
                logger.error(f"실시간 구독 거부: {msg} · {keys}")
            return
        if h.get("tr_cd") != TR_CD:
            return
        tick = parse_tick(b)
        if not tick["code"] or tick["price"] <= 0:
            return
        self.last_msg_at = tick["receivedAt"]
        with self._lock:
            self._snap[tick["code"]] = tick


stream = KrxStream()
