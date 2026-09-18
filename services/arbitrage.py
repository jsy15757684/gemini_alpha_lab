"""테더 프리미엄·김프·환율 모니터 + 차익거래 전략 시뮬레이터.

**이 모듈은 주문을 내지 않는다.** 실제 차익거래에 필요한 다리가 없기 때문이다.

  · USDT 스왑      빗썸 단독 — 봇 엔진의 usdt_premium 전략으로 실매매 구현됨
  · 무전송 양방향  해외 거래소 주문 연동 필요 — 여기서는 시뮬레이션만

바이낸스는 현물 시세 조회만 쓴다. 이 모듈에는 거래 API 연동이 없다.
따라서 여기서 돌아가는 봇은 전부 **가상 체결 시뮬레이션**이며, 화면도 그렇게
표시해야 한다. 한쪽 다리(빗썸)만 실주문으로 연결하면 헤지가 없는 단방향
베팅이 되므로, 그런 형태의 '실전 모드'는 두지 않는다.

지표 자체는 실시간 실데이터로 계산한다. 조회에 실패하면 추정치를 채우지 않고
실패로 표시한다 — 가짜 김프로 매매 신호를 만드는 것이 이 기능에서 가장
위험한 고장이다.
"""

import os
import time
import json
import uuid
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import requests

from services import bithumb, botstore, jsonfile
from services.envconf import env_float

logger = logging.getLogger(__name__)

# 이 모듈이 지원하는 유일한 모드. 주문 경로가 없으므로 실전 모드는 없다.
MODE_SIM = "SIM"

# 실시간 시세 캐시 및 락
_RADAR_CACHE: Dict[str, Any] = {}
_RADAR_CACHE_TIME = 0.0
_RADAR_TTL_SEC = 2.0
_RADAR_LOCK = threading.Lock()


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


def fetch_binance_market_data() -> Tuple[Dict[str, Dict[str, float]], str]:
    """바이낸스 **현물** 시세.

    예전에는 선물 마크가격(/fapi/v1/premiumIndex)을 썼다. 그런데 무전송
    양방향은 현물을 거래한다. 선물 마크가격과 현물가는 베이시스만큼 다르므로,
    지표와 실제 체결가가 어긋난다. 전략이 보는 가격으로 지표를 만든다.

    받지 못한 코인은 결과에 넣지 않는다. 추정치로 채우면 없는 괴리가 생긴다.
    """
    out: Dict[str, Dict[str, float]] = {}
    wanted = list(bithumb.COINS.keys())
    symbols = json.dumps([f"{c}USDT" for c in wanted], separators=(",", ":"))
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbols": symbols}, timeout=8)
        if r.status_code != 200:
            return out, f"바이낸스 조회 실패 (HTTP {r.status_code})"
        for item in r.json():
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            coin = sym[:-4]
            if coin not in wanted:
                continue
            try:
                out[coin] = {"price": float(item["price"])}
            except (KeyError, TypeError, ValueError):
                continue
        missing = sorted(set(wanted) - set(out))
        if missing:
            return out, f"바이낸스에서 받지 못한 종목: {', '.join(missing)}"
        return out, ""
    except Exception as e:
        return out, f"바이낸스 조회 실패: {e}"


def get_arbitrage_radar(force: bool = False) -> Dict[str, Any]:
    """테더 프리미엄·김프·무전송 괴리 실시간 지표.

    값을 받지 못한 항목은 None 으로 두고 errors 에 사유를 남긴다.
    호출하는 쪽(봇·화면)은 None 을 '모름' 으로 다뤄야 하며 0 으로 취급하면 안 된다.
    """
    global _RADAR_CACHE, _RADAR_CACHE_TIME
    now = time.time()
    with _RADAR_LOCK:
        if not force and _RADAR_CACHE and (now - _RADAR_CACHE_TIME < _RADAR_TTL_SEC):
            return _RADAR_CACHE

        errors: List[str] = []

        fx_info = get_official_fx()
        official_fx, fx_err = fx_info["rate"], fx_info["error"]
        if fx_err:
            errors.append(fx_err)

        binance_data, bn_err = fetch_binance_market_data()
        if bn_err:
            errors.append(bn_err)

        bithumb_prices: Dict[str, Optional[float]] = {}
        failed_coins: List[str] = []
        for c in list(bithumb.COINS.keys()) + ["USDT"]:
            try:
                p = bithumb.get_price(c)
                bithumb_prices[c] = p if p and p > 0 else None
            except Exception:
                bithumb_prices[c] = None
            if bithumb_prices[c] is None:
                failed_coins.append(c)
        if failed_coins:
            errors.append(f"빗썸 시세를 받지 못한 종목: {', '.join(failed_coins)}")

        usdt_price = bithumb_prices.get("USDT")

        # 테더 프리미엄은 '빗썸 USDT' 와 '공시환율' 둘 다 있어야 계산된다.
        if usdt_price is not None and official_fx:
            usdt_prem_pct = (usdt_price - official_fx) / official_fx * 100.0
            if usdt_prem_pct < -0.5:
                usdt_status = "역프리미엄"
            elif usdt_prem_pct > 2.0:
                usdt_status = "김프 과열"
            else:
                usdt_status = "정상"
        else:
            usdt_prem_pct = None
            usdt_status = "계산 불가 (데이터 없음)"

        coin_radars = []
        for c, name in bithumb.COINS.items():
            b_p = bithumb_prices.get(c)
            bn = binance_data.get(c)
            bin_p = bn["price"] if bn else None

            kimchi_pct = None
            if b_p is not None and bin_p and official_fx:
                bin_krw_official = bin_p * official_fx
                if bin_krw_official > 0:
                    kimchi_pct = (b_p - bin_krw_official) / bin_krw_official * 100.0

            spatial_pct = None
            if b_p is not None and bin_p and usdt_price:
                bin_krw_usdt = bin_p * usdt_price
                if bin_krw_usdt > 0:
                    spatial_pct = (b_p - bin_krw_usdt) / bin_krw_usdt * 100.0

            coin_radars.append({
                "coin": c,
                "name": name,
                "bithumbPrice": round(b_p, 0) if b_p is not None else None,
                "binanceUsdPrice": bin_p,
                "kimchiPremiumPct": round(kimchi_pct, 2) if kimchi_pct is not None else None,
                "spatialSpreadPct": round(spatial_pct, 2) if spatial_pct is not None else None,
            })

        result = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "officialFxRate": official_fx,
            # 이 환율이 언제 값인지. 주말·공휴일에는 직전 영업일 값이 그대로
            # 남아, 24시간 도는 USDT 와 비교한 프리미엄이 착시가 된다.
            "officialFxAsOf": fx_info["asOf"],
            "officialFxAgeDays": fx_info["ageDays"],
            "officialFxStale": fx_info["stale"],
            "bithumbUsdtPrice": usdt_price,
            "usdtPremiumPct": round(usdt_prem_pct, 2) if usdt_prem_pct is not None else None,
            "usdtStatus": usdt_status,
            "coins": coin_radars,
            "dataOk": not errors,
            "errors": errors,
        }

        _RADAR_CACHE = result
        _RADAR_CACHE_TIME = now
        return result


class ArbitrageBot:
    """차익거래 전략 시뮬레이터.

    실주문은 내지 않는다. 체결은 전부 가상이며 수수료만 반영한다.
    실제로 돌리려면 해외 거래소 주문 연동이 선행되어야 한다.
    """

    # 거래소마다 수수료가 다르다. 한 값으로 묶으면 해외 다리 비용이
    # 2.5배 과소평가되어 없는 기회가 보인다 (실제로 그랬다).
    FEE_DOMESTIC = 0.0004   # 빗썸 시장가 0.04%
    FEE_FOREIGN = 0.0010    # 바이낸스 현물 taker 0.10% (BNB 할인 시 0.075%)
    FEE = FEE_DOMESTIC      # 국내 단독 전략(usdt_swap)의 기본값

    # 호가를 넘는 비용. 중간가로 계산하면 실제보다 유리하게 나온다.
    # 실측(2026-09-18): 빗썸 BTC 0.008% · ETH 0.029% · SOL 0.139% · XRP 0.055%,
    # 바이낸스는 0.000~0.010%. 종목·시점에 따라 변하니 설정값으로 둔다.
    SLIPPAGE_DOMESTIC = 0.0005   # 0.05%
    SLIPPAGE_FOREIGN = 0.0001    # 0.01%

    def __init__(self, bot_id: str, strategy: str, coin: str,
                 capital_krw: float, config: Dict[str, Any]):
        self.bot_id = bot_id
        self.strategy = strategy
        self.coin = coin
        self.mode = MODE_SIM
        self.initial_krw = float(capital_krw)
        self.config = config

        self.cash_krw = float(capital_krw)
        self.foreign_cash_usdt = 0.0
        self.coin_units_domestic = 0.0
        self.foreign_units = 0.0        # 해외에 보유한 코인

        # 손익은 '이번 포지션에 들어간 돈' 과 비교해야 한다.
        # 최초 자본과 비교하면 2회차부터 이전 회차 수익까지 다시 더해진다.
        self.cost_basis_krw = 0.0
        self._setup_done = False

        self.is_running = False
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs: List[Dict[str, Any]] = []
        self.realized_pnl = 0.0
        self.last_status = "대기 중"
        self.total_trades = 0

        # 평가액 계산용 마지막 관측 시세. 네트워크 없이 status() 를 만들기 위해
        # 루프에서 갱신해 둔다. 없으면 None 이고, 그 경우 평가액은 None 이다
        # (0 으로 두면 '평가액 0원' 이라는 관측값처럼 보인다).
        self._last_usdt_krw: Optional[float] = None
        self._last_coin_krw: Optional[float] = None
        self._last_binance_usd: Optional[float] = None

        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()


    def log(self, kind: str, message: str):
        item = {"time": datetime.now().strftime("%H:%M:%S"),
                "kind": kind, "message": message}
        with self._lock:
            self.logs.append(item)
            if len(self.logs) > 200:
                self.logs.pop(0)
        logger.info(f"[{self.bot_id}] {message}")

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("INFO", f"{self.strategy} 시뮬레이션 시작 (가상 자본 {self.initial_krw:,.0f}원) "
                         f"— 실제 주문은 나가지 않습니다")

    def stop(self):
        self.is_running = False
        self.last_status = "정지됨"
        self.log("INFO", "시뮬레이션 정지")
        self._persist()

    def _run_loop(self):
        while self.is_running:
            try:
                radar = get_arbitrage_radar()
                fx = radar.get("officialFxRate")
                usdt_krw = radar.get("bithumbUsdtPrice")

                self._last_usdt_krw = usdt_krw
                item = next((c for c in radar.get("coins", [])
                             if c["coin"] == self.coin), None)
                if item:
                    self._last_coin_krw = item.get("bithumbPrice")
                    self._last_binance_usd = item.get("binanceUsdPrice")

                # 값이 없으면 판단을 보류한다. 0 이나 추정치로 대신하지 않는다.
                if not fx or not usdt_krw:
                    self.last_status = ("데이터 없음 — 판단 보류 ("
                                        + "; ".join(radar.get("errors", [])) + ")")
                elif self.strategy == "usdt_swap":
                    self._step_usdt_swap(radar, fx, usdt_krw)
                elif self.strategy == "spatial_dual":
                    self._step_spatial_dual(radar, fx, usdt_krw)

            except Exception as e:
                self.log("ERROR", f"실행 중 오류: {e}")

            time.sleep(3.0)

    # ── 1. USDT 환차익 스왑 ───────────────────────────────────────
    def _step_usdt_swap(self, radar: Dict[str, Any], fx: float, usdt_price: float):
        prem_pct = radar.get("usdtPremiumPct")
        if prem_pct is None:
            self.last_status = "테더 프리미엄 계산 불가 — 판단 보류"
            return

        buy_at = float(self.config.get("usdtBuyThreshold", -0.8))
        sell_at = float(self.config.get("usdtSellThreshold", 2.0))

        if self.coin_units_domestic == 0 and self.cash_krw >= 5000:
            if prem_pct <= buy_at:
                invest = self.cash_krw
                units = invest * (1 - self.FEE) / usdt_price
                self.coin_units_domestic = units
                self.cost_basis_krw = invest
                self.cash_krw = 0.0
                self.total_trades += 1
                self.log("BUY", f"[가상 체결] 역프 {prem_pct:+.2f}% — USDT {units:,.2f} 매수 "
                                f"(@ {usdt_price:,.0f}원, {invest:,.0f}원)")
                self._persist()
                self.last_status = f"테더 보유 (진입 {usdt_price:,.0f}원, 프리미엄 {prem_pct:+.2f}%)"
            else:
                self.last_status = f"역프 감시 중 (현재 {prem_pct:+.2f}%, 목표 ≤ {buy_at}%)"

        elif self.coin_units_domestic > 0:
            if prem_pct >= sell_at:
                units = self.coin_units_domestic
                proceeds = units * usdt_price * (1 - self.FEE)
                pnl = proceeds - self.cost_basis_krw       # 이번 회차 원금과 비교
                self.realized_pnl += pnl
                self.cash_krw = proceeds
                self.coin_units_domestic = 0.0
                self.cost_basis_krw = 0.0
                self.total_trades += 1
                self.log("SELL", f"[가상 체결] 김프 {prem_pct:+.2f}% — USDT 전량 매도 "
                                 f"{proceeds:,.0f}원 회수 (손익 {pnl:+,.0f}원)")
                self._persist()
                self.last_status = f"청산 완료 (누적 {self.realized_pnl:+,.0f}원)"
            else:
                now_val = self.coin_units_domestic * usdt_price
                self.last_status = (f"청산 대기 (현재 {prem_pct:+.2f}%, 목표 ≥ {sell_at}%, "
                                    f"평가 {now_val - self.cost_basis_krw:+,.0f}원)")

    # ── 2. 무전송 양방향 ──────────────────────────────────────────
    def _step_spatial_dual(self, radar: Dict[str, Any], fx: float, usdt_price: float):
        """거래소 간 괴리를 양방향으로 먹는다.

        '무전송' 은 코인을 옮기지 않는다는 뜻이다. 그래서 양쪽에 재고와 현금을
        모두 들고, 비싼 쪽에서 팔고 싼 쪽에서 사서 구성을 맞바꾼다.

        한 방향으로만 돌면 그쪽 재고가 소진되는 순간 영구히 멈춘다.
        (이전 구현이 그랬다 — 국내 재고를 다 팔면 '재고 부족' 으로 대기했다.)
        """
        item = next((c for c in radar.get("coins", []) if c["coin"] == self.coin), None)
        if not item:
            return
        spread = item.get("spatialSpreadPct")
        p_bithumb = item.get("bithumbPrice")
        p_binance = item.get("binanceUsdPrice")
        if spread is None or not p_bithumb or not p_binance:
            self.last_status = "스프레드 계산 불가 — 판단 보류"
            return

        p_binance_krw = p_binance * usdt_price

        # 초기 재고 구축도 '거래' 다. 예전에는 생성 시점에 보유량을 그냥
        # 채워 넣어, 아무 체결 없이 포지션이 생긴 것처럼 보였다.
        # 양방향으로 돌려면 양쪽에 재고와 현금이 모두 있어야 한다.
        if not self._setup_done:
            q = self.initial_krw * 0.25
            dom_units = q * (1 - self.FEE_DOMESTIC) / p_bithumb
            for_units = q * (1 - self.FEE_FOREIGN) / p_binance_krw
            self.coin_units_domestic = dom_units
            self.foreign_units = for_units
            self.foreign_cash_usdt = q / usdt_price
            self.cash_krw = self.initial_krw - q * 3
            self.cost_basis_krw = self.initial_krw
            self._setup_done = True
            self.total_trades += 1
            self.log("BUY", f"[가상 체결] 초기 재고 구축 — 국내 {dom_units:.6f} · "
                            f"해외 {for_units:.6f} {self.coin} · 원화 {self.cash_krw:,.0f}원 · "
                            f"해외현금 ${self.foreign_cash_usdt:,.2f}")
            self._persist()
            return

        trigger = abs(float(self.config.get("triggerSpreadPct", 0.4)))
        dust = 1e-7
        min_krw = 5000.0
        min_usdt = 5.0

        # 국내가 비싸다 → 국내 매도 + 해외 매수
        if spread >= trigger:
            if self.coin_units_domestic <= dust or self.foreign_cash_usdt < min_usdt:
                self.last_status = (f"국내 고평가 {spread:+.2f}% 포착 — 국내 재고 또는 "
                                    f"해외 현금 부족으로 대기")
                return
            chunk = min(self.coin_units_domestic * 0.25,
                        (self.foreign_cash_usdt * 0.5) / p_binance)
            if chunk <= dust:
                self.last_status = "체결 가능 수량이 너무 작습니다"
                return
            # 국내 매도는 매수호가로, 해외 매수는 매도호가로 체결된다.
            sell_krw = chunk * p_bithumb * (1 - self.FEE_DOMESTIC - self.SLIPPAGE_DOMESTIC)
            buy_usdt = chunk * p_binance * (1 + self.FEE_FOREIGN + self.SLIPPAGE_FOREIGN)
            margin = sell_krw - buy_usdt * usdt_price

            self.coin_units_domestic -= chunk
            self.cash_krw += sell_krw
            self.foreign_cash_usdt -= buy_usdt
            self.foreign_units += chunk
            direction = "국내 매도 → 해외 매수"

        # 국내가 싸다 → 국내 매수 + 해외 매도
        elif spread <= -trigger:
            if self.foreign_units <= dust or self.cash_krw < min_krw:
                self.last_status = (f"국내 저평가 {spread:+.2f}% 포착 — 해외 재고 또는 "
                                    f"원화 부족으로 대기")
                return
            chunk = min(self.foreign_units * 0.25,
                        (self.cash_krw * 0.5) / p_bithumb)
            if chunk <= dust:
                self.last_status = "체결 가능 수량이 너무 작습니다"
                return
            buy_krw = chunk * p_bithumb * (1 + self.FEE_DOMESTIC + self.SLIPPAGE_DOMESTIC)
            sell_usdt = chunk * p_binance * (1 - self.FEE_FOREIGN - self.SLIPPAGE_FOREIGN)
            margin = sell_usdt * usdt_price - buy_krw

            self.foreign_units -= chunk
            self.foreign_cash_usdt += sell_usdt
            self.cash_krw -= buy_krw
            self.coin_units_domestic += chunk
            direction = "해외 매도 → 국내 매수"

        else:
            self.last_status = (f"양방향 괴리 감시 중 (현재 {spread:+.2f}%, "
                                f"기준 ±{trigger}%) | 국내 {self.coin_units_domestic:.6f} · "
                                f"해외 {self.foreign_units:.6f} {self.coin}")
            return

        self.realized_pnl += margin
        self.total_trades += 1
        self.log("ORDER", f"[가상 체결] 괴리 {spread:+.2f}% — {direction} "
                          f"{chunk:.6f} {self.coin} (마진 {margin:+,.0f}원)")
        self.last_status = (f"차익 실현 중 ({direction}, 스프레드 {spread:+.2f}%, "
                            f"누적 {self.realized_pnl:+,.0f}원)")
        self._persist()

    # ── 평가액 ────────────────────────────────────────────────────
    def equity_krw(self) -> Optional[float]:
        """현재 총 평가액(원). 시세를 아직 못 받았으면 None.

        실현 손익만으로는 시뮬레이션 성과를 판단할 수 없다. 전략마다 가치가
        국내 현금 · 국내 코인 · 해외 증거금 · 해외 포지션에 나뉘어 있어서,
        한쪽만 보면 재고가 줄어든 것을 수익으로 착각하게 된다.
        """
        u = self._last_usdt_krw
        c = self._last_coin_krw
        b = self._last_binance_usd
        if u is None:
            return None

        if self.strategy == "usdt_swap":
            # coin_units_domestic 은 USDT 수량이다.
            return self.cash_krw + self.coin_units_domestic * u

        if self.strategy == "spatial_dual":
            if (self.coin_units_domestic > 0 or self.foreign_units > 0) \
               and (c is None or b is None):
                return None
            total = self.cash_krw + self.foreign_cash_usdt * u
            if self.coin_units_domestic > 0:
                total += self.coin_units_domestic * c
            if self.foreign_units > 0:
                total += self.foreign_units * b * u
            return total

        return None

    # ── 영속화 ────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        return {
            "botId": self.bot_id, "strategy": self.strategy, "coin": self.coin,
            "initialKrw": self.initial_krw, "config": self.config,
            "cashKrw": self.cash_krw,
            "foreignCashUsdt": self.foreign_cash_usdt,
            "unitsDomestic": self.coin_units_domestic,
            "foreignUnits": self.foreign_units,
            "costBasisKrw": self.cost_basis_krw,
            "realizedPnl": self.realized_pnl,
            "totalTrades": self.total_trades,
            "setupDone": self._setup_done,
            "createdAt": self.created_at,
            "wasRunning": self.is_running,
            "logs": self.logs[-50:],
        }

    @classmethod
    def from_snapshot(cls, d: Dict[str, Any]) -> "ArbitrageBot":
        bot = cls(d["botId"], d["strategy"], d["coin"],
                  float(d.get("initialKrw", 1_000_000.0)), d.get("config") or {})
        bot.cash_krw = float(d.get("cashKrw", bot.cash_krw))
        bot.foreign_cash_usdt = float(d.get("foreignCashUsdt", 0.0))
        bot.coin_units_domestic = float(d.get("unitsDomestic", 0.0))
        # 구 스냅샷은 두 의미를 한 키(unitsForeign)에 담았다. 전략으로 나눈다.
        # 구 스냅샷은 unitsForeign 한 키를 썼다.
        bot.foreign_units = float(d.get("foreignUnits", d.get("unitsForeign", 0.0)))
        bot.cost_basis_krw = float(d.get("costBasisKrw", 0.0))
        bot.realized_pnl = float(d.get("realizedPnl", 0.0))
        bot.total_trades = int(d.get("totalTrades", 0))
        bot._setup_done = bool(d.get("setupDone", False))
        bot.created_at = d.get("createdAt", bot.created_at)
        bot.logs = list(d.get("logs") or [])
        return bot

    def _persist(self):
        try:
            arbitrage_manager.persist()
        except Exception as e:
            logger.error(f"[{self.bot_id}] 시뮬레이터 상태 저장 실패: {e}")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            eq = self.equity_krw()
            return {
                "botId": self.bot_id,
                "strategy": self.strategy,
                "coin": self.coin,
                "mode": self.mode,
                "simulated": True,
                "initialKrw": self.initial_krw,
                "equityKrw": round(eq, 0) if eq is not None else None,
                "totalReturnPct": (round((eq - self.initial_krw) / self.initial_krw * 100.0, 2)
                                   if eq is not None and self.initial_krw > 0 else None),
                "cashKrw": round(self.cash_krw, 0),
                "foreignCashUsdt": round(self.foreign_cash_usdt, 2),
                "coinUnitsDomestic": round(self.coin_units_domestic, 6),
                "foreignUnits": round(self.foreign_units, 6),
                "realizedPnl": round(self.realized_pnl, 0),
                "returnPct": round((self.realized_pnl / self.initial_krw * 100.0)
                                   if self.initial_krw > 0 else 0.0, 2),
                "totalTrades": self.total_trades,
                "isRunning": self.is_running,
                "lastStatus": self.last_status,
                "createdAt": self.created_at,
                "logs": self.logs[-20:],
            }


class ArbitrageBotManager:
    """시뮬레이터 인스턴스 관리.

    주문 경로가 없으므로 계좌(account)를 받지 않는다. 받아두면 나중에
    '실전 모드' 가 있는 것처럼 오해할 여지를 남긴다.

    상태는 data/arb_bots.json 에 저장한다. 실계좌 봇(bots.json)과 파일을
    분리해 가상 체결이 실제 포지션 기록에 섞이지 않게 한다.
    """

    def __init__(self):
        self.bots: Dict[str, ArbitrageBot] = {}
        # RLock 이어야 한다. stop_bot/delete_bot 이 락을 쥔 채 bot.stop() 을
        # 부르고, 그 안에서 _persist() → persist() 로 같은 락을 다시 잡는다.
        # 일반 Lock 이면 그 자리에서 교착된다 (실제로 걸렸다).
        self._lock = threading.RLock()

    def create_bot(self, strategy: str, coin: str, capital_krw: float,
                   config: Dict[str, Any]) -> ArbitrageBot:
        with self._lock:
            bot_id = f"arb-{strategy[:4]}-{uuid.uuid4().hex[:6]}"
            bot = ArbitrageBot(bot_id, strategy, coin, capital_krw, config)
            self.bots[bot_id] = bot
            bot.start()
        self.persist()
        return bot

    def stop_bot(self, bot_id: str) -> bool:
        with self._lock:
            bot = self.bots.get(bot_id)
            if not bot:
                return False
            bot.stop()
        self.persist()
        return True

    def delete_bot(self, bot_id: str) -> bool:
        with self._lock:
            bot = self.bots.get(bot_id)
            if not bot:
                return False
            bot.stop()
            del self.bots[bot_id]
        self.persist()
        return True

    def list_bots(self) -> List[Dict[str, Any]]:
        with self._lock:
            bots = list(self.bots.values())
        return [b.status() for b in bots]

    # ── 영속화 ────────────────────────────────────────────────────
    def persist(self) -> None:
        with self._lock:
            records = [b.snapshot() for b in self.bots.values()]
        botstore.arb_store.save(records)

    def restore(self) -> Dict[str, Any]:
        """저장된 시뮬레이터를 복원한다.

        실주문이 없으므로 거래소 대조는 필요 없다. 다만 지원 목록에서 빠진
        종목은 지표를 계산할 수 없으므로 재가동하지 않는다.
        """
        try:
            records = botstore.arb_store.load()
        except jsonfile.StoreReadError as e:
            # 시뮬레이터라 실계좌 위험은 없지만, 못 읽은 파일을 빈 것으로
            # 취급해 덮어쓰면 기록이 사라진다. 복원을 포기하고 파일은 둔다.
            logger.error(f"시뮬레이터 복원을 중단했습니다 — {e} "
                         "원인을 고친 뒤 서비스를 재시작하세요.")
            return {"restored": 0, "resumed": 0, "error": str(e)}

        if not records:
            return {"restored": 0, "resumed": 0}

        resumed = 0
        for r in records:
            try:
                bot = ArbitrageBot.from_snapshot(r)
            except Exception as e:
                logger.error(f"시뮬레이터 복원 실패 {r.get('botId')}: {e}")
                continue
            self.bots[bot.bot_id] = bot

            if bot.coin != "USDT" and bithumb.normalize_coin(bot.coin) is None:
                bot.log("ERROR", f"{bot.coin} 는 더 이상 지원하지 않는 종목이라 "
                                 f"재가동하지 않습니다.")
                continue
            if r.get("wasRunning"):
                bot.start()
                resumed += 1
            else:
                bot.log("INFO", "이전에 정지된 상태로 복원되었습니다.")

        logger.info(f"차익거래 시뮬레이터 복원: 총 {len(self.bots)}개 · 재가동 {resumed}개")
        return {"restored": len(self.bots), "resumed": resumed}


arbitrage_manager = ArbitrageBotManager()
