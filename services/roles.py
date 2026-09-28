"""프로세스 역할 — 크립토와 나무증권을 따로 돌리기 위한 스위치.

  APP_ROLE=all     (기본) 한 프로세스가 전부 한다. 로컬 개발 · 테스트 · 미리보기.
  APP_ROLE=crypto  화면 + 로그인 + 빗썸 봇. 나무증권 요청은 워커로 넘긴다.
  APP_ROLE=namuh   나무증권 봇만 돌리는 워커. 127.0.0.1 에서만 받고,
                   crypto 프로세스가 붙인 내부 토큰이 없는 요청은 거절한다.

왜 나누나
  화면 하나 고치려고 재시작하면 미국장 LOC 창에 있던 봇까지 끊긴다. 한쪽이
  죽어도 다른 쪽은 계속 돌아야 한다.

왜 나무증권(해외+국내)은 한 프로세스인가
  같은 앱키로 토큰 하나를 쓴다. 이 토큰은 '만료 전 재발급 금지' 라 두
  프로세스가 따로 받으면 서로의 토큰을 무효로 만들 수 있다. 호출 한도도
  같이 쓰므로 1.1초 간격 제어가 한 곳에 있어야 한다.
"""

import hmac
import os
import secrets
from typing import Optional

ROLES = ("all", "crypto", "namuh")
ROLE = (os.getenv("APP_ROLE") or "all").strip().lower()
if ROLE not in ROLES:
    # 잘못 적은 역할로 뜨면 봇이 아무 데서도 안 돌거나 양쪽에서 두 번 돈다.
    raise SystemExit(f"APP_ROLE={ROLE!r} 은 쓸 수 없습니다. {' · '.join(ROLES)} 중 하나여야 합니다.")

WORKER_URL = (os.getenv("APP_NAMUH_WORKER_URL") or "http://127.0.0.1:8889").rstrip("/")
INTERNAL_HEADER = "X-Internal-Token"

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_TOKEN_FILE = os.path.join(_DATA_DIR, ".internal_token")
_token: Optional[str] = None


def owns(broker: str) -> bool:
    """이 프로세스가 이 브로커의 봇을 돌리는가."""
    if ROLE == "all":
        return True
    return (broker == "namuh") == (ROLE == "namuh")


def split() -> bool:
    """크립토와 나무증권이 다른 프로세스에서 도는가."""
    return ROLE != "all"


def internal_token() -> str:
    """두 프로세스가 공유하는 내부 토큰.

    APP_INTERNAL_TOKEN 이 있으면 그것을 쓰고, 없으면 data/.internal_token 을
    읽는다. 파일도 없으면 먼저 뜬 쪽이 만든다(O_EXCL 로 한 번만). 사람이
    설정할 일이 없게 하려는 것이다. 두 프로세스는 같은 계정으로 돈다.
    """
    global _token
    if _token:
        return _token
    env = (os.getenv("APP_INTERNAL_TOKEN") or "").strip()
    if env:
        _token = env
        return _token
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(_TOKEN_FILE):
        # 다 쓴 임시 파일을 link 로 한 번에 건다. O_EXCL 로 만들고 나서 쓰면,
        # 두 프로세스가 동시에 뜰 때 한쪽이 '만들었지만 아직 빈' 파일을 읽는다
        # (분리 테스트에서 실제로 그렇게 워커가 기동에 실패했다).
        tmp = f"{_TOKEN_FILE}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        try:
            os.link(tmp, _TOKEN_FILE)
        except FileExistsError:
            pass                                # 다른 쪽이 먼저 걸었다 — 그것을 쓴다
        finally:
            os.unlink(tmp)
    with open(_TOKEN_FILE, encoding="utf-8") as f:
        _token = f.read().strip()
    if len(_token) < 20:
        raise RuntimeError(f"내부 토큰 파일이 비었거나 짧습니다: {_TOKEN_FILE}")
    return _token


def valid_internal(value: Optional[str]) -> bool:
    return bool(value) and hmac.compare_digest(value, internal_token())
