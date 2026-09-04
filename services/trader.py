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

from services import bithumb, botstore
from services import gemini_service
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
        self.last_ai_analysis: Optional[Dict[str, Any]] = None
        self.price_failures = 0

        self.is_running = False
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _persist(self):
        """상태가 바뀌면 전체 스냅샷을 다시 쓴다. 봇 수가 적어 비용이 미미하다."""
        try:
            bot_manager.persist()
        except Exception as e:
            logger.error(f"[{self.bot_id}] 상태 저장 실패: {e}")

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
        
        if self.params.useGemini:
            gem_mode_label = "순수 AI 매매" if self.params.geminiMode == "ai_only" else "하이브리드 (지표+AI 승인)"
            self.log("INFO", f"🤖 [Gemini AI 전략] {gem_mode_label} · 최소 신뢰도 {self.params.geminiMinConfidence}% 이상 진입")
        else:
            # 실제로 선택된 진입 규칙을 적는다. 예전엔 규칙과 무관하게 RSI 로 찍혔다.
            from services.strategy import ENTRY_RULES
            labels = [ENTRY_RULES[r]["label"] for r in self.params.entryRules if r in ENTRY_RULES]
            joiner = " AND " if self.params.entryMode == "all" else " 또는 "
            self.log("INFO", f"진입: {joiner.join(labels) or '없음'}"
                             + (f" (RSI 기준선 {self.params.rsiBuy:.0f})" if "rsiCrossUp" in self.params.entryRules else ""))
        
        self.log("INFO", f"청산: 익절 +{self.params.takeProfitPct}% · 손절 -{self.params.stopLossPct}%"
                         + f" · RSI {self.params.rsiSell:.0f} 과매수"
                         + (f" · 트레일링 {self.params.trailingStopPct}%" if self.params.trailingStopPct > 0 else "")
                         + (f" · {self.params.slowMa}봉 추세필터" if self.params.useTrendFilter else ""))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._persist()

    def stop(self, liquidate: bool = True):
        self.is_running = False
        if liquidate and self.pos.open:
            try:
                price = bithumb.get_price(self.coin)
                self._exit(price, "사용자 정지 명령 (시장가 청산)")
            except bithumb.BithumbError as e:
                self.log("ERROR", f"청산 실패 — 포지션이 남아 있습니다: {e.message}")
        self.log("WARNING", "봇이 정지되었습니다.")
        self._persist()

    # ── 메인 루프 ──
    def _loop(self):
        poll = PRICE_POLL_SEC
        candle_ttl = CANDLE_REFRESH_SECONDS.get(self.interval, 600)
        bars: List[Dict[str, Any]] = []
        bars_at = 0.0
        last_ai_check = 0.0
        ai_check_interval = 30.0  # AI 분석 갱신 주기 (30초)

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

                # ── 전략 판단 실행 ──
                if self.params.useGemini:
                    # 1) 포지션 보유 중인 경우: 익절/손절/트레일링스탑 리스크 관리 우선 확인
                    if self.pos.open:
                        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100
                        if self.params.takeProfitPct > 0 and pnl_pct >= self.params.takeProfitPct:
                            self.last_decision = f"익절 도달 (+{pnl_pct:.2f}%)"
                            self._exit(price, self.last_decision)
                            time.sleep(poll)
                            continue
                        if self.params.stopLossPct > 0 and pnl_pct <= -self.params.stopLossPct:
                            self.last_decision = f"손절 도달 ({pnl_pct:.2f}%)"
                            self._exit(price, self.last_decision)
                            time.sleep(poll)
                            continue
                        if self.params.trailingStopPct > 0 and self.pos.peakPrice > 0:
                            drop_pct = (self.pos.peakPrice - price) / self.pos.peakPrice * 100
                            if drop_pct >= self.params.trailingStopPct:
                                self.last_decision = f"트레일링 스톱 (고점 대비 -{drop_pct:.2f}%)"
                                self._exit(price, self.last_decision)
                                time.sleep(poll)
                                continue

                    # 2) 스마트 AI 트리거 방식
                    if self.params.geminiMode == "ai_only":
                        # 캔들 갱신 주기에 맞춰 스마트 AI 분석 (과도한 API 호출 방지)
                        ai_interval = max(60.0, float(candle_ttl))
                        if not self.last_ai_analysis or (now - last_ai_check) >= ai_interval:
                            try:
                                ai_res = gemini_service.analyze_coin(
                                    coin=self.coin,
                                    interval=self.interval,
                                    custom_bars=bars,
                                    current_price=price,
                                    pos_open=self.pos.open,
                                    entry_price=self.pos.entryPrice if self.pos.open else None,
                                    force_refresh=True
                                )
                                if ai_res.get("success"):
                                    self.last_ai_analysis = ai_res
                                    last_ai_check = now
                                    self.log("INFO", f"🤖 AI 분석 갱신: {ai_res.get('action')} ({ai_res.get('confidence')}%) — {ai_res.get('summary')}")
                                else:
                                    self.log("WARNING", f"AI 응답 지연: {ai_res.get('summary')}")
                            except Exception as ai_err:
                                self.log("WARNING", f"Gemini AI 분석 실패: {ai_err}")

                        ai_action = (self.last_ai_analysis or {}).get("action", "HOLD")
                        ai_conf = (self.last_ai_analysis or {}).get("confidence", 0)
                        ai_summary = (self.last_ai_analysis or {}).get("summary", "")

                        if ai_action == "BUY" and not self.pos.open:
                            if ai_conf >= self.params.geminiMinConfidence:
                                self.last_decision = f"Gemini AI 매수 신호 (신뢰도 {ai_conf}%)"
                                self._enter(price, f"Gemini AI 신호 ({ai_conf}%): {ai_summary}")
                            else:
                                self.last_decision = f"Gemini 매수 감지 (신뢰도 {ai_conf}% < 기준 {self.params.geminiMinConfidence}%)"
                        elif ai_action == "SELL" and self.pos.open:
                            if ai_conf >= self.params.geminiMinConfidence:
                                self.last_decision = f"Gemini AI 매도 신호 (신뢰도 {ai_conf}%)"
                                self._exit(price, f"Gemini AI 신호 ({ai_conf}%): {ai_summary}")
                            else:
                                self.last_decision = f"Gemini 매도 감지 (신뢰도 {ai_conf}%)"
                        else:
                            self.last_decision = f"Gemini AI 관망 ({ai_action}, {ai_conf}%) — {ai_summary or '시그널 대기'}"

                    elif self.params.geminiMode == "hybrid":
                        # 하이브리드: 기술 지표가 매수 신호를 냈을 때만 핀포인트로 Gemini AI 승인 요청!
                        d: Decision = decide(bars, i, price, self.pos, self.params)
                        if d.action == "BUY" and not self.pos.open:
                            self.log("INFO", f"⚡ 기술지표 매수 조건 포착 ({d.reason}) → Gemini AI 최종 승인 요청 중...")
                            try:
                                ai_res = gemini_service.analyze_coin(
                                    coin=self.coin,
                                    interval=self.interval,
                                    custom_bars=bars,
                                    current_price=price,
                                    pos_open=False,
                                    force_refresh=True
                                )
                                self.last_ai_analysis = ai_res
                                ai_action = ai_res.get("action", "HOLD")
                                ai_conf = ai_res.get("confidence", 0)
                                ai_summary = ai_res.get("summary", "")

                                if ai_action != "SELL" and ai_conf >= self.params.geminiMinConfidence:
                                    self.last_decision = f"하이브리드 매수 승인 (지표 + AI {ai_conf}%)"
                                    self._enter(price, f"{d.reason} + AI승인({ai_conf}%): {ai_summary}")
                                else:
                                    self.last_decision = f"기술지표 신호 발생했으나 AI 매수 미승인 ({ai_action}, 신뢰도 {ai_conf}%)"
                                    self.log("WARNING", f"진입 보류 — AI 판단: {ai_action}({ai_conf}%), 사유: {ai_summary}")
                            except Exception as ai_err:
                                self.log("WARNING", f"Gemini 검증 실패로 지표 기반 단독 진입: {ai_err}")
                                self.last_decision = f"{d.reason} (AI 폴백 진입)"
                                self._enter(price, d.reason)

                        elif d.action == "SELL" and self.pos.open:
                            self.last_decision = d.reason
                            self._exit(price, d.reason)
                        else:
                            self.last_decision = d.reason

                else:
                    # 기본 기술적 지표 전략
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

            # 실체결 후 빗썸 실제 잔고 동기화 (슬리피지/수수료 차감 반영)
            try:
                time.sleep(0.5)
                bal = self.account.get_balance()
                actual_coin = bal.get("coinsAvailable", {}).get(self.coin) or bal.get("coins", {}).get(self.coin, 0.0)
                if actual_coin > 0:
                    units = actual_coin
                    self.log("INFO", f"실체결 보유량 동기화: {units:.8f} {self.coin}")
            except Exception as e:
                logger.warning(f"매수 후 잔고 조회 실패 (이론 수량 {units:.8f} 유지): {e}")

        self.pos = Position(units=units, entryPrice=price, peakPrice=price)
        self.cash = 0.0
        self.log("BUY", f"매수 {units:.8f} {self.coin} @ {price:,.0f}원 "
                        f"({invest:,.0f}원) | 사유: {reason}")
        self._persist()

    def _exit(self, price: float, reason: str):
        units = self.pos.units
        if units <= 0:
            return
        fee = self.params.feePct / 100.0

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                self.log("WARNING", "실주문 보류 — 빗썸 API 키가 등록되지 않았습니다.")
                return

            sell_units = units
            # 실주문 매도 전 거래소 실제 주문가능 잔고 확인 및 자동 보정
            try:
                bal = self.account.get_balance()
                actual_coin = bal.get("coinsAvailable", {}).get(self.coin) or bal.get("coins", {}).get(self.coin, 0.0)
                if actual_coin <= 0:
                    self.log("WARNING", f"거래소에 {self.coin} 잔고가 없습니다 (외부 매도 또는 잔고 0). 내부 포지션을 정리합니다.")
                    self.pos = Position()
                    self._persist()
                    return
                # 슬리피지/수수료 절사 등으로 인한 잔고 차이 보정
                if actual_coin < units or abs(actual_coin - units) / max(units, 1e-8) < 0.05:
                    if abs(actual_coin - units) > 1e-8:
                        self.log("INFO", f"매도 수량 자동 보정: 장부 {units:.8f} → 실제 잔고 {actual_coin:.8f} {self.coin}")
                    sell_units = actual_coin
            except Exception as e:
                logger.warning(f"매도 전 잔고 확인 실패 (장부 수량으로 시도): {e}")

            try:
                res = self.account.market_sell(self.coin, sell_units)
                units = sell_units
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
        self._persist()

    # ── 영속화 ──
    def snapshot(self) -> Dict[str, Any]:
        """디스크에 저장할 최소 상태. 로그와 지표는 저장하지 않는다(재계산 가능)."""
        return {
            "botId": self.bot_id, "coin": self.coin, "interval": self.interval,
            "mode": self.mode, "initialKrw": self.initial_krw,
            "params": self.params.to_dict(),
            "cash": self.cash,
            "units": self.pos.units, "entryPrice": self.pos.entryPrice,
            "peakPrice": self.pos.peakPrice,
            "realizedPnl": self.realized_pnl,
            "totalTrades": self.total_trades, "winningTrades": self.winning_trades,
            "createdAt": self.created_at, "wasRunning": self.is_running,
        }

    @classmethod
    def restore(cls, d: Dict[str, Any],
                account: Optional[bithumb.BithumbAccount]) -> "TradingBot":
        bot = cls(d["botId"], d["coin"], d["interval"], d["mode"],
                  float(d["initialKrw"]), StrategyParams.from_dict(d.get("params")), account)
        bot.cash = float(d.get("cash", d["initialKrw"]))
        bot.pos = Position(units=float(d.get("units", 0.0)),
                           entryPrice=float(d.get("entryPrice", 0.0)),
                           peakPrice=float(d.get("peakPrice", 0.0)))
        bot.realized_pnl = float(d.get("realizedPnl", 0.0))
        bot.total_trades = int(d.get("totalTrades", 0))
        bot.winning_trades = int(d.get("winningTrades", 0))
        bot.created_at = d.get("createdAt", bot.created_at)
        return bot

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
            "lastAiAnalysis": self.last_ai_analysis,
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
        self.persist()
        return True

    def all_status(self) -> List[Dict[str, Any]]:
        return [b.status() for b in self.bots.values()]

    # ── 영속화 / 복원 ──

    def persist(self) -> None:
        botstore.save([b.snapshot() for b in self.bots.values()])

    def restore(self, account: Optional[bithumb.BithumbAccount]) -> Dict[str, Any]:
        """저장된 봇을 복원한다.

        LIVE 봇이 포지션을 들고 있었다면 빗썸 실제 보유량과 대조한다.
        내부 장부가 거래소보다 많다고 주장하면(= 팔 수 없는 수량) 자동으로
        재가동하지 않는다. 그 상태로 매도를 걸면 주문이 거부되거나
        의도하지 않은 수량이 나가기 때문이다. 판단은 사용자에게 맡긴다.
        """
        records = botstore.load()
        if not records:
            return {"restored": 0, "resumed": 0, "held": 0, "notes": []}

        # '보유량이 0' 과 '조회를 못 했다' 는 다르다. 후자를 0 으로 취급하면
        # 잘못된 사유를 안내하게 된다 (실제로 그렇게 안내했다).
        exchange: Dict[str, float] = {}
        balance_known = False
        balance_error = ""
        need_check = any(r.get("mode") == "LIVE" and float(r.get("units", 0)) > 0
                         for r in records)
        if need_check:
            if not (account and account.configured):
                balance_error = "빗썸 API 키가 등록되지 않았습니다."
            else:
                try:
                    exchange = account.get_balance().get("coins", {})
                    balance_known = True
                except bithumb.BithumbError as e:
                    balance_error = e.message
                    logger.error(f"복원 중 빗썸 잔고 조회 실패: {e.message}")

        notes: List[str] = []
        resumed = held = 0

        for r in records:
            try:
                bot = TradingBot.restore(r, account)
            except Exception as e:
                logger.error(f"봇 복원 실패 {r.get('botId')}: {e}")
                continue
            self.bots[bot.bot_id] = bot

            if not r.get("wasRunning"):
                bot.log("INFO", "이전에 정지된 상태로 복원되었습니다. 재가동하지 않습니다.")
                continue

            # LIVE + 포지션 보유 → 거래소와 대조
            if bot.mode == "LIVE" and bot.pos.open:
                if not balance_known:
                    msg = (f"{bot.coin} 포지션 {bot.pos.units:.8f} 를 들고 있는데 "
                           f"빗썸 잔고를 조회하지 못해 대조할 수 없습니다. "
                           f"재가동을 보류합니다. (사유: {balance_error})")
                    bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                    held += 1
                    continue
                actual = float(exchange.get(bot.coin, 0.0))
                # 계좌에 봇 것 외의 보유분이 있을 수 있으므로 '이상' 이면 정상으로 본다.
                if actual + 1e-8 < bot.pos.units:
                    msg = (f"내부 장부({bot.pos.units:.8f} {bot.coin})가 빗썸 실제 "
                           f"보유량({actual:.8f})보다 많습니다. 재가동을 보류합니다. "
                           f"빗썸에서 실제 보유량을 확인한 뒤 이 봇을 삭제하거나 "
                           f"수동으로 정리하세요.")
                    bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                    held += 1
                    continue
                bot.log("INFO", f"거래소 대조 통과 (내부 {bot.pos.units:.8f} ≤ 빗썸 {actual:.8f})")

            if bot.pos.open:
                bot.log("WARNING",
                        f"포지션을 들고 재시작되었습니다 — 진입가 {bot.pos.entryPrice:,.0f}원 · "
                        f"{bot.pos.units:.8f} {bot.coin}. 손절·익절 감시를 재개합니다.")
            bot.start()
            resumed += 1

        self.persist()
        summary = {"restored": len(self.bots), "resumed": resumed,
                   "held": held, "notes": notes}
        logger.info(f"봇 복원: 총 {summary['restored']}개 · 재가동 {resumed}개 · 보류 {held}개")
        for n in notes:
            logger.warning(n)
        return summary


bot_manager = BotManager()
