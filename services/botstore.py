"""봇 상태 영속화.

봇 상태가 메모리에만 있으면, 재시작·배포·크래시 때 봇은 사라지는데
빗썸의 실제 포지션은 남는다. 손절을 감시하던 주체가 없어진 채 포지션이
방치되는 것이 이 프로그램에서 가장 위험한 상황이다.

그래서 상태가 바뀔 때마다 디스크에 쓰고, 시작할 때 복원한다.

저장 위치는 data/bots.json (gitignore 대상). 쓰기는 임시파일 + rename 으로
원자적으로 처리해, 쓰는 도중 죽어도 파일이 깨지지 않게 한다.

주의: 컨테이너 디스크가 휘발성인 환경(Render 등)에서는 재배포 시 이 파일도
사라진다. 24시간 실전 운용은 디스크가 유지되는 곳에서 해야 한다.
"""

import os
import json
import logging
import tempfile
import threading
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

STORE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bots.json")

_lock = threading.Lock()


def save(records: List[Dict[str, Any]]) -> None:
    """봇 상태 전체를 원자적으로 저장한다."""
    with _lock:
        try:
            os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STORE_FILE), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "bots": records}, f, ensure_ascii=False)
                os.replace(tmp, STORE_FILE)   # 원자적 교체
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error(f"봇 상태 저장 실패: {e}")


def load() -> List[Dict[str, Any]]:
    """저장된 봇 상태를 읽는다. 파일이 없거나 깨졌으면 빈 목록."""
    with _lock:
        if not os.path.exists(STORE_FILE):
            return []
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"봇 상태 파일을 읽지 못했습니다 (무시하고 진행): {e}")
            return []
    bots = data.get("bots") if isinstance(data, dict) else None
    return bots if isinstance(bots, list) else []


def clear() -> None:
    with _lock:
        try:
            if os.path.exists(STORE_FILE):
                os.remove(STORE_FILE)
        except Exception as e:
            logger.warning(f"봇 상태 파일 삭제 실패: {e}")
