"""봇 상태 영속화.

봇 상태가 메모리에만 있으면, 재시작·배포·크래시 때 봇은 사라지는데
빗썸의 실제 포지션은 남는다. 손절을 감시하던 주체가 없어진 채 포지션이
방치되는 것이 이 프로그램에서 가장 위험한 상황이다.

그래서 상태가 바뀔 때마다 디스크에 쓰고, 시작할 때 복원한다.

저장 위치는 data/bots.json (gitignore 대상). 읽고 쓰는 규칙은
services/jsonfile.py 에 모아 두었다 — 특히 '읽지 못했다' 를 '봇이 없다'
로 바꾸지 않는 규칙이 여기서 제일 중요하다. 그렇게 바꾸면 다음 저장이
실전 포지션을 들고 있는 봇의 기록을 지운다.

주의: 컨테이너 디스크가 휘발성인 PaaS 에서는 재배포 시 이 파일도 사라진다.
24시간 실전 운용은 디스크가 유지되는 곳(VPS)에서 해야 한다.
"""

import os
import logging
import threading
from typing import Any, Dict, List

from services import roles
from services.jsonfile import Guard, StoreReadError, read_records, write_records

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

STORE_FILE = os.path.join(_DATA_DIR, "bots.json")

_lock = threading.Lock()

# 봇 상태 파일 전용 빗장. 읽기에 실패하면 이후 저장을 전부 거부한다.
guard = Guard("봇 상태")


class JsonStore:
    """레코드 목록을 원자적으로 읽고 쓰는 작은 저장소.

    자동매매 봇과 차익거래 시뮬레이터가 같은 방식을 쓰되 파일은 분리한다.
    실계좌 포지션을 담은 파일에 가상 체결을 섞지 않기 위해서다.
    """

    def __init__(self, filename: str, label: str):
        self.path = os.path.join(_DATA_DIR, filename)
        self.label = label
        self._lock = threading.Lock()
        self.guard = Guard(label)

    def save(self, records: List[Dict[str, Any]]) -> None:
        with self._lock:
            if self.guard.refuse_write():
                return
            try:
                write_records(self.path, "records", records)
            except Exception as e:
                logger.error(f"{self.label} 저장 실패: {e}")

    def load(self) -> List[Dict[str, Any]]:
        """저장된 레코드를 읽는다.

        파일이 없으면 빈 목록. **읽기에 실패하면 StoreReadError 를 올린다.**
        """
        with self._lock:
            try:
                recs = read_records(self.path, "records", self.label)
            except StoreReadError as e:
                self.guard.block(str(e))
                raise
        return [] if recs is None else recs


# 차익거래 시뮬레이터 상태 (가상 체결 — 실계좌 봇과 파일을 분리한다)
arb_store = JsonStore("arb_bots.json", "차익거래 시뮬레이터 상태")


# ── 크립토 / 나무증권 파일 분리 ──
#
# 프로세스를 나누면(roles.split()) 두 프로세스가 한 파일을 같이 쓸 수 없다.
# 각자 자기 봇만 들고 있으므로, 한쪽이 저장하는 순간 다른 쪽 봇이 파일에서
# 지워진다. 그래서 나무증권 봇은 bots_namuh.json 으로 옮긴다.
#
# 옮기는 것은 처음 한 번뿐이고(bots_namuh.json 이 없을 때), 먼저 뜬 쪽이
# 파일 잠금 아래에서 한다. 나무증권 파일을 먼저 쓰고 나서 원래 파일을
# 줄이므로, 중간에 꺼져도 봇이 사라지지 않는다(양쪽에 남을 뿐이고, 각
# 프로세스는 자기 브로커 것만 읽는다).

def namuh_store_file() -> str:
    # 테스트는 STORE_FILE 을 임시 경로로 바꾼다. 나무증권 파일도 따라가야
    # 테스트가 운영 파일을 읽지 않는다.
    return os.path.join(os.path.dirname(STORE_FILE), "bots_namuh.json")


def broker_of(rec: Dict[str, Any]) -> str:
    b = rec.get("broker")
    if b:
        return b
    from services.namuh import NAMUH_STOCKS      # 옛 기록에는 broker 가 없다
    return "namuh" if rec.get("coin") in NAMUH_STOCKS else "bithumb"


def _migrate_locked() -> None:
    nfile = namuh_store_file()
    if os.path.exists(nfile):
        return
    import fcntl
    lock_path = os.path.join(os.path.dirname(STORE_FILE), ".bots.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        if os.path.exists(nfile):               # 기다리는 동안 다른 쪽이 옮겼다
            return
        recs = read_records(STORE_FILE, "bots", "봇 상태") or []
        mine = [r for r in recs if broker_of(r) == "namuh"]
        rest = [r for r in recs if broker_of(r) != "namuh"]
        write_records(nfile, "bots", mine)
        if mine:
            write_records(STORE_FILE, "bots", rest)
        logger.info(f"봇 상태를 나눴습니다 — 나무증권 {len(mine)}개 → {os.path.basename(nfile)}, "
                    f"빗썸 {len(rest)}개는 {os.path.basename(STORE_FILE)} 에 남김")


def _files() -> Dict[str, str]:
    """이 프로세스가 읽고 쓰는 파일 {브로커 묶음: 경로}."""
    if roles.ROLE == "crypto":
        return {"bithumb": STORE_FILE}
    if roles.ROLE == "namuh":
        return {"namuh": namuh_store_file()}
    # all: 나눈 적이 있으면 두 파일을 다 쓴다. 없으면 예전처럼 한 파일.
    if os.path.exists(namuh_store_file()):
        return {"bithumb": STORE_FILE, "namuh": namuh_store_file()}
    return {"*": STORE_FILE}


def _pick(kind: str, recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if kind == "*":
        return list(recs)
    return [r for r in recs if (broker_of(r) == "namuh") == (kind == "namuh")]


def save(records: List[Dict[str, Any]]) -> None:
    """봇 상태 전체를 원자적으로 저장한다 (이 프로세스가 맡은 파일만)."""
    with _lock:
        if guard.refuse_write():
            return
        try:
            for kind, path in _files().items():
                write_records(path, "bots", _pick(kind, records))
        except Exception as e:
            logger.error(f"봇 상태 저장 실패: {e}")


def load() -> List[Dict[str, Any]]:
    """저장된 봇 상태를 읽는다 (이 프로세스가 맡은 브로커 것만).

    파일이 없으면 빈 목록(첫 실행). **읽기에 실패하면 StoreReadError 를
    올린다** — 호출부는 이것을 '봇이 0개' 로 해석하면 안 된다.
    """
    with _lock:
        try:
            if roles.split():
                try:
                    _migrate_locked()
                except StoreReadError:
                    raise
                except Exception as e:
                    # 옮기다 실패하면(주로 권한) 복원을 멈춘다. 반쯤 옮긴 채
                    # 돌면 어느 쪽 파일이 진짜인지 알 수 없다.
                    raise StoreReadError(f"봇 상태를 나누지 못했습니다: {e}")
            out: List[Dict[str, Any]] = []
            for kind, path in _files().items():
                out.extend(_pick(kind, read_records(path, "bots", "봇 상태") or []))
        except StoreReadError as e:
            guard.block(str(e))
            raise
    return out


def clear() -> None:
    with _lock:
        if guard.blocked:
            logger.error("봇 상태 파일을 읽지 못한 상태라 삭제도 거부합니다.")
            return
        try:
            if os.path.exists(STORE_FILE):
                os.remove(STORE_FILE)
        except Exception as e:
            logger.warning(f"봇 상태 파일 삭제 실패: {e}")
