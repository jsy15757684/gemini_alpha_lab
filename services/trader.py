"""빗썸 원화 자동매매 봇.

백테스트와 같은 strategy.decide() 를 호출한다. 판단 로직이 한 곳에만 있으므로
백테스트 결과가 실제 봇 행동을 예측한다.

지켜야 할 규칙 3가지
  1) 시세를 못 받으면 추정치를 만들지 않고 그 틱의 판단을 보류한다.
  2) LIVE 모드에서 실주문이 거부되면 내부 포지션도 바꾸지 않는다.
     (내부 장부와 거래소 실제 보유량이 어긋나는 것이 가장 위험하다)
  3) 모든 판단에는 근거가 로그로 남는다.
"""

import os
import time
import uuid
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import bithumb
from services.strategy import Decision, Position, StrategyParams, compute_indicators, decide

logger = logging.getLogger(__name__)

MAX_ACTIVE_BOTS = int(os.getenv("APP_MAX_ACTIVE_BOTS", "10"))

# 가격 확인 주기와 캔들 갱신 주기는 분리해야 한다.
#
#   · 지표(RSI/MA)는 캔들이 닫혀야 바뀌므로 자주 받을 필요가 없다.
#   · 그러나 손절·익절·트레일링은 '현재가' 로 판단한다. 캔들 간격이 길다고
#     가격 확인까지 느리게 하면 24h 봇은 손절을 5분에 한 번만 검사하게 되어
#     급락 시 손실이 크게 밀린다. (실제로 그렇게 만들어 놨었다)
#
# 따라서 가격은 캔들 간격과 무관하게 항상 같은 주기로 확인한다.
PRICE_POLL_SEC = float(os.getenv("APP_PRICE_POLL_SEC", "10"))

CANDLE_REFRESH_SECONDS = {
    "1m": 30, "3m": 60, "5m": 90, "10m": 150,
    "30m": 300, "1h": 600, "6h": 1800, "12h": 3600, "24h": 3600,
}


class TooManyBots(Exception):
    pass


class TradingBot:
    def __init__(self, bot_id: str, coin: str, interval: str, mode: str,
                 capital_krw: float, params: StrategyParams,
                 account: Optional[bithumb.BithumbAccount] = None):
        self.bot_id = bot_id
        self.coin = coin
        self.interval = interval
        self.mode = mode                     # "PAPER" | "LIVE"
        self.initial_krw = float(capital_krw)
        self.params = params
        self.account = account

        self.cash = float(capital_krw)
        self.pos = Position()
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0

        self.last_price = 0.0
        self.last_price_at = 0.0
        self.last_rsi: Optional[float] = None
        self.last_decision = "가동 대기"
        self.price_failures = 0

        self.is_running = False
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ── 로그 ──
    def log(self, level: str, message: str):
        with self._lock:
            self.logs.insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                 "level": level, "message": message})
            del self.logs[200:]
        logger.info(f"[{self.bot_id}] {level}: {message}")

    # ── 수명주기 ──
    def start(self):
        self.is_running = True
        # 루프 첫 틱 전에 상태를 조회하면 현재가가 0 으로 보였다. 시작 시점에 채운다.
        try:
            self.last_price = bithumb.get_price(self.coin)
            self.last_price_at = time.time()
        except bithumb.BithumbError as e:
            self.log("WARNING", f"시작 시점 시세 조회 실패: {e.message}")
        mode_label = "실전(LIVE)" if self.mode == "LIVE" else "모의투자(PAPER)"
        self.log("INFO", f"{mode_label} 봇 시작 · {self.coin}/KRW · {self.interval} 캔들 · "
                         f"운용자본 {self.initial_krw:,.0f}원")
        self.log("INFO", f"전략: RSI({self.params.rsiPeriod}) {self.params.rsiBuy:.0f} 상향돌파 진입 · "
                         f"익절 +{self.params.takeProfitPct}% · 손절 -{self.params.stopLossPct}%"
                         + (f" · 트레일링 {self.params.trailingStopPct}%" if self.params.trailingStopPct > 0 else "")
                         + (f" · {self.params.slowMa}봉 추세필터" if self.params.useTrendFilter else ""))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, liquidate: bool = True):
        self.is_running = False
        if liquidate and self.pos.open:
            try:
                price = bithumb.get_price(self.coin)
                self._exit(price, "사용자 정지 명령 (시장가 청산)")
            except bithumb.BithumbError as e:
                self.log("ERROR", f"청산 실패 — 포지션이 남아 있습니다: {e.message}")
        self.log("WARNING", "봇이 정지되었습니다.")

    # ── 메인 루프 ──
    def _loop(self):
        poll = PRICE_POLL_SEC
        candle_ttl = CANDLE_REFRESH_SECONDS.get(self.interval, 600)
        bars: List[Dict[str, Any]] = []
        bars_at = 0.0

        while self.is_running:
            try:
                now = time.time()

                if not bars or (now - bars_at) >= candle_ttl:
                    try:
                        candles = bithumb.get_candles(self.coin, self.interval, limit=200)
                        bars = compute_indicators(candles, self.params)
                        bars_at = now
                    except bithumb.BithumbError as e:
                        if not bars:
                            self.log("WARNING", f"캔들을 받지 못해 판단을 보류합니다: {e.message}")
                            time.sleep(poll)
                            continue
                        self.log("WARNING", f"캔들 갱신 실패, 직전 값 사용: {e.message}")

                try:
                    price = bithumb.get_price(self.coin)
                except bithumb.BithumbError as e:
                    self.price_failures += 1
                    if self.price_failures in (1, 5, 20) or self.price_failures % 60 == 0:
                        self.log("WARNING", f"시세 수신 실패 {self.price_failures}회 — "
                                            f"추정치로 매매하지 않고 보류합니다: {e.message}")
                    time.sleep(poll)
                    continue

                if self.price_failures:
                    self.log("INFO", f"시세 수신 재개 ({self.price_failures}회 실패 후)")
                    self.price_failures = 0

                self.last_price = price
                self.last_price_at = time.time()
                if self.pos.open and price > self.pos.peakPrice:
                    self.pos.peakPrice = price

                i = len(bars) - 1
                self.last_rsi = bars[i].get("rsi")
                d: Decision = decide(bars, i, price, self.pos, self.params)
                self.last_decision = d.reason

                if d.action == "BUY" and not self.pos.open:
                    self._enter(price, d.reason)
                elif d.action == "SELL" and self.pos.open:
                    self._exit(price, d.reason)

            except Exception as e:
                logger.exception(f"[{self.bot_id}] 루프 오류")
                self.log("ERROR", f"내부 오류: {e}")

            time.sleep(poll)

    # ── 체결 ──
    def _enter(self, price: float, reason: str):
        invest = self.cash
        if invest < 5000:
            return
        fee = self.params.feePct / 100.0
        units = invest * (1 - fee) / price

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                self.log("WARNING", "실주문 보류 — 빗썸 API 키가 등록되지 않았습니다.")
                return
            try:
                res = self.account.market_buy(self.coin, invest)
            except bithumb.BithumbError as e:
                # 주문이 안 나갔으면 내부 장부도 건드리지 않는다.
                self.log("ERROR", f"실주문 매수 실패 — 포지션 변경 없음: {e.message}")
                return
            self.log("ORDER", f"빗썸 실주문 매수 접수 (주문번호 {res.get('orderId')}, API {res.get('apiVersion')})")

        self.pos = Position(units=units, entryPrice=price, peakPrice=price)
        self.cash = 0.0
        self.log("BUY", f"매수 {units:.8f} {self.coin} @ {price:,.0f}원 "
                        f"({invest:,.0f}원) | 사유: {reason}")

    def _exit(self, price: float, reason: str):
        units = self.pos.units
        if units <= 0:
            return
        fee = self.params.feePct / 100.0

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                self.log("WARNING", "실주문 보류 — 빗썸 API 키가 등록되지 않았습니다.")
                return
            try:
                res = self.account.market_sell(self.coin, units)
            except bithumb.BithumbError as e:
                self.log("ERROR", f"실주문 매도 실패 — 포지션 유지: {e.message}")
                return
            self.log("ORDER", f"빗썸 실주문 매도 접수 (주문번호 {res.get('orderId')}, API {res.get('apiVersion')})")

        proceeds = units * price * (1 - fee)
        pnl = proceeds - (units * self.pos.entryPrice)
        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100

        self.cash = proceeds
        self.realized_pnl += pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        self.pos = Position()

        self.log("SELL", f"매도 {units:.8f} {self.coin} @ {price:,.0f}원 | "
                         f"손익 {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | 사유: {reason}")

    # ── 상태 ──
    def status(self) -> Dict[str, Any]:
        price = self.last_price or self.pos.entryPrice
        equity = self.cash + self.pos.units * price
        unreal = (price - self.pos.entryPrice) * self.pos.units if self.pos.open else 0.0
        return {
            "botId": self.bot_id,
            "coin": self.coin,
            "coinName": bithumb.COINS.get(self.coin, self.coin),
            "interval": self.interval,
            "mode": self.mode,
            "currency": "KRW",
            "isRunning": self.is_running,
            "createdAt": self.created_at,
            "initialKrw": round(self.initial_krw, 0),
            "equityKrw": round(equity, 0),
            "cashKrw": round(self.cash, 0),
            "units": round(self.pos.units, 8),
            "entryPrice": round(self.pos.entryPrice, 0),
            "currentPrice": round(price, 0),
            "unrealizedPnlKrw": round(unreal, 0),
            "unrealizedPnlPct": round((price - self.pos.entryPrice) / self.pos.entryPrice * 100, 2)
                                if self.pos.open and self.pos.entryPrice else 0.0,
            "realizedPnlKrw": round(self.realized_pnl, 0),
            "totalReturnPct": round((equity - self.initial_krw) / self.initial_krw * 100, 2),
            "totalTrades": self.total_trades,
            "winRatePct": round(self.winning_trades / self.total_trades * 100, 2)
                          if self.total_trades else 0.0,
            "rsi": round(self.last_rsi, 1) if self.last_rsi is not None else None,
            "priceAgeSec": round(time.time() - self.last_price_at, 1) if self.last_price_at else None,
            "pricePollSec": PRICE_POLL_SEC,
            "lastDecision": self.last_decision,
            "priceFailures": self.price_failures,
            "params": self.params.to_dict(),
            "recentLogs": self.logs[:20],
        }


class BotManager:
    def __init__(self):
        self.bots: Dict[str, TradingBot] = {}
        self._lock = threading.Lock()

    def active_count(self) -> int:
        return sum(1 for b in self.bots.values() if b.is_running)

    def _new_id(self, coin: str) -> str:
        with self._lock:
            while True:
                bid = f"{coin}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                if bid not in self.bots:
                    return bid

    def deploy(self, coin: str, interval: str, mode: str, capital_krw: float,
               params: Dict[str, Any], account: Optional[bithumb.BithumbAccount]) -> TradingBot:
        if self.active_count() >= MAX_ACTIVE_BOTS:
            raise TooManyBots(f"동시 가동 봇 상한({MAX_ACTIVE_BOTS}개)에 도달했습니다. "
                              f"기존 봇을 정지한 뒤 다시 시도하세요.")
        p = StrategyParams.from_dict(params)
        bot = TradingBot(self._new_id(coin), coin, interval, mode, capital_krw, p, account)
        self.bots[bot.bot_id] = bot
        bot.start()
        return bot

    def get(self, bot_id: str) -> Optional[TradingBot]:
        return self.bots.get(bot_id)

    def stop(self, bot_id: str) -> bool:
        bot = self.bots.get(bot_id)
        if not bot:
            return False
        bot.stop(liquidate=True)
        return True

    def stop_all(self) -> int:
        n = 0
        for bot in list(self.bots.values()):
            if bot.is_running:
                bot.stop(liquidate=True)
                n += 1
        return n

    def delete(self, bot_id: str) -> bool:
        bot = self.bots.get(bot_id)
        if not bot:
            return False
        bot.stop(liquidate=True)
        del self.bots[bot_id]
        return True

    def all_status(self) -> List[Dict[str, Any]]:
        return [b.status() for b in self.bots.values()]


bot_manager = BotManager()
