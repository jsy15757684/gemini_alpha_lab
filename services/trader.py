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

from services import bithumb, botstore, tradelog
from services import gemini_service
from services.strategy import Decision, Position, StrategyParams, compute_indicators, decide
from services.envconf import env_float, env_int

logger = logging.getLogger(__name__)

MAX_ACTIVE_BOTS = env_int("APP_MAX_ACTIVE_BOTS", 10)

# 가격 확인 주기와 캔들 갱신 주기는 분리해야 한다.
#
#   · 지표(RSI/MA)는 캔들이 닫혀야 바뀌므로 자주 받을 필요가 없다.
#   · 그러나 손절·익절·트레일링은 '현재가' 로 판단한다. 캔들 간격이 길다고
#     가격 확인까지 느리게 하면 24h 봇은 손절을 5분에 한 번만 검사하게 되어
#     급락 시 손실이 크게 밀린다. (실제로 그렇게 만들어 놨었다)
#
# 따라서 가격은 캔들 간격과 무관하게 항상 같은 주기로 확인한다.
PRICE_POLL_SEC = env_float("APP_PRICE_POLL_SEC", 10.0)

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
        self.trade_history: List[Dict[str, Any]] = []

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
        self._last_bar_time: Optional[int] = None

    def _record_trade(self, action: str, price: float, units: float, amount_krw: float,
                      pnl: float = 0.0, return_pct: float = 0.0, reason: str = ""):
        """체결된 매매 기록을 보관한다."""
        with self._lock:
            trade_item = {
                "id": f"t-{int(time.time()*1000)}-{uuid.uuid4().hex[:4]}",
                "botId": self.bot_id,
                "coin": self.coin,
                "coinName": bithumb.COINS.get(self.coin, self.coin),
                "mode": self.mode,
                "action": action,
                "turn": self.pos.turn,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "price": round(price, 0),
                "units": round(units, 8),
                "amountKrw": round(amount_krw, 0),
                "pnlKrw": round(pnl, 0),
                "returnPct": round(return_pct, 2),
                "reason": reason,
            }
            self.trade_history.insert(0, trade_item)
            del self.trade_history[500:]
        # 봇과 분리된 장부에도 남긴다. 봇을 지워도 체결 기록과 실현 손익은
        # 남아야 한다 — 화면이 '누적 정산' 이라고 부르는 근거가 이것이다.
        # 락 밖에서 호출한다(디스크 쓰기를 봇 락 안에서 하지 않는다).
        try:
            tradelog.append(trade_item)
        except Exception as e:
            logger.error(f"[{self.bot_id}] 체결 일지 기록 실패: {e}")

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
        
        if self.params.strategyType == "raoer_infinite":
            chunk_krw = self.initial_krw / self.params.splitCount
            if self.params.raoerUseAi:
                self.log("INFO", f"🔄✨ [AI 스마트 무한매수] {self.params.splitCount}분할 (기본 1회 {chunk_krw:,.0f}원 · AI 동적 0.5x~{self.params.raoerMaxMultiplier}x) | "
                                 f"가변 익절 +{self.params.raoerMinProfitPct}%~+{self.params.raoerMaxProfitPct}% · 쿼터방어 {self.params.quarterCutPct:.0f}%")
            else:
                self.log("INFO", f"🔄 [라오어 무한매수법] {self.params.splitCount}분할 매수 (1회당 {chunk_krw:,.0f}원) · "
                                 f"목표 익절 +{self.params.targetProfitPct}% · 쿼터방어 {self.params.quarterCutPct:.0f}%")
        elif self.params.strategyType == "usdt_premium":
            self.log("INFO", f"💱 [USDT 환차익] 역프 {self.params.usdtBuyPremiumPct}% 이하 매수 → "
                             f"김프 {self.params.usdtSellPremiumPct}% 이상 매도 · "
                             f"기준은 서울외환시장 공시환율")
            self.log("INFO", "손익은 프리미엄뿐 아니라 원/달러 환율 변동에도 좌우됩니다 "
                             "— 무위험 차익거래가 아닙니다.")
        elif self.params.strategyType == "raoer_vr":
            self.log("INFO", f"⚖️ [라오어 밸류리밸런싱 VR] 기울기 G={self.params.vrGradient} · 리밸런싱 밴드 ±{self.params.vrBandPct}%")
        elif self.params.useGemini:
            gem_mode_label = "순수 AI 매매" if self.params.geminiMode == "ai_only" else "하이브리드 (지표+AI 승인)"
            self.log("INFO", f"🤖 [Gemini AI 전략] {gem_mode_label} · 최소 신뢰도 {self.params.geminiMinConfidence}% 이상 진입")
        else:
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
                if self.params.strategyType == "raoer_infinite":
                    cur_bar_time = bars[-1].get("time") if bars else None
                    if self.params.raoerUseAi and (now - last_ai_check >= float(candle_ttl) or not self.last_ai_analysis):
                        try:
                            ai_res = gemini_service.analyze_raoer_context(
                                coin=self.coin,
                                interval=self.interval,
                                bars=bars,
                                turn=max(1, self.pos.turn),
                                split_count=self.params.splitCount,
                                current_price=price,
                                entry_price=self.pos.entryPrice if self.pos.open else None,
                                min_profit_pct=self.params.raoerMinProfitPct,
                                max_profit_pct=self.params.raoerMaxProfitPct,
                                max_mult=self.params.raoerMaxMultiplier,
                            )
                            if ai_res.get("success"):
                                self.last_ai_analysis = ai_res
                                last_ai_check = now
                                self.log("INFO", f"🤖 [AI 무한매수 분석] 비중 {ai_res.get('sizingMultiplier')}x 배수 | 가변 목표 +{ai_res.get('dynamicTargetProfitPct')}% ({ai_res.get('reason')})")
                        except Exception as e:
                            logger.warning(f"[{self.bot_id}] AI 무한매수 분석 실패: {e}")

                    target_tp = self.params.targetProfitPct
                    if self.params.raoerUseAi and self.last_ai_analysis and self.last_ai_analysis.get("success"):
                        target_tp = self.last_ai_analysis.get("dynamicTargetProfitPct", target_tp)

                    # 1) 목표 익절선 도달 시 즉시 전량 익절
                    if self.pos.open:
                        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                        if pnl_pct >= target_tp:
                            ai_note = f" (AI 가변목표 +{target_tp:.1f}%)" if self.params.raoerUseAi else ""
                            self.last_decision = f"무한매수 목표 익절 (+{pnl_pct:.2f}% ≥ +{target_tp:.1f}%){ai_note}"
                            self._exit(price, self.last_decision)
                            time.sleep(poll)
                            continue

                    # 2) 캔들 갱신 시점마다 기계적 / AI 동적 분할 매수 / 쿼터 방어
                    if cur_bar_time and cur_bar_time != self._last_bar_time:
                        self._last_bar_time = cur_bar_time
                        base_chunk_krw = self.initial_krw / self.params.splitCount
                        sizing_mult = 1.0
                        ai_reason = ""
                        if self.params.raoerUseAi and self.last_ai_analysis and self.last_ai_analysis.get("success"):
                            sizing_mult = self.last_ai_analysis.get("sizingMultiplier", 1.0)
                            ai_reason = f" [AI {sizing_mult}x 배수: {self.last_ai_analysis.get('reason', '')}]"

                        chunk_krw = base_chunk_krw * sizing_mult

                        if not self.pos.open:
                            self.last_decision = f"무한매수 1/{self.params.splitCount}회차 첫 매수{ai_reason}"
                            self._enter_chunk(price, chunk_krw, self.last_decision)
                        elif self.pos.turn < self.params.splitCount:
                            pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                            self.last_decision = f"무한매수 {self.pos.turn + 1}/{self.params.splitCount}회차 매수 (평단 대비 {pnl_pct:+.2f}%){ai_reason}"
                            self._enter_chunk(price, chunk_krw, self.last_decision)
                        else:
                            pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                            self.last_decision = f"무한매수 {self.params.splitCount}회 소진 쿼터매도 방어 ({pnl_pct:+.2f}%)"
                            self._exit_quarter(price, self.last_decision)
                    else:
                        if self.pos.open:
                            pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                            ai_badge = f" · AI 목표 +{target_tp:.1f}%" if self.params.raoerUseAi else ""
                            self.last_decision = f"무한매수 진행 중 (T={self.pos.turn}/{self.params.splitCount}, 평단 {self.pos.entryPrice:,.0f}원, 손익 {pnl_pct:+.2f}%{ai_badge})"
                        else:
                            self.last_decision = "무한매수 다음 캔들 1회차 대기 중"

                elif self.params.strategyType == "usdt_premium":
                    # USDT 환차익: 빗썸 USDT 가격 vs 서울외환시장 공시환율.
                    # 거래소가 하나뿐이라 '한쪽만 체결' 문제가 없다.
                    from services.arbitrage import get_official_fx_rate
                    fx, fx_err = get_official_fx_rate()
                    if not fx:
                        # 환율을 모르면 프리미엄을 계산할 수 없다. 추정하지 않는다.
                        self.last_decision = f"공시환율 조회 실패 — 판단 보류 ({fx_err})"
                        time.sleep(poll)
                        continue

                    prem = (price - fx) / fx * 100.0
                    buy_at = self.params.usdtBuyPremiumPct
                    sell_at = self.params.usdtSellPremiumPct

                    if not self.pos.open:
                        if prem <= buy_at:
                            self._enter(price, f"테더 역프 {prem:+.2f}% (매수선 {buy_at}%) · "
                                               f"공시환율 {fx:,.1f}원")
                        else:
                            self.last_decision = (f"역프 감시 중 (현재 {prem:+.2f}%, "
                                                  f"목표 ≤ {buy_at}%, 환율 {fx:,.1f}원)")
                    else:
                        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                        if prem >= sell_at:
                            self._exit(price, f"테더 김프 {prem:+.2f}% (매도선 {sell_at}%) · "
                                              f"손익 {pnl_pct:+.2f}%")
                        elif self.params.stopLossPct > 0 and pnl_pct <= -self.params.stopLossPct:
                            # 프리미엄이 아니라 환율이 무너진 경우의 안전장치.
                            # 기본은 꺼져 있다(0). 켜면 프리미엄 회복 전에 끊길 수 있다.
                            self._exit(price, f"손절 {pnl_pct:+.2f}% (환율 하락 방어) · "
                                              f"프리미엄 {prem:+.2f}%")
                        else:
                            self.last_decision = (f"김프 대기 중 (현재 {prem:+.2f}%, "
                                                  f"목표 ≥ {sell_at}%, 평가 {pnl_pct:+.2f}%)")

                elif self.params.strategyType == "raoer_vr":
                    # 라오어 밸류리밸런싱 VR:
                    cur_bar_time = bars[-1].get("time") if bars else None
                    if cur_bar_time and cur_bar_time != self._last_bar_time:
                        self._last_bar_time = cur_bar_time
                        from services.strategy import decide_raoer_vr
                        equity = self.cash + self.pos.units * price
                        d = decide_raoer_vr(price, self.pos, self.params, equity, self.cash)
                        self.last_decision = d.reason
                        if d.action == "SELL_PARTIAL":
                            amt = d.detail.get("amount", 0.0)
                            self._exit_partial(price, amt / price, d.reason)
                        elif d.action == "BUY_PARTIAL":
                            amt = d.detail.get("amount", 0.0)
                            self._enter_partial(price, amt, d.reason)
                        self.pos.vrTargetV = d.detail.get("targetV", self.pos.vrTargetV)

                elif self.params.useGemini:
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

            bal_before = 0.0
            try:
                b = self.account.get_balance()
                bal_before = float(b.get("coins", {}).get(self.coin, 0.0))
            except Exception:
                pass

            try:
                res = self.account.market_buy(self.coin, invest)
            except bithumb.BithumbError as e:
                self.log("ERROR", f"실주문 매수 실패 — 포지션 변경 없음: {e.message}")
                return
            self.log("ORDER", f"빗썸 실주문 매수 접수 (주문번호 {res.get('orderId')}, API {res.get('apiVersion')})")

            try:
                time.sleep(0.5)
                b = self.account.get_balance()
                bal_after = float(b.get("coins", {}).get(self.coin, 0.0))
                delta = bal_after - bal_before
                if delta > 0 and abs(delta - units) / max(units, 1e-8) < 0.2:
                    units = delta
                    self.log("INFO", f"실체결 보유량 동기화: {units:.8f} {self.coin}")
            except Exception as e:
                logger.warning(f"매수 후 잔고 조회 실패 (이론 수량 {units:.8f} 유지): {e}")

        self.pos = Position(units=units, entryPrice=price, peakPrice=price, turn=1, totalInvested=invest)
        self.cash = 0.0
        self._record_trade("BUY", price, units, invest, pnl=0.0, return_pct=0.0, reason=reason)
        self.log("BUY", f"매수 {units:.8f} {self.coin} @ {price:,.0f}원 "
                        f"({invest:,.0f}원) | 사유: {reason}")
        self._persist()

    def _enter_chunk(self, price: float, invest_krw: float, reason: str):
        """라오어 무한매수 분할 매수."""
        invest = min(self.cash, invest_krw)
        if invest < 5000:
            self.log("WARNING", f"분할 매수 잔여 현금 부족 ({self.cash:,.0f}원 < 5,000원). 매수 보류.")
            return
        fee = self.params.feePct / 100.0
        new_units = invest * (1 - fee) / price

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                self.log("WARNING", "실주문 보류 — 빗썸 API 키가 등록되지 않았습니다.")
                return

            bal_before = 0.0
            try:
                b = self.account.get_balance()
                bal_before = float(b.get("coins", {}).get(self.coin, 0.0))
            except Exception:
                pass

            try:
                res = self.account.market_buy(self.coin, invest)
            except bithumb.BithumbError as e:
                self.log("ERROR", f"실주문 분할 매수 실패: {e.message}")
                return
            self.log("ORDER", f"빗썸 실주문 분할 매수 접수 (주문번호 {res.get('orderId')}, API {res.get('apiVersion')})")

            try:
                time.sleep(0.5)
                b = self.account.get_balance()
                bal_after = float(b.get("coins", {}).get(self.coin, 0.0))
                delta = bal_after - bal_before
                if delta > 0 and abs(delta - new_units) / max(new_units, 1e-8) < 0.2:
                    new_units = delta
                    self.log("INFO", f"실체결 수량 동기화: {new_units:.8f} {self.coin}")
            except Exception as e:
                logger.warning(f"분할 매수 후 잔고 동기화 실패: {e}")

        u0 = self.pos.units
        p0 = self.pos.entryPrice
        u_total = u0 + new_units
        p_avg = (u0 * p0 + new_units * price) / u_total if u_total > 0 else price

        self.pos.units = u_total
        self.pos.entryPrice = p_avg
        self.pos.peakPrice = max(self.pos.peakPrice, price)
        self.pos.turn += 1
        self.pos.totalInvested += invest
        self.cash = max(0.0, self.cash - invest)

        self._record_trade("BUY_CHUNK", price, new_units, invest, pnl=0.0, return_pct=0.0, reason=reason)
        self.log("BUY", f"[{self.pos.turn}/{self.params.splitCount}회차 분할매수] {new_units:.8f} {self.coin} @ {price:,.0f}원 "
                        f"({invest:,.0f}원) | 평단가 {p_avg:,.0f}원 (총 {u_total:.8f} {self.coin}) | 사유: {reason}")
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
            try:
                bal = self.account.get_balance()
                actual_coin = bal.get("coinsAvailable", {}).get(self.coin) or bal.get("coins", {}).get(self.coin, 0.0)
                if actual_coin <= 0:
                    self.log("WARNING", f"거래소에 {self.coin} 잔고가 없습니다 (외부 매도 또는 잔고 0). 내부 포지션을 정리합니다.")
                    self.pos = Position()
                    self._persist()
                    return
                # 거래소 실제 잔고가 이 봇의 장부보다 적을 때만 실제 잔고로 제한 (타 봇/외부 물량 침범 금지)
                if actual_coin < sell_units:
                    self.log("INFO", f"매도 수량 제한: 장부 {units:.8f} → 실제 잔고 {actual_coin:.8f} {self.coin}")
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
        # 원가는 '실제로 쓴 원화'(totalInvested)다. units x entryPrice 로 잡으면
        # 매수 수수료가 빠져 손익이 그만큼 과대 계상된다.
        # 실측: 100만원 매수 후 즉시 매도 → 현금은 -800원인데 손익은 -400원.
        cost = self.pos.totalInvested if self.pos.totalInvested > 0 else units * self.pos.entryPrice
        pnl = proceeds - cost
        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100 if self.pos.entryPrice > 0 else 0.0

        self.cash += proceeds
        self.realized_pnl += pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        self._record_trade("SELL", price, units, proceeds, pnl=pnl, return_pct=pnl_pct, reason=reason)
        self.pos = Position()

        self.log("SELL", f"전량 매도 {units:.8f} {self.coin} @ {price:,.0f}원 | "
                         f"손익 {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | 사유: {reason}")
        self._persist()

    def _exit_quarter(self, price: float, reason: str):
        """라오어 무한매수 소진 시 25% 쿼터 매도 방어."""
        cut_ratio = self.params.quarterCutPct / 100.0
        units_to_sell = self.pos.units * cut_ratio
        if units_to_sell <= 0:
            return
        fee = self.params.feePct / 100.0

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                self.log("WARNING", "실주문 보류 — 빗썸 API 키가 등록되지 않았습니다.")
                return
            try:
                bal = self.account.get_balance()
                actual_coin = bal.get("coinsAvailable", {}).get(self.coin) or bal.get("coins", {}).get(self.coin, 0.0)
                if actual_coin < units_to_sell:
                    units_to_sell = actual_coin
            except Exception as e:
                logger.warning(f"쿼터 매도 전 잔고 확인 실패: {e}")

            try:
                res = self.account.market_sell(self.coin, units_to_sell)
            except bithumb.BithumbError as e:
                self.log("ERROR", f"실주문 쿼터 매도 실패: {e.message}")
                return
            self.log("ORDER", f"빗썸 실주문 쿼터 매도 접수 (주문번호 {res.get('orderId')})")

        proceeds = units_to_sell * price * (1 - fee)
        # 판 비율만큼 원가도 덜어낸다. 남은 포지션의 원가가 부풀지 않게 한다.
        sold_ratio = (units_to_sell / self.pos.units) if self.pos.units > 0 else 1.0
        cost = (self.pos.totalInvested * sold_ratio) if self.pos.totalInvested > 0 \
               else units_to_sell * self.pos.entryPrice
        self.pos.totalInvested = max(0.0, self.pos.totalInvested - cost)
        pnl = proceeds - cost
        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0 if self.pos.entryPrice > 0 else 0.0

        self.pos.units -= units_to_sell
        turns_rolled_back = max(1, int(self.params.splitCount * cut_ratio))
        self.pos.turn = max(1, self.pos.turn - turns_rolled_back)
        self.cash += proceeds
        self.realized_pnl += pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1

        self._record_trade("SELL_QUARTER", price, units_to_sell, proceeds, pnl=pnl, return_pct=pnl_pct, reason=reason)
        self.log("SELL", f"[쿼터매도 방어] {units_to_sell:.8f} {self.coin} 매도 ({proceeds:,.0f}원 확보) | "
                         f"손익 {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | 회차 조정: T={self.pos.turn} | 사유: {reason}")
        self._persist()

    def _enter_partial(self, price: float, invest_krw: float, reason: str):
        invest = min(self.cash, invest_krw)
        if invest < 5000:
            return
        fee = self.params.feePct / 100.0
        new_units = invest * (1 - fee) / price

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                return
            bal_before = 0.0
            try:
                b = self.account.get_balance()
                bal_before = float(b.get("coins", {}).get(self.coin, 0.0))
            except Exception:
                pass

            try:
                res = self.account.market_buy(self.coin, invest)
            except bithumb.BithumbError as e:
                self.log("ERROR", f"VR 부분 매수 실패: {e.message}")
                return

            try:
                time.sleep(0.5)
                b = self.account.get_balance()
                bal_after = float(b.get("coins", {}).get(self.coin, 0.0))
                delta = bal_after - bal_before
                if delta > 0 and abs(delta - new_units) / max(new_units, 1e-8) < 0.2:
                    new_units = delta
            except Exception:
                pass

        u0 = self.pos.units
        p0 = self.pos.entryPrice
        u_total = u0 + new_units
        p_avg = (u0 * p0 + new_units * price) / u_total if u_total > 0 else price

        self.pos.units = u_total
        self.pos.entryPrice = p_avg
        self.cash = max(0.0, self.cash - invest)
        self._record_trade("BUY_VR", price, new_units, invest, pnl=0.0, return_pct=0.0, reason=reason)
        self.log("BUY", f"[VR 리밸런싱 매수] {new_units:.8f} {self.coin} ({invest:,.0f}원) | 사유: {reason}")
        self._persist()

    def _exit_partial(self, price: float, sell_units: float, reason: str):
        units_to_sell = min(self.pos.units, sell_units)
        if units_to_sell <= 0:
            return
        fee = self.params.feePct / 100.0

        if self.mode == "LIVE":
            if not (self.account and self.account.configured):
                return
            try:
                bal = self.account.get_balance()
                actual_coin = bal.get("coinsAvailable", {}).get(self.coin) or bal.get("coins", {}).get(self.coin, 0.0)
                if actual_coin < units_to_sell:
                    units_to_sell = actual_coin
            except Exception as e:
                pass

            try:
                res = self.account.market_sell(self.coin, units_to_sell)
            except bithumb.BithumbError as e:
                self.log("ERROR", f"VR 부분 매도 실패: {e.message}")
                return

        proceeds = units_to_sell * price * (1 - fee)
        sold_ratio = (units_to_sell / self.pos.units) if self.pos.units > 0 else 1.0
        cost = (self.pos.totalInvested * sold_ratio) if self.pos.totalInvested > 0 \
               else units_to_sell * self.pos.entryPrice
        self.pos.totalInvested = max(0.0, self.pos.totalInvested - cost)
        pnl = proceeds - cost
        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0 if self.pos.entryPrice > 0 else 0.0
        self.pos.units -= units_to_sell
        self.cash += proceeds
        self.realized_pnl += pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        self._record_trade("SELL_VR", price, units_to_sell, proceeds, pnl=pnl, return_pct=pnl_pct, reason=reason)
        self.log("SELL", f"[VR 리밸런싱 매도] {units_to_sell:.8f} {self.coin} ({proceeds:,.0f}원) | 사유: {reason}")
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
            "turn": self.pos.turn,
            "totalInvested": self.pos.totalInvested,
            "vrTargetV": self.pos.vrTargetV,
            "realizedPnl": self.realized_pnl,
            "totalTrades": self.total_trades, "winningTrades": self.winning_trades,
            "tradeHistory": self.trade_history,
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
                           peakPrice=float(d.get("peakPrice", 0.0)),
                           turn=int(d.get("turn", 0)),
                           totalInvested=float(d.get("totalInvested", 0.0)),
                           vrTargetV=float(d.get("vrTargetV", 0.0)))
        bot.realized_pnl = float(d.get("realizedPnl", 0.0))
        bot.total_trades = int(d.get("totalTrades", 0))
        bot.winning_trades = int(d.get("winningTrades", 0))
        bot.trade_history = list(d.get("tradeHistory", []))
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
            "strategyType": self.params.strategyType,
            "turn": self.pos.turn,
            "splitCount": self.params.splitCount,
            "targetProfitPct": self.params.targetProfitPct,
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

    def all_trade_history(self) -> Dict[str, Any]:
        """전체 체결 일지와 누적 손익 정산.

        집계 근거는 현재 살아 있는 봇이 아니라 tradelog(영속 장부)다.
        봇을 지웠다고 과거 체결과 실현 손익이 사라지면 그 화면을 '누적 정산'
        이라고 부를 수 없다.

        실현 손익과 체결 횟수는 매도 행(SELL*)만 센다. 봇 내부 집계
        (realized_pnl / total_trades)가 매도에서만 증가하는 것과 같은 규칙이라
        화면의 봇별 숫자와 합계가 어긋나지 않는다.
        """
        rows = tradelog.all_rows()

        total_pnl = 0.0
        total_trades = 0
        winning_trades = 0
        total_buy_krw = 0.0
        total_sell_krw = 0.0
        by_coin: Dict[str, Dict[str, Any]] = {}

        for t in rows:
            act = t.get("action", "") or ""
            try:
                amt = float(t.get("amountKrw", 0.0) or 0.0)
                pnl = float(t.get("pnlKrw", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            coin = t.get("coin") or "?"

            stats = by_coin.setdefault(coin, {
                "coin": coin,
                "coinName": bithumb.COINS.get(coin, coin),
                "realizedPnlKrw": 0.0,
                "totalTrades": 0,
                "winningTrades": 0,
                "winRatePct": 0.0,
            })

            if "SELL" in act:
                total_sell_krw += amt
                total_pnl += pnl
                total_trades += 1
                stats["realizedPnlKrw"] += pnl
                stats["totalTrades"] += 1
                if pnl > 0:
                    winning_trades += 1
                    stats["winningTrades"] += 1
            elif "BUY" in act:
                total_buy_krw += amt

        rows.sort(key=lambda x: x.get("time", ""), reverse=True)

        win_rate = round(winning_trades / total_trades * 100, 2) if total_trades > 0 else 0.0

        coin_summary = []
        for stats in by_coin.values():
            tot = stats["totalTrades"]
            stats["winRatePct"] = round(stats["winningTrades"] / tot * 100, 2) if tot > 0 else 0.0
            stats["realizedPnlKrw"] = round(stats["realizedPnlKrw"], 0)
            coin_summary.append(stats)
        coin_summary.sort(key=lambda x: x["realizedPnlKrw"], reverse=True)

        return {
            "summary": {
                "totalRealizedPnlKrw": round(total_pnl, 0),
                "totalTrades": total_trades,
                "winningTrades": winning_trades,
                "winRatePct": win_rate,
                "totalBuyKrw": round(total_buy_krw, 0),
                "totalSellKrw": round(total_sell_krw, 0),
                "byCoin": coin_summary,
            },
            "trades": rows[:500],
        }

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
        # 봇이 하나도 없어도 장부는 올려둔다. 봇을 전부 지운 뒤에도
        # 매매 일지와 누적 손익은 계속 보여야 한다.
        tradelog.load()

        records = botstore.load()
        if not records:
            return {"restored": 0, "resumed": 0, "held": 0, "notes": []}

        # '보유량이 0' 과 '조회를 못 했다' 는 다르다. 후자를 0 으로 취급하면
        # 잘못된 사유를 안내하게 된다 (실제로 그렇게 안내했다).
        exchange: Dict[str, float] = {}
        balance_known = False
        balance_error = ""
        # 미지원 종목으로 저장된 봇도 포지션이 있으면 대조 대상이다.
        # 사용자가 실제로 정리해야 하는지 판단하려면 거래소 보유량이 필요하다.
        need_check = any(float(r.get("units", 0)) > 0 and
                         (r.get("mode") == "LIVE" or
                          bithumb.normalize_coin(r.get("coin", "")) is None)
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
        allocated_units: Dict[str, float] = {}

        for r in records:
            try:
                bot = TradingBot.restore(r, account)
            except Exception as e:
                logger.error(f"봇 복원 실패 {r.get('botId')}: {e}")
                continue
            self.bots[bot.bot_id] = bot

            # 지원 목록에서 빠진 코인(예: 취급 중단)으로 저장된 봇은 재가동하지
            # 않는다. 시세 조회가 매 틱 실패해 루프만 도는 상태가 되고, 포지션을
            # 들고 있으면 손절 감시가 되지 않는 채로 방치된다.
            if bithumb.normalize_coin(bot.coin) is None:
                # 거래소 실제 보유량을 알 수 있으면 함께 알린다. '장부에 있다' 와
                # '거래소에 있다' 는 다르고, 사용자가 확인해야 할 것은 후자다.
                if bot.pos.units <= 0:
                    where = "보유 포지션은 없습니다."
                elif balance_known:
                    actual = float(exchange.get(bot.coin, 0.0))
                    where = (f"빗썸 실제 보유량은 {actual:.8f} {bot.coin} 입니다. "
                             + ("빗썸에서 직접 정리하세요." if actual > 0
                                else "거래소에는 남아 있지 않으니 이 봇은 삭제하셔도 됩니다."))
                else:
                    where = (f"내부 장부상 {bot.pos.units:.8f} {bot.coin} 를 들고 있습니다. "
                             f"빗썸 잔고를 조회하지 못해 대조하지 못했으니 직접 확인하세요.")
                msg = (f"{bot.coin} 는 더 이상 지원하지 않는 종목이라 재가동하지 "
                       f"않습니다. {where}")
                bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                bot.is_running = False
                held += 1
                continue

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
                req_total = allocated_units.get(bot.coin, 0.0) + bot.pos.units
                # 계좌에 봇 것 외의 보유분이 있을 수 있으므로 '이상' 이면 정상으로 본다.
                if actual + 1e-8 < req_total:
                    msg = (f"내부 장부 누적({req_total:.8f} {bot.coin})이 빗썸 실제 "
                           f"보유량({actual:.8f})보다 많습니다. 재가동을 보류합니다. "
                           f"빗썸에서 실제 보유량을 확인한 뒤 이 봇을 삭제하거나 "
                           f"수동으로 정리하세요.")
                    bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                    held += 1
                    continue
                allocated_units[bot.coin] = req_total
                bot.log("INFO", f"거래소 대조 통과 (봇 장부 {bot.pos.units:.8f} / 계좌 잔고 {actual:.8f} {bot.coin})")

            if bot.pos.open:
                bot.log("WARNING",
                        f"포지션을 들고 재시작되었습니다 — 진입가 {bot.pos.entryPrice:,.0f}원 · "
                        f"{bot.pos.units:.8f} {bot.coin}. 손절·익절 감시를 재개합니다.")
            bot.start()
            resumed += 1

        self.persist()

        # 장부가 생기기 전에 쌓인 체결은 봇 스냅샷에만 있다. 한 번 끌어온다.
        # id 기준 중복 제외라 여러 번 호출해도 안전하다.
        try:
            merged = tradelog.seed([t for b in self.bots.values() for t in b.trade_history])
            if merged:
                logger.info(f"기존 봇 기록 {merged}건을 체결 일지로 이관했습니다.")
        except Exception as e:
            logger.error(f"체결 일지 이관 실패: {e}")

        summary = {"restored": len(self.bots), "resumed": resumed,
                   "held": held, "notes": notes}
        logger.info(f"봇 복원: 총 {summary['restored']}개 · 재가동 {resumed}개 · 보류 {held}개")
        for n in notes:
            logger.warning(n)
        return summary


bot_manager = BotManager()
