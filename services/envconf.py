"""환경변수에서 숫자 설정을 안전하게 읽는다.

os.getenv(name, default) 는 변수가 '존재하지만 비어 있을' 때 default 가 아니라
빈 문자열을 돌려준다. 그래서 float(os.getenv("X", 10)) 는 X= 로 선언만 된
환경에서 ValueError 를 던진다.

이게 모듈 최상단(import 시점)에서 터지면 서버는 부팅 자체를 못 하고
systemd 는 무한 재시작한다. 실제로 APP_SESSION_TTL_SEC 가 빈 값이라
재시작 64회를 돌며 자동매매가 멈춘 적이 있다.

설정값 하나가 비었다고 전체가 죽어서는 안 된다. 값이 없거나 숫자가 아니면
기본값으로 돌아가고 경고만 남긴다.
"""

import os
import logging

logger = logging.getLogger(__name__)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning(
            f"{name} 값 '{raw}' 을 숫자로 읽을 수 없어 기본값 {default} 을 씁니다.")
        return float(default)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(float(raw.strip()))   # "8888.0" 같은 입력도 받아준다
    except ValueError:
        logger.warning(
            f"{name} 값 '{raw}' 을 정수로 읽을 수 없어 기본값 {default} 을 씁니다.")
        return int(default)


def load_dotenv_keys(path: str, prefixes: tuple) -> int:
    """.env 에서 이 접두어로 시작하는 줄만 환경변수로 올린다 (server.py 와 같은 규칙).

    점검 스크립트용이다. 예전에는 `sudo -u bithumb env $(grep NAMUH_ .env | xargs) python …`
    로 넘겼는데, sudo 가 명령줄 전체를 /var/log/auth.log 와 저널에 남겨 나무증권 키 ·
    시크릿 · 계좌번호가 로그에 적혔다(2026-10-08 점검: auth.log 52줄 · 저널 48줄).
    스크립트가 직접 읽으면 명령줄에 값이 없다. 이미 있는 변수와 빈 값은 건드리지 않는다.
    """
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = (x.strip() for x in line.split("=", 1))
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                if k.startswith(prefixes) and v and k not in os.environ:
                    os.environ[k] = v
                    n += 1
    except OSError:
        pass
    return n
