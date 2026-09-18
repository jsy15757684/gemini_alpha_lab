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


def save(records: List[Dict[str, Any]]) -> None:
    """봇 상태 전체를 원자적으로 저장한다."""
    with _lock:
        if guard.refuse_write():
            return
        try:
            write_records(STORE_FILE, "bots", records)
        except Exception as e:
            logger.error(f"봇 상태 저장 실패: {e}")


def load() -> List[Dict[str, Any]]:
    """저장된 봇 상태를 읽는다.

    파일이 없으면 빈 목록(첫 실행). **읽기에 실패하면 StoreReadError 를
    올린다** — 호출부는 이것을 '봇이 0개' 로 해석하면 안 된다.
    """
    with _lock:
        try:
            bots = read_records(STORE_FILE, "bots", "봇 상태")
        except StoreReadError as e:
            guard.block(str(e))
            raise
    return [] if bots is None else bots


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
