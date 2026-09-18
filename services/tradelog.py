"""체결 일지 — 봇과 분리된 영속 장부.

봇 상태(data/bots.json)는 '지금 무엇을 들고 있는가' 를 담는다. 봇을 지우면
그 기록도 함께 사라진다. 그건 포지션 관리 관점에서는 맞지만, 매매 일지와
누적 실현 손익에는 치명적이다 — 끝난 봇을 정리했다는 이유로 과거 체결과
손익이 장부에서 없어지면 그 화면은 '누적' 이라고 부를 수 없다.

그래서 체결은 여기에 따로 쌓는다. 추가만 하고 봇 삭제에 영향받지 않는다.
data/trades.json 에 원자적으로 쓴다(임시파일 + rename).
"""

import os
import logging
import threading
from typing import Any, Dict, List, Optional

from services.jsonfile import Guard, StoreReadError, read_records, write_records

logger = logging.getLogger(__name__)

LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trades.json")

# 무한히 쌓이지 않게 상한을 둔다. 최신부터 유지한다.
MAX_ROWS = 5000

_lock = threading.RLock()
_rows: List[Dict[str, Any]] = []
_loaded = False

# 일지 파일 전용 빗장. 읽기에 실패하면 이후 저장을 전부 거부한다.
# 그러지 않으면 '읽은 것이 없다 = 빈 장부' 로 믿고 과거 체결을 덮어쓴다.
guard = Guard("체결 일지")


def _save_locked() -> None:
    if guard.refuse_write():
        return
    try:
        write_records(LOG_FILE, "trades", _rows)
    except Exception as e:
        logger.error(f"체결 일지 저장 실패: {e}")


def load() -> None:
    """디스크에서 한 번만 읽어 메모리에 올린다.

    읽기에 실패하면 StoreReadError 를 올리고, 그 뒤로는 이 파일에 쓰지
    않는다. 빈 장부로 진행하면 다음 체결 1건이 과거 전체를 덮어쓴다.
    """
    global _loaded
    with _lock:
        if _loaded:
            return
        try:
            rows = read_records(LOG_FILE, "trades", "체결 일지")
        except StoreReadError as e:
            _loaded = True          # 반복해서 실패 로그를 쏟지 않는다
            guard.block(str(e))
            raise
        _loaded = True
        if rows:
            _rows.extend(rows)
        logger.info(f"체결 일지 {len(_rows)}건을 읽었습니다.")


def _load_quietly() -> None:
    """읽기 실패를 삼키고 진행한다 (빗장이 파일을 지켜 준다).

    체결 직후처럼 '여기서 예외를 올리면 매매 흐름이 끊기는' 자리에서 쓴다.
    파일은 빗장 덕에 안전하고, 화면에는 warning() 으로 사실을 알린다.
    """
    try:
        load()
    except StoreReadError:
        pass


def warning() -> Optional[str]:
    """장부를 신뢰할 수 없으면 그 사유. 정상이면 None."""
    return guard.reason


def append(trade: Dict[str, Any]) -> None:
    """체결 1건을 장부에 추가한다. 최신이 앞에 온다."""
    with _lock:
        _load_quietly()
        _rows.insert(0, dict(trade))
        del _rows[MAX_ROWS:]
        _save_locked()


def seed(trades: List[Dict[str, Any]]) -> int:
    """이미 있는 기록을 장부에 합친다 (id 기준 중복 제외).

    이 기능이 없던 시절 봇 스냅샷에만 남아 있던 체결을 한 번 끌어오기 위한
    것이다. 복원 때 호출한다.
    """
    with _lock:
        _load_quietly()
        if guard.blocked:
            logger.error("체결 일지를 읽지 못한 상태라 기존 기록 합치기를 건너뜁니다.")
            return 0
        known = {r.get("id") for r in _rows if r.get("id")}
        added = 0
        for t in trades:
            tid = t.get("id")
            if tid and tid in known:
                continue
            _rows.append(dict(t))
            if tid:
                known.add(tid)
            added += 1
        if added:
            _rows.sort(key=lambda x: x.get("time", ""), reverse=True)
            del _rows[MAX_ROWS:]
            _save_locked()
            logger.info(f"기존 봇 기록 {added}건을 체결 일지에 합쳤습니다.")
        return added


def all_rows() -> List[Dict[str, Any]]:
    with _lock:
        _load_quietly()
        return list(_rows)
