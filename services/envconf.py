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
