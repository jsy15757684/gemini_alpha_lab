"""매크로 국면 감지 적응형 변속 기어 (Macro Regime Adaptive Gear System).

- 나스닥(QQQ) 200일 이동평균선(SMA 200)과 CBOE VIX(변동성 지수)를 실시간 추적.
- 시장 상태에 따라 3단 변속 기어를 자동 판정:
  1. 🚀 3단 [고속 질주 모드 (Bull / Low Volatility)]: QQQ > 200일선 & VIX < 20
     -> 목표 익절률 +12%~+15% 상향, 공격적 추세 추종
  2. 🔄 2단 [단기 순환 모드 (Neutral / Sideways)]: QQQ ~ 200일선 or 20 <= VIX <= 30
     -> 목표 익절률 +8%~+10% 표준, 빠른 회전율
  3. 🛡️ 1단 [생존 방어 모드 (Bear / Crisis)]: QQQ < 200일선 & VIX > 30
     -> 1회 매수금 0.5배 축소, 총알 비축 및 폭락장 바닥 생존
"""

import logging
import threading
import time
from typing import Any, Dict, Optional
import requests

logger = logging.getLogger(__name__)

# 기어 식별자
GEAR_BULL = "3_BULL"        # 🚀 3단 고속 질주
GEAR_NEUTRAL = "2_NEUTRAL"  # 🔄 2단 단기 순환
GEAR_BEAR = "1_BEAR"        # 🛡️ 1단 생존 방어

_CACHE_LOCK = threading.Lock()
_REGIME_CACHE: Optional[Dict[str, Any]] = None
_LAST_FETCH_TIME: float = 0.0
_TTL_SECONDS: float = 900.0  # 15분 캐시


def _fetch_qqq_sma200() -> Dict[str, Any]:
    """야후 파이낸스 차트 API를 통해 QQQ 1년 일봉 수신 및 200 SMA 계산."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/QQQ?range=1y&interval=1d"
    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    if res.status_code != 200:
        raise RuntimeError(f"QQQ 차트 수신 실패 (HTTP {res.status_code})")

    data = res.json()["chart"]["result"][0]
    quotes = data["indicators"]["quote"][0]
    closes = [float(c) for c in quotes.get("close", []) if c is not None and c > 0]

    if len(closes) < 200:
        raise ValueError(f"QQQ 캔들 부족 ({len(closes)}/200개)")

    current_price = closes[-1]
    sma_200 = sum(closes[-200:]) / 200.0
    diff_pct = (current_price - sma_200) / sma_200 * 100.0

    return {
        "price": round(current_price, 2),
        "sma200": round(sma_200, 2),
        "diffPct": round(diff_pct, 2),
        "isAbove200": current_price >= sma_200,
    }


def _fetch_vix() -> float:
    """CBOE VIX(변동성 지수) 수신."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/^VIX?range=5d&interval=1d"
    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    if res.status_code != 200:
        raise RuntimeError(f"VIX 차트 수신 실패 (HTTP {res.status_code})")

    data = res.json()["chart"]["result"][0]
    meta = data.get("meta", {})
    price = meta.get("regularMarketPrice")
    if price and float(price) > 0:
        return round(float(price), 2)

    quotes = data["indicators"]["quote"][0]
    closes = [float(c) for c in quotes.get("close", []) if c is not None and c > 0]
    if closes:
        return round(closes[-1], 2)
    raise ValueError("VIX 데이터가 비어 있습니다.")


def evaluate_regime(qqq_price: float, sma_200: float, vix: float) -> Dict[str, Any]:
    """QQQ 및 VIX 수치로부터 기어를 판정한다."""
    is_above = qqq_price >= sma_200
    diff_pct = (qqq_price - sma_200) / sma_200 * 100.0 if sma_200 > 0 else 0.0

    if is_above and vix < 20.0:
        gear = GEAR_BULL
        gear_num = 3
        name = "🚀 3단 고속 질주 (강세장)"
        badge_color = "#3b82f6"
        sizing_mult = 1.2
        target_tp = 12.0
        desc = "나스닥 200일선 위 & 저변동성 강세장 국면으로 목표 수익률을 +12.0%로 상향하여 수익을 극대화합니다."
    elif (not is_above) and vix >= 30.0:
        gear = GEAR_BEAR
        gear_num = 1
        name = "🛡️ 1단 생존 방어 (위기/하락장)"
        badge_color = "#ef4444"
        sizing_mult = 0.5
        target_tp = 7.0
        desc = "나스닥 200일선 하회 & VIX 30 초과 위기 국면으로 1회 매수금을 0.5배로 축소하여 총알을 아끼고 생존합니다."
    else:
        gear = GEAR_NEUTRAL
        gear_num = 2
        name = "🔄 2단 단기 순환 (횡보/중립)"
        badge_color = "#10b981"
        sizing_mult = 1.0
        target_tp = 10.0
        desc = "중립 및 박스권 횡보 국면으로 표준 1.0배 매수와 +10.0% 목표 익절률을 유지합니다."

    return {
        "gear": gear,
        "gearNumber": gear_num,
        "gearName": name,
        "badgeColor": badge_color,
        "sizingMultiplier": sizing_mult,
        "recommendedTargetProfitPct": target_tp,
        "description": desc,
        "qqqPrice": round(qqq_price, 2),
        "qqqSma200": round(sma_200, 2),
        "qqqDiffPct": round(diff_pct, 2),
        "isQqqAbove200": is_above,
        "vix": round(vix, 2),
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def neutral_regime(note: str) -> Dict[str, Any]:
    """지표를 못 받았을 때 쓰는 **진짜** 중립 국면.

    예전에는 evaluate_regime(500, 480, 18.5) 를 기본값으로 썼다. 주석에는
    '기본 안전 국면(중립 2단)' 이라고 적혀 있었지만, 그 숫자는 200일선 위 +
    VIX 20 미만이라 실제로는 **3단 고속 질주**(1.2배 매수 · 목표 12%) 로
    판정됐다. 야후가 막히면 봇이 가장 공격적으로 사들이는 구조였다.

    국면을 모를 때는 모른다고 해야 한다. 그래서 여기서는 없는 시세를
    지어내지 않고, 배수 1.0 · 목표 익절률 없음(봇이 제 설정을 그대로 씀)
    으로 돌려준다. degraded 플래그로 화면에서도 구분한다.
    """
    return {
        "gear": GEAR_NEUTRAL,
        "gearNumber": 2,
        "gearName": "🔄 2단 단기 순환 (지표 없음)",
        "badgeColor": "#6b7280",
        "sizingMultiplier": 1.0,
        # None 이면 트레이더가 목표 익절률을 덮어쓰지 않는다 (봇 설정 유지).
        "recommendedTargetProfitPct": None,
        "description": f"나스닥·VIX 지표를 받지 못해 국면을 판정할 수 없습니다. {note} "
                       f"기어 개입 없이 봇의 기본 설정(1.0배 · 설정된 목표 익절률)으로 운용합니다.",
        "qqqPrice": None,
        "qqqSma200": None,
        "qqqDiffPct": None,
        "isQqqAbove200": None,
        "vix": None,
        "degraded": True,
        "note": note,
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_macro_regime(force_refresh: bool = False) -> Dict[str, Any]:
    """현재 매크로 국면과 적응형 기어 상태를 반환한다 (15분 캐시)."""
    global _REGIME_CACHE, _LAST_FETCH_TIME

    with _CACHE_LOCK:
        now = time.time()
        if not force_refresh and _REGIME_CACHE and (now - _LAST_FETCH_TIME < _TTL_SECONDS):
            return dict(_REGIME_CACHE)

    try:
        qqq_info = _fetch_qqq_sma200()
        vix = _fetch_vix()
        regime = evaluate_regime(qqq_info["price"], qqq_info["sma200"], vix)

        with _CACHE_LOCK:
            _REGIME_CACHE = regime
            _LAST_FETCH_TIME = time.time()
        logger.info(f"[MacroRegime] 국면 갱신: {regime['gearName']} | QQQ ${regime['qqqPrice']} (SMA200 ${regime['qqqSma200']}, {regime['qqqDiffPct']:+.1f}%) | VIX {regime['vix']}")
        return dict(regime)
    except Exception as e:
        logger.warning(f"[MacroRegime] 지표 수신 실패: {e}")
        with _CACHE_LOCK:
            if _REGIME_CACHE:
                # 직전에 받아둔 값이 있으면 그대로 쓰되, 최신이 아님을 밝힌다.
                stale = dict(_REGIME_CACHE)
                stale["stale"] = True
                stale["note"] = f"최신 지표 수신 실패 — {int(time.time() - _LAST_FETCH_TIME)}초 전 값 사용"
                return stale
            return neutral_regime(f"수신 실패: {e}")
