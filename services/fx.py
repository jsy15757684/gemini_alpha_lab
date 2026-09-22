"""서울외환시장 공시환율.

원래 차익거래 모듈 안에 있었다. 차익거래(USDT 환차익·무전송 양방향)를
화면에서 뺀 뒤에도 이 함수들은 남는다 — 미국주식 봇이 달러 평가액을
원화로 환산할 때 쓴다.

기준일(asOf)을 같이 돌려주는 게 핵심이다. 서울외환시장은 주 5일만 열려
토·일·공휴일에는 금요일 값이 그대로 남는다. 그 값을 지금 값처럼 쓰면
24시간 도는 시세와 비교한 '괴리' 가 실제가 아니라 분모가 낡아서 생긴
착시가 된다.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, date
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_FX_CACHE: Dict[str, Any] = {"rate": None, "asOf": None, "error": "", "at": 0.0}
_FX_TTL_SEC = 60.0
_FX_LOCK = threading.Lock()


def seoul_today() -> "date":
    """서울 기준 오늘 날짜. 서버는 UTC 로 돌지만 공시환율은 한국 영업일이다."""
    return (datetime.utcnow() + timedelta(hours=9)).date()


def get_official_fx() -> Dict[str, Any]:
    """공시환율과 **그 값의 기준일**.

    기준일이 중요하다. 서울외환시장은 주 5일만 열려서 토·일·공휴일에는
    금요일(또는 직전 영업일) 값이 그대로 남는다. 그 값을 지금 값처럼 쓰면,
    24시간 도는 빗썸 USDT 와 비교한 '프리미엄' 이 실제 괴리가 아니라
    '분모가 낡아서 생긴 착시' 가 된다.

    실측(2026-09, 24개 주말): 금 종가→월 종가로 빗썸 USDT 는 -0.242%,
    공시환율은 -0.222% 움직였다. 프리미엄 자체는 -0.020%p 밖에 안 변한다.
    즉 주말의 USDT 하락은 괴리가 아니라 **아직 공시되지 않은 환율 움직임**
    이다. 그 착시를 신호로 받아 매수한 17회는 다음 영업일 평균 -0.254%,
    승률 12.5% 였다.

    반환: {rate, asOf, ageDays, stale, error}
      - rate  : 환율 (실패하면 None)
      - asOf  : 그 값의 기준일 'YYYY-MM-DD' (모르면 None)
      - stale : 기준일이 오늘이 아니다 = 외환시장이 닫혀 있다
    """
    global _FX_CACHE
    now = time.time()
    with _FX_LOCK:
        c = dict(_FX_CACHE)
    if c["rate"] is not None and (now - c["at"]) < _FX_TTL_SEC:
        return _fx_view(c["rate"], c["asOf"], c["error"])

    rate, as_of, err = _fetch_official_fx_rate()
    if rate is not None:
        with _FX_LOCK:
            _FX_CACHE = {"rate": rate, "asOf": as_of, "error": "", "at": time.time()}
        return _fx_view(rate, as_of, "")

    # 실패해도 직전 값을 버리지 않는다. 다만 오래된 값은 쓰지 않는다.
    if c["rate"] is not None and (now - c["at"]) < _FX_TTL_SEC * 5:
        return _fx_view(c["rate"], c["asOf"], f"{err} (직전 값 사용)")
    return _fx_view(None, None, err)


def _fx_view(rate: Optional[float], as_of: Optional[str], err: str) -> Dict[str, Any]:
    age: Optional[int] = None
    if as_of:
        try:
            age = (seoul_today() - date.fromisoformat(as_of)).days
        except ValueError:
            as_of = None
    # 기준일을 모르면 신선하다고 믿지 않는다. 모르는 것은 낡은 것으로 다룬다.
    stale = rate is not None and (age is None or age > 0)
    return {"rate": rate, "asOf": as_of, "ageDays": age, "stale": stale, "error": err}


def get_official_fx_rate() -> Tuple[Optional[float], str]:
    """예전 호출부를 위한 얇은 래퍼 (값과 사유만 필요할 때)."""
    v = get_official_fx()
    return v["rate"], v["error"]


def _fetch_official_fx_rate() -> Tuple[Optional[float], Optional[str], str]:
    """(환율, 기준일, 사유) 를 돌려준다."""
    url = ("https://m.stock.naver.com/front-api/marketIndex/prices"
           "?category=exchange&reutersCode=FX_USDKRW")
    try:
        r = requests.get(url, timeout=4)
        if r.status_code != 200:
            return None, None, f"환율 조회 실패 (HTTP {r.status_code})"
        items = r.json().get("result", [])
        if not isinstance(items, list) or not items:
            return None, None, "환율 응답이 비어 있습니다"
        val = float(str(items[0].get("closePrice", "")).replace(",", ""))
        if val <= 500:
            return None, None, f"환율 값이 비정상입니다 ({val})"
        as_of = str(items[0].get("localTradedAt") or "").strip() or None
        return val, as_of, ""
    except Exception as e:
        return None, None, f"환율 조회 실패: {e}"
