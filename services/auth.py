"""단일 사용자 인증 (환경변수 비밀번호 + 서버 세션).

이 앱은 실계좌 API 키를 보관하고 실주문을 낼 수 있는데도 인증이 전혀 없었다.
전역 broker_manager 를 사이트 방문자 전원이 공유하는 구조였으므로,
데이터 엔드포인트 전부를 세션 뒤로 옮긴다.

설계 원칙
  1) 비밀번호는 코드·저장소에 두지 않는다. 오직 환경변수 APP_ACCESS_PASSWORD.
  2) 환경변수가 없으면 '열린 상태'가 아니라 '잠긴 상태'로 실패한다 (fail closed).
  3) 세션 토큰만 쿠키로 나가고 비밀번호는 서버 밖으로 나가지 않는다.
  4) 무차별 대입에 대비해 IP 단위 시도 제한을 둔다.

세션은 메모리에만 둔다. 서버가 재시작되면 재로그인이 필요하다.
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
from services.envconf import env_float

logger = logging.getLogger(__name__)

COOKIE_NAME = "gal_session"

# 세션 유효시간 (기본 12시간)
SESSION_TTL_SEC = env_float("APP_SESSION_TTL_SEC", 12 * 60 * 60)

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
    """환경변수는 매 호출마다 읽는다. 없으면 .env 파일을 직접 조회한다."""
    val = (os.getenv("APP_ACCESS_PASSWORD") or "").strip()
    if val:
        return val
    # 환경변수에 주입되지 않은 실행 환경 대비: 프로젝트 루트의 .env 파일 직접 로드
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base_dir, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or not line:
                        continue
                    if line.startswith("APP_ACCESS_PASSWORD="):
                        p = line.split("=", 1)[1].strip()
                        if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
                            p = p[1:-1]
                        return p.strip()
        except Exception as e:
            logger.warning(f".env 파일에서 비밀번호 읽기 실패: {e}")
    return ""


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


# 지문 계산용 고정 솔트. 평문 sha256 사전 대입을 막기 위한 것이며 비밀은 아니다.
_FP_SALT = b"gemini-alpha-lab/password-fingerprint/v1"


def password_fingerprint() -> Optional[str]:
    """비밀번호의 짧은 지문. 값 자체는 드러내지 않고 '같은 값인지'만 대조하게 한다.

    로컬 .env 와 배포 환경변수가 같은 값인지 확인할 때 쓴다.
    HTTP 로 노출하지 않고 서버 기동 로그에만 남긴다.
    """
    pw = _password()
    if not pw:
        return None
    return hashlib.sha256(_FP_SALT + pw.encode("utf-8")).hexdigest()[:8]


def password_debug_line() -> str:
    """기동 로그용 한 줄. 길이와 지문만 남기고 값은 남기지 않는다."""
    pw = _password()
    if not pw:
        return "APP_ACCESS_PASSWORD 미설정 — 모든 데이터 API 가 잠깁니다."
    return (f"APP_ACCESS_PASSWORD 로드됨 · 길이 {len(pw)}자 · 지문 {password_fingerprint()} "
            f"(값은 기록하지 않습니다. 로컬과 배포의 지문이 같으면 같은 비밀번호입니다)")


def verify_password(candidate: str) -> bool:
    """타이밍 공격을 피하기 위해 compare_digest 로 비교한다."""
    expected = _password()
    if not expected:
        return False
    # 붙여넣기로 앞뒤 공백이 섞여 들어오는 경우가 흔하다.
    # 저장된 값(_password())은 이미 strip 되어 있으므로 입력도 같게 맞춘다.
    return hmac.compare_digest(
        hashlib.sha256((candidate or "").strip().encode("utf-8")).digest(),
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


def _peer_ip(request) -> str:
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def trusted_proxy_count() -> int:
    """앞단에 둔 신뢰 가능한 프록시 개수. 기본 0 (= 헤더를 믿지 않는다).

    X-Forwarded-For 는 클라이언트가 마음대로 보낼 수 있는 헤더다. 이걸 무조건
    믿으면 로그인 시도 제한이 무력화된다 — 헤더 값만 바꾸면 매번 '새 IP' 가
    되어 무제한으로 시도할 수 있다. 실제로 그랬다:
      같은 IP 8회 실패 → 429 잠금 → XFF 를 바꾸자 "남은 시도 7회" 로 초기화

    그래서 기본은 소켓 주소만 쓴다. 리버스 프록시를 둔 경우에만
    APP_TRUST_PROXY 에 그 단수를 넣는다(보통 1).
    """
    raw = (os.getenv("APP_TRUST_PROXY") or "").strip().lower()
    if not raw or raw in ("0", "false", "no", "off"):
        return 0
    if raw in ("1", "true", "yes", "on"):
        return 1
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(f"APP_TRUST_PROXY 값 '{raw}' 을 읽을 수 없어 0 으로 봅니다.")
        return 0


def client_ip(request) -> str:
    """실제 클라이언트 IP.

    프록시를 신뢰하도록 설정한 경우에만 X-Forwarded-For 를 본다. 이때도
    맨 앞이 아니라 '오른쪽에서 신뢰 단수만큼' 떨어진 항목을 쓴다. 맨 앞은
    클라이언트가 직접 써 넣은 값이라 위조가 가능하고, 오른쪽 항목이 우리가
    믿는 프록시가 기록한 값이다.
    """
    n = trusted_proxy_count()
    if n <= 0:
        return _peer_ip(request)
    parts = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",")
             if p.strip()]
    if len(parts) >= n:
        return parts[-n]
    return _peer_ip(request)


def is_https(request) -> bool:
    """세션 쿠키에 Secure 를 붙일지 판단한다.

    X-Forwarded-Proto 도 위조 가능한 헤더이므로 프록시를 신뢰할 때만 본다.
    """
    if trusted_proxy_count() > 0:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"
