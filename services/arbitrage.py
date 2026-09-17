"""김프·펀딩비·환율 모니터 + 차익거래 전략 시뮬레이터.

**이 모듈은 주문을 내지 않는다.** 실제 차익거래에 필요한 다리가 없기 때문이다.

  · USDT 스왑        빗썸 USDT 실매수/매도 필요        — 미구현
  · 델타뉴트럴 헷지  해외 거래소 선물 숏 필요          — 미구현
  · 무전송 양방향    해외 거래소 계좌·주문 필요        — 미구현

바이낸스는 시세(premiumIndex) 조회만 쓴다. 거래 API 연동은 없다.
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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import requests

from services import bithumb, botstore
from services.envconf import env_float

logger = logging.getLogger(__name__)

# 이 모듈이 지원하는 유일한 모드. 주문 경로가 없으므로 실전 모드는 없다.
MODE_SIM = "SIM"

# 실시간 시세 캐시 및 락
_RADAR_CACHE: Dict[str, Any] = {}
_RADAR_CACHE_TIME = 0.0
_RADAR_TTL_SEC = 2.0
_RADAR_LOCK = threading.Lock()


_FX_CACHE: Tuple[Optional[float], str, float] = (None, "", 0.0)
_FX_TTL_SEC = 60.0
_FX_LOCK = threading.Lock()


def get_official_fx_rate() -> Tuple[Optional[float], str]:
    """서울외환시장 원/달러 기준환율.

    실패하면 (None, 사유) 를 돌려준다. 예전에는 1385.0 을 대신 돌려줬는데,
    그러면 화면은 정상으로 보이면서 김프가 통째로 틀어진다.

    공시환율은 초 단위로 바뀌지 않는다. 봇 루프가 10초마다 부르므로
    60초 캐시를 둬서 외부 호출을 줄인다.
    """
    global _FX_CACHE
    now = time.time()
    with _FX_LOCK:
        val, err, at = _FX_CACHE
        if val is not None and (now - at) < _FX_TTL_SEC:
            return val, err

    rate, err = _fetch_official_fx_rate()
    with _FX_LOCK:
        if rate is not None:
            _FX_CACHE = (rate, "", time.time())
        else:
            # 실패해도 직전 값을 버리지 않는다. 다만 오래된 값은 쓰지 않는다.
            val, _, at = _FX_CACHE
            if val is not None and (now - at) < _FX_TTL_SEC * 5:
                return val, f"{err} (직전 값 사용)"
    return rate, err


def _fetch_official_fx_rate() -> Tuple[Optional[float], str]:
    url = ("https://m.stock.naver.com/front-api/marketIndex/prices"
           "?category=exchange&reutersCode=FX_USDKRW")
    try:
        r = requests.get(url, timeout=4)
        if r.status_code != 200:
            return None, f"환율 조회 실패 (HTTP {r.status_code})"
        items = r.json().get("result", [])
        if not items:
            return None, "환율 응답이 비어 있습니다"
        val = float(str(items[0].get("closePrice", "")).replace(",", ""))
        if val <= 500:
            return None, f"환율 값이 비정상입니다 ({val})"
        return val, ""
    except Exception as e:
        return None, f"환율 조회 실패: {e}"


def fetch_binance_market_data() -> Tuple[Dict[str, Dict[str, float]], str]:
    """바이낸스 선물 마크가격과 펀딩비율 (USDT 마진).

    받지 못한 코인은 결과에 넣지 않는다. 예전에는 2024년경 고정가
    (BTC $65,000 등)를 기본값으로 두어, 조회가 실패해도 그 값으로 김프를
    계산했다. 현재가와 차이가 커서 없는 김프가 크게 생긴다.
    """
    out: Dict[str, Dict[str, float]] = {}
    wanted = set(bithumb.COINS.keys())
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=6)
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
                out[coin] = {
                    "price": float(item.get("markPrice")),
                    "fundingRate": float(item.get("lastFundingRate", 0.0)),
                }
            except (TypeError, ValueError):
                continue
        missing = sorted(wanted - set(out))
        if missing:
            return out, f"바이낸스에서 받지 못한 종목: {', '.join(missing)}"
        return out, ""
    except Exception as e:
        return out, f"바이낸스 조회 실패: {e}"


def get_arbitrage_radar(force: bool = False) -> Dict[str, Any]:
    """김프·무전송 스프레드·펀딩비 실시간 지표.

    값을 받지 못한 항목은 None 으로 두고 errors 에 사유를 남긴다.
    호출하는 쪽(봇·화면)은 None 을 '모름' 으로 다뤄야 하며 0 으로 취급하면 안 된다.
    """
    global _RADAR_CACHE, _RADAR_CACHE_TIME
    now = time.time()
    with _RADAR_LOCK:
        if not force and _RADAR_CACHE and (now - _RADAR_CACHE_TIME < _RADAR_TTL_SEC):
            return _RADAR_CACHE

        errors: List[str] = []

        official_fx, fx_err = get_official_fx_rate()
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
            fr = bn["fundingRate"] if bn else None

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
                # 펀딩비는 음수일 수 있다 (숏이 내는 구간). 부호를 그대로 둔다.
                "fundingRate8h": round(fr * 100.0, 4) if fr is not None else None,
                "fundingRateAnnualPct": round(fr * 3 * 365 * 100.0, 2) if fr is not None else None,
            })

        result = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "officialFxRate": official_fx,
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

    FEE = 0.0004   # 편도 0.04%

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
        # 해외 쪽 수량은 전략마다 의미가 다르다. 한 변수에 담으면
        # 'short' 라는 이름으로 롱 재고를 세게 되어 평가액이 틀어진다.
        self.foreign_units = 0.0        # spatial_dual: 해외에 보유한 코인(롱)
        self.hedge_short_units = 0.0    # kimkim_funding: 해외 1배 숏 수량

        # 손익은 '이번 포지션에 들어간 돈' 과 비교해야 한다.
        # 최초 자본과 비교하면 2회차부터 이전 회차 수익까지 다시 더해진다.
        self.cost_basis_krw = 0.0
        self.entry_binance_usd = 0.0     # 헤지 다리 손익 계산용
        self.accrued_funding_usdt = 0.0
        self._last_funding_at = 0.0
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

        if self.strategy == "kimkim_funding":
            # 국내 현물 절반, 해외 증거금 절반 (가상 배분)
            self.cash_krw = capital_krw * 0.5
            self.foreign_reserve_krw = capital_krw * 0.5
        else:
            self.foreign_reserve_krw = 0.0

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
                elif self.strategy == "kimkim_funding":
                    self._step_kimkim_funding(radar, fx, usdt_krw)
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

    # ── 2. 김프 델타뉴트럴 + 펀딩비 ───────────────────────────────
    def _step_kimkim_funding(self, radar: Dict[str, Any], fx: float, usdt_price: float):
        item = next((c for c in radar.get("coins", []) if c["coin"] == self.coin), None)
        if not item:
            return
        kimchi = item.get("kimchiPremiumPct")
        p_bithumb = item.get("bithumbPrice")
        p_binance = item.get("binanceUsdPrice")
        fr_8h = item.get("fundingRate8h")
        if kimchi is None or not p_bithumb or not p_binance:
            self.last_status = "김프 계산 불가 — 판단 보류"
            return

        entry_at = float(self.config.get("entryKimchiPct", 1.0))
        exit_at = float(self.config.get("exitKimchiPct", 5.0))

        if self.coin_units_domestic == 0 and self.cash_krw >= 5000:
            if kimchi <= entry_at:
                invest = self.cash_krw
                units = invest * (1 - self.FEE) / p_bithumb
                self.coin_units_domestic = units
                self.hedge_short_units = units
                self.cost_basis_krw = invest + self.foreign_reserve_krw
                self.entry_binance_usd = p_binance
                self.cash_krw = 0.0
                self.accrued_funding_usdt = 0.0
                self._last_funding_at = time.time()
                self.total_trades += 1
                self.log("BUY", f"[가상 체결] 김프 {kimchi:+.2f}% — 국내 현물 {units:.6f} {self.coin} 매수 "
                                f"+ 해외 1배 숏 {units:.6f} 동시 구축 (가상)")
                self._persist()
                self.last_status = f"헤지 유지 중 (김프 {kimchi:+.2f}%, 수량 {units:.6f})"
            else:
                self.last_status = f"김프 저점 감시 (현재 {kimchi:+.2f}%, 목표 ≤ {entry_at}%)"
            return

        # 펀딩비 누적 — 경과 시간 기준. 부호를 그대로 반영한다.
        # 숏이 받기만 하는 것이 아니다. 펀딩비가 음수면 숏이 낸다.
        if fr_8h is not None and self._last_funding_at:
            elapsed = time.time() - self._last_funding_at
            self._last_funding_at = time.time()
            notional_usdt = self.hedge_short_units * p_binance
            self.accrued_funding_usdt += notional_usdt * (fr_8h / 100.0) * (elapsed / 28800.0)

        if kimchi >= exit_at:
            units = self.coin_units_domestic
            domestic_proceeds = units * p_bithumb * (1 - self.FEE)
            # 헤지 다리 손익: 숏이므로 가격이 내리면 이익이다.
            # 이걸 빼면 '델타뉴트럴' 이라는 이름이 성립하지 않는다.
            short_pnl_krw = (self.entry_binance_usd - p_binance) * units * usdt_price
            funding_krw = self.accrued_funding_usdt * usdt_price
            total = domestic_proceeds + short_pnl_krw + funding_krw
            pnl = total - self.cost_basis_krw

            self.realized_pnl += pnl
            self.cash_krw = total * 0.5
            self.foreign_reserve_krw = total * 0.5
            self.coin_units_domestic = 0.0
            self.hedge_short_units = 0.0
            self.cost_basis_krw = 0.0
            self.accrued_funding_usdt = 0.0
            self.total_trades += 1
            self.log("SELL", f"[가상 체결] 김프 {kimchi:+.2f}% — 양다리 동시 청산 | "
                             f"현물 {domestic_proceeds:,.0f}원 · 숏 {short_pnl_krw:+,.0f}원 · "
                             f"펀딩 {funding_krw:+,.0f}원 → 손익 {pnl:+,.0f}원")
            self._persist()
            self.last_status = f"청산 완료 (누적 {self.realized_pnl:+,.0f}원)"
        else:
            self.last_status = (f"펀딩비 누적 중 (김프 {kimchi:+.2f}%, 청산 ≥ {exit_at}%, "
                                f"누적 펀딩 ${self.accrued_funding_usdt:+,.2f})")

    # ── 3. 무전송 양방향 ──────────────────────────────────────────
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
            dom_units = q * (1 - self.FEE) / p_bithumb
            for_units = q * (1 - self.FEE) / p_binance_krw
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
            sell_krw = chunk * p_bithumb * (1 - self.FEE)
            buy_usdt = chunk * p_binance * (1 + self.FEE)
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
            buy_krw = chunk * p_bithumb * (1 + self.FEE)
            sell_usdt = chunk * p_binance * (1 - self.FEE)
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

        if self.strategy == "kimkim_funding":
            if self.coin_units_domestic > 0 and (c is None or b is None):
                return None
            total = self.cash_krw + self.foreign_reserve_krw
            if self.coin_units_domestic > 0:
                total += self.coin_units_domestic * c
                # 해외는 1배 숏이다. 진입가보다 내리면 이익.
                total += (self.entry_binance_usd - b) * self.hedge_short_units * u
            total += self.accrued_funding_usdt * u
            return total

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
            "foreignReserveKrw": self.foreign_reserve_krw,
            "unitsDomestic": self.coin_units_domestic,
            "foreignUnits": self.foreign_units,
            "hedgeShortUnits": self.hedge_short_units,
            "costBasisKrw": self.cost_basis_krw,
            "entryBinanceUsd": self.entry_binance_usd,
            "accruedFundingUsdt": self.accrued_funding_usdt,
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
        bot.foreign_reserve_krw = float(d.get("foreignReserveKrw", bot.foreign_reserve_krw))
        bot.coin_units_domestic = float(d.get("unitsDomestic", 0.0))
        # 구 스냅샷은 두 의미를 한 키(unitsForeign)에 담았다. 전략으로 나눈다.
        legacy = float(d.get("unitsForeign", 0.0))
        bot.foreign_units = float(d.get("foreignUnits",
                                        legacy if d.get("strategy") == "spatial_dual" else 0.0))
        bot.hedge_short_units = float(d.get("hedgeShortUnits",
                                            legacy if d.get("strategy") == "kimkim_funding" else 0.0))
        bot.cost_basis_krw = float(d.get("costBasisKrw", 0.0))
        bot.entry_binance_usd = float(d.get("entryBinanceUsd", 0.0))
        bot.accrued_funding_usdt = float(d.get("accruedFundingUsdt", 0.0))
        bot.realized_pnl = float(d.get("realizedPnl", 0.0))
        bot.total_trades = int(d.get("totalTrades", 0))
        bot._setup_done = bool(d.get("setupDone", False))
        bot.created_at = d.get("createdAt", bot.created_at)
        bot.logs = list(d.get("logs") or [])
        # 펀딩비는 경과 시간으로 쌓는다. 복원 직후를 기준점으로 잡지 않으면
        # 서버가 꺼져 있던 시간까지 수취한 것으로 계산된다.
        bot._last_funding_at = time.time()
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
                "foreignReserveKrw": round(self.foreign_reserve_krw, 0),
                "coinUnitsDomestic": round(self.coin_units_domestic, 6),
                "foreignUnits": round(self.foreign_units, 6),
                "hedgeShortUnits": round(self.hedge_short_units, 6),
                "accruedFundingUsdt": round(self.accrued_funding_usdt, 4),
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
        records = botstore.arb_store.load()
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
