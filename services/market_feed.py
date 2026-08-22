"""실시간 시세 · 기술지표 피드 (봇 전용).

봇 루프는 예전에 random.uniform() 으로 가격을 만들어 매매를 판정했다.
이 모듈은 그 자리를 대신해 '실제로 조회된 값만' 돌려준다.

설계 원칙 3가지
  1) 값을 못 받으면 절대 추정치를 만들지 않는다. None 을 돌려주고 호출자가 판단을 보류한다.
  2) 봇이 여러 개 떠도 외부 API 를 두드리는 횟수는 종목 수만큼으로 묶는다 (TTL 캐시 + 락).
  3) 빗썸 브로커는 반드시 빗썸 원화 호가를 쓴다. 달러 시세에 환율을 곱해 만들지 않는다.
"""

import time
import logging
import threading
from typing import Any, Dict, Optional

from services.market_service import get_asset_quote, get_chart_data, resolve_symbol

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 폴링 주기 (초). 빗썸 public 호가는 자주 봐도 되지만 yfinance 는 자주 두드리면 차단된다.
CRYPTO_PRICE_TTL = 8.0
EQUITY_PRICE_TTL = 60.0
# RSI/MACD/볼린저는 일봉 기반이라 하루에 한 번만 바뀐다. 5분이면 충분하다.
INDICATOR_TTL = 300.0

# 빗썸 원화 마켓에서 이 프로젝트가 다루는 코인
BITHUMB_COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}

_lock = threading.Lock()
_price_cache: Dict[str, Dict[str, Any]] = {}
_ind_cache: Dict[str, Dict[str, Any]] = {}


def _cached(cache: Dict[str, Dict[str, Any]], key: str, ttl: float) -> Optional[Any]:
    with _lock:
        hit = cache.get(key)
        if hit and (time.time() - hit["at"]) < ttl:
            return hit["value"]
    return None


def _store(cache: Dict[str, Dict[str, Any]], key: str, value: Any) -> None:
    with _lock:
        cache[key] = {"at": time.time(), "value": value}


def to_bithumb_coin(symbol: str) -> Optional[str]:
    """'BTC-USD' / 'KRW-BTC' / '비트코인' -> 'BTC'. 빗썸 원화 마켓에 없으면 None."""
    resolved = resolve_symbol(symbol).upper()
    coin = resolved.replace("-USD", "").replace("KRW-", "").replace("-KRW", "")
    return coin if coin in BITHUMB_COINS else None


def get_live_price(symbol: str, broker: str = "") -> Optional[Dict[str, Any]]:
    """실제 조회된 현재가만 반환한다. 실패하면 None.

    반환: {"price": float, "currency": "KRW"|"USD", "source": str}
    """
    is_bithumb = "BITHUMB" in (broker or "").upper()

    if is_bithumb:
        coin = to_bithumb_coin(symbol)
        if not coin:
            # 빗썸에 상장되지 않은 종목을 빗썸 브로커로 굴리려는 상황.
            # 예전 코드는 달러 시세에 1350 을 곱해 원화인 척했다. 이제는 거부한다.
            return None
        cached = _cached(_price_cache, f"bithumb:{coin}", CRYPTO_PRICE_TTL)
        if cached:
            return cached
        try:
            # 순환 import 방지를 위해 지연 import
            from services.bithumb_client import BithumbClient
            res = BithumbClient().get_ticker(coin, "KRW")
            if res.get("status") != "0000":
                logger.warning(f"Bithumb ticker 실패 {coin}: {res.get('message')}")
                return None
            price = float(res.get("data", {}).get("closing_price", 0) or 0)
            if price <= 0:
                return None
            tick = {"price": price, "currency": "KRW", "source": "bithumb-public"}
            _store(_price_cache, f"bithumb:{coin}", tick)
            return tick
        except Exception as e:
            logger.warning(f"Bithumb ticker 예외 {coin}: {e}")
            return None

    resolved = resolve_symbol(symbol)
    cached = _cached(_price_cache, f"quote:{resolved}", EQUITY_PRICE_TTL)
    if cached:
        return cached
    try:
        q = get_asset_quote(resolved)
        # get_asset_quote 는 조회 실패 시 하드코딩 값을 isFallback=True 로 표시해 돌려준다.
        # 봇은 그 값으로 절대 매매하지 않는다.
        if q.get("isFallback"):
            logger.warning(f"{resolved} 시세가 fallback 값이라 봇 판단에서 제외")
            return None
        price = float(q.get("currentPrice", 0) or 0)
        if price <= 0:
            return None
        tick = {
            "price": price,
            "currency": q.get("currency", "USD"),
            "source": q.get("dataSource", "yfinance"),
        }
        _store(_price_cache, f"quote:{resolved}", tick)
        return tick
    except Exception as e:
        logger.warning(f"시세 조회 예외 {resolved}: {e}")
        return None


def get_live_indicators(symbol: str) -> Optional[Dict[str, Any]]:
    """일봉 캔들에서 RSI · 거래량비 · 볼린저 스퀴즈 돌파 · MACD 모멘텀을 실제로 계산한다.

    캔들을 못 받거나 목업이면 None (호출자는 신규 진입을 보류해야 한다).
    """
    resolved = resolve_symbol(symbol)
    cached = _cached(_ind_cache, resolved, INDICATOR_TTL)
    if cached:
        return cached
    try:
        chart = get_chart_data(resolved, timeframe="6mo")
        if chart.get("isFallback"):
            logger.warning(f"{resolved} 차트가 목업이라 지표 계산 생략")
            return None
        candles = chart.get("candles") or []
        if len(candles) < 25:
            return None

        last = candles[-1]
        prev = candles[-2]

        rsi = last.get("rsi14")
        if rsi is None:
            return None

        # 거래량비: 최근 20일 평균 대비 %
        vols = [c.get("volume") or 0 for c in candles[-21:-1]]
        avg_vol = (sum(vols) / len(vols)) if vols else 0
        vol_ratio = round((last.get("volume", 0) / avg_vol) * 100, 1) if avg_vol > 0 else 100.0

        # MACD 모멘텀: 히스토그램이 양수이고 전일보다 확대되는 중
        hist_now = last.get("macdHist")
        hist_prev = prev.get("macdHist")
        macd_up = bool(
            hist_now is not None and hist_prev is not None
            and hist_now > 0 and hist_now > hist_prev
        )

        # 볼린저 스퀴즈 돌파: 최근 밴드폭이 20일 최저 수준까지 좁혀졌다가 상단을 뚫은 상태
        def bandwidth(c):
            up, lo, cl = c.get("bbUpper"), c.get("bbLower"), c.get("close")
            if not up or not lo or not cl:
                return None
            return (up - lo) / cl

        bws = [b for b in (bandwidth(c) for c in candles[-21:-1]) if b is not None]
        bw_prev = bandwidth(prev)
        squeeze_breakout = False
        if bws and bw_prev is not None and last.get("bbUpper"):
            was_squeezed = bw_prev <= (min(bws) * 1.25)
            broke_up = last["close"] > last["bbUpper"]
            squeeze_breakout = bool(was_squeezed and broke_up)

        ind = {
            "rsi14": round(float(rsi), 1),
            "volumeRatio": vol_ratio,
            "macdMomentumUp": macd_up,
            "isSqueezeBreakout": squeeze_breakout,
            "asOf": last.get("time"),
            "source": chart.get("dataSource", "yfinance"),
        }
        _store(_ind_cache, resolved, ind)
        return ind
    except Exception as e:
        logger.warning(f"지표 계산 예외 {resolved}: {e}")
        return None


def price_poll_seconds(symbol: str, broker: str = "") -> float:
    if "BITHUMB" in (broker or "").upper() or to_bithumb_coin(symbol):
        return CRYPTO_PRICE_TTL
    return EQUITY_PRICE_TTL
