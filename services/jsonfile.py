"""상태 JSON 파일을 안전하게 읽고 쓴다.

봇 상태(bots.json)·체결 일지(trades.json)·시뮬레이터 상태(arb_bots.json)가
모두 같은 규칙을 따라야 해서 한 곳에 모았다.

핵심 규칙은 하나다. **'파일이 없다' 와 '읽지 못했다' 를 구분한다.**

전에는 권한 오류나 파손을 빈 목록으로 바꿔 돌려줬다. 그러면 프로그램은
기록이 하나도 없다고 믿고 진행하고, 바로 다음 저장이 원자적 교체로 과거
기록을 지운다. 2026-09-18 에 실제로 그렇게 됐다 — data/ 안의 파일이 잠깐
root 소유가 되는 바람에 체결 일지 126건이 6건이 되고 시뮬레이터 상태가
빈 배열이 됐다. 같은 경로가 bots.json 에도 있었다. 거기서 터졌다면
빗썸에 실전 포지션이 남은 채로 봇이 장부에서 사라진다 — 익절·손절·
거래소 대조를 아무도 하지 않는 상태가 된다.

그래서 읽기 실패는 예외로 올리고, **한 번 읽기에 실패한 파일에는 다시
쓰지 않는다**(`Guard`). 호출부가 예외를 삼켜도 파일은 살아남는다.
복구 경로는 사람이 원인을 고치고 재시작하는 것 하나뿐이다.
"""

import os
import json
import logging
import tempfile
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StoreReadError(RuntimeError):
    """파일이 있는데 읽지 못했다. '기록이 없다' 와 같게 처리하면 안 된다."""


def read_records(path: str, key: str, label: str) -> Optional[List[Dict[str, Any]]]:
    """레코드 목록을 읽는다.

    - 파일이 없으면 None (첫 실행이다. 저장해도 잃을 것이 없다)
    - 읽기 실패·형식 이상이면 StoreReadError. 절대 빈 목록으로 바꾸지 않는다
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise StoreReadError(f"{label} 파일을 읽지 못했습니다: {e}") from e

    recs = data.get(key) if isinstance(data, dict) else None
    if not isinstance(recs, list):
        raise StoreReadError(
            f"{label} 파일 형식이 예상과 다릅니다 ('{key}' 목록이 없습니다). "
            "덮어쓰지 않고 그대로 두었습니다.")
    return recs


def write_records(path: str, key: str, records: List[Dict[str, Any]]) -> None:
    """임시파일 + rename 으로 원자적으로 쓴다. 쓰는 도중 죽어도 안 깨진다."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, key: records}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Guard:
    """읽기에 실패한 파일을 덮어쓰지 못하게 막는 빗장.

    한 번 걸리면 재시작 전까지 풀리지 않는다. 자동 복구를 넣지 않은 것은
    의도다 — 원인(권한·디스크·파손)을 사람이 확인하지 않은 채 다시 쓰기
    시작하면, 지우려던 그 사고가 그대로 일어난다.
    """

    def __init__(self, label: str):
        self.label = label
        self._lock = threading.Lock()
        self._reason: Optional[str] = None

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def blocked(self) -> bool:
        return self._reason is not None

    def block(self, reason: str) -> None:
        with self._lock:
            if self._reason:
                return
            self._reason = reason
            logger.error(f"{self.label}: {reason} — 이 파일에는 더 이상 쓰지 않습니다. "
                         "원인을 고친 뒤 서비스를 재시작하세요.")

    def reset(self) -> None:
        """시험용. 운영 경로에서는 부르지 않는다."""
        with self._lock:
            self._reason = None

    def refuse_write(self) -> bool:
        """쓰기를 거부해야 하면 True (거부 사유를 남긴다)."""
        if not self._reason:
            return False
        logger.error(f"{self.label} 저장을 거부했습니다 — {self._reason}. "
                     "기존 파일을 덮어쓰지 않습니다.")
        return True
