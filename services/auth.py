"""단일 사용자 인증 (환경변수 비밀번호 + 서버 세션).

이 앱은 실계좌 API 키를 보관하고 실주문을 낼 수 있는데도 인증이 전혀 없었다.
전역 broker_manager 를 사이트 방문자 전원이 공유하는 구조였으므로,
데이터 엔드포인트 전부를 세션 뒤로 옮긴다.

설계 원칙
  1) 비밀번호는 코드·저장소에 두지 않는다. 오직 환경변수 APP_ACCESS_PASSWORD.
  2) 환경변수가 없으면 '열린 상태'가 아니라 '잠긴 상태'로 실패한다 (fail closed).
  3) 세션 토큰만 쿠키로 나가고 비밀번호는 서버 밖으로 나가지 않는다.
  4) 무차별 대입에 대비해 IP 단위 시도 제한을 둔다.

Render 무료 플랜은 컨테이너가 재시작되면 메모리가 비므로 재로그인이 필요하다.
1인 사용 도구에서는 허용 가능한 트레이드오프이고, 대신 세션이 디스크에 남지 않는다.
"""

import os
import hmac
import time
import secrets
import hashlib
import logging
import threading
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

COOKIE_NAME = "gal_session"

# 세션 유효시간 (기본 12시간)
SESSION_TTL_SEC = float(os.getenv("APP_SESSION_TTL_SEC", 12 * 60 * 60))

# 로그인 시도 제한
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW_SEC = 300.0
LOCKOUT_SEC = 900.0

_lock = threading.Lock()
# token -> expires_at
_sessions: Dict[str, float] = {}
# ip -> {"count": int, "first_at": float, "locked_until": float}
_attempts: Dict[str, Dict[str, float]] = {}


def _password() -> str:
    """환경변수는 매 호출마다 읽는다 (재배포 없이 값 교체가 반영되도록)."""
    return (os.getenv("APP_ACCESS_PASSWORD") or "").strip()


def is_configured() -> bool:
    """비밀번호가 설정되어 있는지. False 면 모든 보호 엔드포인트를 차단한다."""
    return len(_password()) > 0


def password_strength_warning() -> Optional[str]:
    pw = _password()
    if not pw:
        return None
    if len(pw) < 12:
        return "APP_ACCESS_PASSWORD 가 12자 미만입니다. 더 긴 값으로 교체하세요."
    return None


def verify_password(candidate: str) -> bool:
    """타이밍 공격을 피하기 위해 compare_digest 로 비교한다."""
    expected = _password()
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256((candidate or "").encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


# ---------- 시도 제한 ----------

def lock_remaining(ip: str) -> float:
    """남은 잠금 시간(초). 0 이면 잠기지 않은 상태."""
    with _lock:
        rec = _attempts.get(ip)
        if not rec:
            return 0.0
        remaining = rec.get("locked_until", 0.0) - time.time()
        return remaining if remaining > 0 else 0.0


def register_failure(ip: str) -> float:
    """실패를 기록하고, 잠금이 걸렸으면 남은 초를 반환한다."""
    now = time.time()
    with _lock:
        rec = _attempts.get(ip)
        if not rec or (now - rec.get("first_at", now)) > ATTEMPT_WINDOW_SEC:
            rec = {"count": 0, "first_at": now, "locked_until": 0.0}
        rec["count"] = rec.get("count", 0) + 1
        if rec["count"] >= MAX_ATTEMPTS:
            rec["locked_until"] = now + LOCKOUT_SEC
            rec["count"] = 0
            rec["first_at"] = now
        _attempts[ip] = rec
        remaining = rec.get("locked_until", 0.0) - now
        return remaining if remaining > 0 else 0.0


def clear_failures(ip: str) -> None:
    with _lock:
        _attempts.pop(ip, None)


def attempts_left(ip: str) -> int:
    now = time.time()
    with _lock:
        rec = _attempts.get(ip)
        if not rec or (now - rec.get("first_at", now)) > ATTEMPT_WINDOW_SEC:
            return MAX_ATTEMPTS
        return max(0, MAX_ATTEMPTS - int(rec.get("count", 0)))


# ---------- 세션 ----------

def _purge_expired_locked() -> None:
    now = time.time()
    for tok in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(tok, None)


def create_session() -> Tuple[str, float]:
    """새 세션 토큰과 만료 시각을 발급한다."""
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_TTL_SEC
    with _lock:
        _purge_expired_locked()
        _sessions[token] = expires_at
    return token, expires_at


def validate_session(token: Optional[str]) -> bool:
    if not token:
        return False
    with _lock:
        _purge_expired_locked()
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp <= time.time():
            _sessions.pop(token, None)
            return False
        return True


def destroy_session(token: Optional[str]) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def destroy_all_sessions() -> int:
    with _lock:
        n = len(_sessions)
        _sessions.clear()
        return n


def active_session_count() -> int:
    with _lock:
        _purge_expired_locked()
        return len(_sessions)


def client_ip(request) -> str:
    """Render 등 프록시 뒤에서는 X-Forwarded-For 의 첫 항목이 실제 클라이언트다."""
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def is_https(request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"
