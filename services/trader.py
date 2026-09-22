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
from typing import Any, Dict, List, Optional, Tuple

from services import backtest, bithumb, jsonfile, botstore, tradelog
from services import gemini_service, namuh, macro_regime
from services.namuh import NamuhAccount, NAMUH_STOCKS
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


class LiquidationFailed(Exception):
    """청산하지 못한 봇은 지우지 않는다.

    지우면 계좌에 주인 없는 물량이 남는다 — 익절·손절 감시도 없고, 거래소
    대조에도 안 잡힌다. 실제로 미국장이 닫힌 시각에 삭제했더니 매도가
    거부됐는데 봇만 사라져 TQQQ 1주가 고아가 됐다.
    """
    pass


class TradingBot:
    def __init__(self, bot_id: str, coin: str, interval: str, mode: str,
                 capital_krw: float, params: StrategyParams,
                 account: Optional[bithumb.BithumbAccount] = None,
                 namuh_account: Optional[namuh.NamuhAccount] = None,
                 broker: str = "bithumb"):
        self.bot_id = bot_id
        self.coin = coin.upper().strip()
        self.interval = interval
        self.mode = mode                     # "PAPER" | "LIVE"
        self.broker = broker
        if self.coin in NAMUH_STOCKS or self.broker == "namuh":
            self.broker = "namuh"
            self.market = "US_STOCK"
            self.currency = "USD"
            self.curr_symbol = "$"
            self.coin_name = NAMUH_STOCKS.get(self.coin, {}).get("name", self.coin)
        else:
            self.broker = "bithumb"
            self.market = "CRYPTO"
            self.currency = "KRW"
            self.curr_symbol = "원"
            self.coin_name = bithumb.COINS.get(self.coin, self.coin)

        self.initial_krw = float(capital_krw)
        self.params = params
        self.account = account
        self.namuh_account = namuh_account

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
        self.last_macro_regime: Optional[Dict[str, Any]] = None
        self.price_failures = 0

        self.is_running = False
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_bar_time: Optional[int] = None
        # 복원 직후 첫 봉을 '이미 소비한 것' 으로 볼지 (아래 restore 참고)
        self._adopt_bar_on_start = False
        # 미국 주식 정수 1주 매수 후 남은 잔돈 이월금 (USD)
        self.budget_carryover = 0.0
        # 원전 LOC: 접수했지만 아직 정산 안 한 주문들. 취소 API 가 없어서
        # 한번 낸 주문은 거둬들일 수 없다. 봇이 꺼져도 주문은 거래소에
        # 살아 있으므로 반드시 디스크에 남기고 다음 세션에 정산해야 한다.
        self.pending_orders: List[Dict[str, Any]] = []
        # 그날 이미 주문을 냈는지 (미국 동부 날짜). 하루 한 번을 보장한다.
        self.loc_session: Optional[str] = None

    def _fetch_price(self) -> float:
        if self.broker == "namuh":
            acc = self.namuh_account or namuh.NamuhAccount()
            return acc.get_price(self.coin)
        return bithumb.get_price(self.coin)

    def _fetch_candles(self, limit: int = 200) -> List[Dict[str, Any]]:
        if self.broker == "namuh":
            acc = self.namuh_account or namuh.NamuhAccount()
            return acc.get_candles(self.coin, self.interval, limit=limit)
        return bithumb.get_candles(self.coin, self.interval, limit=limit)

    def _record_trade(self, action: str, price: float, units: float, amount_krw: float,
                      pnl: float = 0.0, return_pct: float = 0.0, reason: str = ""):
        """체결된 매매 기록을 보관한다."""
        with self._lock:
            trade_item = {
                "id": f"t-{int(time.time()*1000)}-{uuid.uuid4().hex[:4]}",
                "botId": self.bot_id,
                "coin": self.coin,
                "coinName": self.coin_name,
                "broker": self.broker,
                "currency": self.currency,
                "mode": self.mode,
                "action": action,
                "turn": self.pos.turn,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "price": round(price, 2 if self.currency == "USD" else 0),
                "units": round(units, 4 if self.currency == "USD" else 8),
                "amountKrw": round(amount_krw, 2 if self.currency == "USD" else 0),
                "pnlKrw": round(pnl, 2 if self.currency == "USD" else 0),
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
            self.last_price = self._fetch_price()
            self.last_price_at = time.time()
        except Exception as e:
            self.log("WARNING", f"시작 시점 시세 조회 실패: {e}")
        mode_label = "실전(LIVE)" if self.mode == "LIVE" else "모의투자(PAPER)"
        broker_label = "나무증권(미국주식)" if self.broker == "namuh" else "빗썸(원화마켓)"
        curr_lbl = "$" if self.currency == "USD" else "원"
        self.log("INFO", f"{mode_label} [{broker_label}] 봇 시작 · {self.coin_name}({self.coin}) · {self.interval} 캔들 · "
                         f"운용자본 {self.initial_krw:,.2f}{curr_lbl}")
        
        if self.params.strategyType == "raoer_infinite":
            v_title = "라오어 V4.0" if self.params.raoerVersion == "v4" else "라오어 V1.0"
            formula_desc = "잔금비례: 잔여현금 ÷ 잔여회차" if self.params.raoerVersion == "v4" else f"고정 1회 {self.initial_krw / self.params.splitCount:,.2f}{curr_lbl}"
            if self.params.raoerUseAi:
                self.log("INFO", f"🔄✨ [{v_title} AI 스마트 무한매수] {self.params.splitCount}분할 ({formula_desc} · AI 동적 0.5x~{self.params.raoerMaxMultiplier}x) | "
                                 f"가변 익절 +{self.params.raoerMinProfitPct}%~+{self.params.raoerMaxProfitPct}% · 리버스 쿼터방어 {self.params.quarterCutPct:.0f}%")
            else:
                self.log("INFO", f"🔄 [{v_title} 무한매수법] {self.params.splitCount}분할 매수 ({formula_desc}) · "
                                 f"목표 익절 +{self.params.targetProfitPct}% · 리버스 쿼터방어 {self.params.quarterCutPct:.0f}%")
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

    def can_liquidate(self) -> Tuple[bool, str]:
        """지금 청산 주문을 낼 수 있는 상태인지. (가능여부, 이유)"""
        if not self.pos.open or self.mode != "LIVE":
            return True, ""
        if self.broker == "namuh":
            if not (self.namuh_account and self.namuh_account.configured):
                return False, "나무증권 API 키가 등록되지 않았습니다."
            ses = namuh.market_session()
            if not ses.get("open"):
                return False, (f"미국 정규장이 닫혀 있어 매도할 수 없습니다 — {ses.get('reason')}")
            return True, ""
        if not (self.account and self.account.configured):
            return False, "빗썸 API 키가 등록되지 않았습니다."
        return True, ""

    def stop(self, liquidate: bool = True) -> bool:
        """정지. 청산까지 깔끔히 끝났으면 True.

        False 면 계좌에 물량이나 미체결 주문이 남아 있다는 뜻이다.
        호출부(삭제)는 이 값을 보고 지울지 말지 정해야 한다.
        """
        self.is_running = False
        if liquidate and self.pos.open:
            try:
                price = self._fetch_price()
                self._exit(price, "사용자 정지 명령 (시장가 청산)")
            except Exception as e:
                self.log("ERROR", f"청산 실패 — 포지션이 남아 있습니다: {e}")
        # 봇을 세워도 거래소의 LOC 는 살아 있다. 정지했는데 마감에 주식이
        # 생기는 일이 없도록 미체결 주문을 거둬들인다.
        if self.mode == "LIVE" and self.pending_orders:
            try:
                self._cancel_pending_loc("봇 정지")
            except Exception as e:
                self.log("ERROR", f"미체결 LOC 취소 실패 — 거래소에 주문이 남아 있습니다: {e}")
        if self.pending_orders:
            self.log("WARNING",
                     f"취소되지 않은 LOC {len(self.pending_orders)}건이 남아 있습니다. "
                     f"마감에 체결될 수 있으니 나무증권 앱에서 확인하세요.")
        self.log("WARNING", "봇이 정지되었습니다.")
        self._persist()
        # 팔려고 했는데 아직 들고 있거나, 거둬들이지 못한 주문이 남았는가.
        return not (liquidate and self.pos.open) and not self.pending_orders

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
                        candles = self._fetch_candles(limit=200)
                        bars = compute_indicators(candles, self.params)
                        bars_at = now
                    except Exception as e:
                        if not bars:
                            self.log("WARNING", f"캔들을 받지 못해 판단을 보류합니다: {e}")
                            time.sleep(poll)
                            continue
                        self.log("WARNING", f"캔들 갱신 실패, 직전 값 사용: {e}")

                try:
                    price = self._fetch_price()
                except Exception as e:
                    self.price_failures += 1
                    if self.price_failures in (1, 5, 20) or self.price_failures % 60 == 0:
                        self.log("WARNING", f"시세 수신 실패 {self.price_failures}회 — "
                                            f"추정치로 매매하지 않고 보류합니다: {e}")
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

                # ── 미국 주식 운영 시간 및 휴장일 스케줄러 점검 ──
                if self.broker == "namuh":
                    # LOC 정산은 장이 닫힌 뒤에 해야 한다. 휴장 게이트보다
                    # 먼저 돌지 않으면 영영 정산되지 않는다.
                    if self.mode == "LIVE" and self.pending_orders:
                        try:
                            self._settle_pending_loc()
                        except Exception as e:
                            logger.warning(f"[{self.bot_id}] LOC 정산 실패: {e}")
                    try:
                        from services.market_schedule import get_us_market_status
                        m_stat = get_us_market_status()
                        if not m_stat["isOpen"]:
                            status_desc = f"미국 증시 휴장 ({m_stat['statusText']} · 다음 개장: {m_stat.get('nextOpenKst', '-')})"
                            # 실전(LIVE) 모드에서는 장외 시간 주문 에러를 방지하기 위해 대기
                            if self.mode == "LIVE":
                                self.last_decision = status_desc
                                time.sleep(poll)
                                continue
                    except Exception as e:
                        logger.warning(f"[{self.bot_id}] 증시 스케줄 확인 실패: {e}")

                # 재시작 직후 중복 매수 방지 (lastBarTime 이 없던 옛 봇용).
                if self._adopt_bar_on_start:
                    self._adopt_bar_on_start = False
                    bt = bars[-1].get("time") if bars else None
                    if bt:
                        self._last_bar_time = bt
                        self.log("INFO", "재시작 시점의 봉을 이미 소비한 것으로 잡았습니다 "
                                         "(중복 매수 방지). 다음 봉부터 회차가 진행됩니다.")

                # ── 전략 판단 실행 ──
                if self.params.strategyType == "raoer_infinite":
                    cur_bar_time = bars[-1].get("time") if bars else None

                    # 매크로 국면 감지 적응형 변속 기어 점검 (나스닥 200일선 & VIX)
                    if (self.currency == "USD" or self.broker == "namuh") and self.params.useMacroGear:
                        try:
                            self.last_macro_regime = macro_regime.get_macro_regime()
                        except Exception as e:
                            logger.warning(f"[{self.bot_id}] 매크로 국면 조회 실패: {e}")

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
                    # 매크로 기어 목표 익절률 반영
                    if self.params.useMacroGear and self.last_macro_regime:
                        rec_tp = self.last_macro_regime.get("recommendedTargetProfitPct")
                        if rec_tp:
                            target_tp = float(rec_tp)

                    if self.params.raoerUseAi and self.last_ai_analysis and self.last_ai_analysis.get("success"):
                        target_tp = self.last_ai_analysis.get("dynamicTargetProfitPct", target_tp)

                    # 1) 목표 익절선 도달 시 즉시 전량 익절
                    if self.pos.open:
                        pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                        if pnl_pct >= target_tp:
                            ai_note = f" (가변목표 +{target_tp:.1f}%)" if (self.params.raoerUseAi or self.params.useMacroGear) else ""
                            self.last_decision = f"무한매수 목표 익절 (+{pnl_pct:.2f}% ≥ +{target_tp:.1f}%){ai_note}"
                            self._exit(price, self.last_decision)
                            # 이 봉은 소비했다고 기록한다. 안 그러면 다음 틱(수초 뒤)에
                            # cur_bar_time != _last_bar_time 이 여전히 참이라, **막 익절한
                            # 그 봉에서 새 회차를 곧바로 매수한다.** 방금 +10% 를 찍은
                            # 가격, 즉 그 봉의 고점에서 새 사이클을 시작하는 셈이다.
                            # 백테스트는 다음 봉을 기다리므로 실전과 백테스트가 어긋났다.
                            self._last_bar_time = cur_bar_time
                            time.sleep(poll)
                            continue

                    # 2) 매수 시점 판단.
                    #
                    # 원전 LOC 는 캔들이 아니라 **거래일** 단위다 (40분할 =
                    # 40거래일). 마감 20분 전 창에서 하루 한 번만 낸다.
                    # 그 외 모드는 종전대로 캔들 갱신마다 판단한다.
                    loc_native = (self.broker == "namuh"
                                  and self.params.locMode == "half_half")
                    if loc_native:
                        from services import market_schedule as _ms
                        _win = _ms.loc_window()
                        buy_now = bool(_win["in"] and self.loc_session != _win["sessionDate"])
                        if not buy_now and self.pos.open:
                            self.last_decision = (
                                f"원전 LOC — 오늘 접수 완료, 마감 체결 대기 "
                                f"(T={self.pos.turn}/{self.params.splitCount})"
                                if self.loc_session == _win["sessionDate"] else
                                f"원전 LOC 대기 — 미국장 개장 후 접수합니다 "
                                f"(T={self.pos.turn}/{self.params.splitCount})")
                    else:
                        buy_now = bool(cur_bar_time and cur_bar_time != self._last_bar_time)

                    if buy_now:
                        self._last_bar_time = cur_bar_time
                        if self.params.raoerVersion == "v4":
                            # V4.0 공식: 잔여 현금 / (N - T)
                            # 미국 주식(USD)인 경우 이전 회차의 미체결 잔돈(budget_carryover)을 제외한 미배정 순수 현금 기준으로 분할
                            rem_turns = max(1, self.params.splitCount - self.pos.turn)
                            if self.currency == "USD":
                                unalloc_cash = max(0.0, self.cash - self.budget_carryover)
                                base_chunk_krw = unalloc_cash / rem_turns
                            else:
                                base_chunk_krw = self.cash / rem_turns
                        else:
                            base_chunk_krw = self.initial_krw / self.params.splitCount
                        sizing_mult = 1.0
                        ai_reason = ""
                        if self.params.useMacroGear and self.last_macro_regime:
                            gear_mult = float(self.last_macro_regime.get("sizingMultiplier", 1.0))
                            sizing_mult *= gear_mult
                            gear_name = self.last_macro_regime.get("gearName", "")
                            ai_reason += f" [{gear_name} {gear_mult}x]"
                        if self.params.raoerUseAi and self.last_ai_analysis and self.last_ai_analysis.get("success"):
                            sizing_mult *= self.last_ai_analysis.get("sizingMultiplier", 1.0)
                            ai_reason += f" [AI {self.last_ai_analysis.get('sizingMultiplier')}x: {self.last_ai_analysis.get('reason', '')}]"

                        # 추세 조절. 판정 기준은 strategy.py 에 적어둔 대로 고정이고,
                        # 백테스트(backtest._trend_of)와 같은 정의를 쓴다.
                        #
                        # 실측(검증 구간 288조합 · 국면 112구간): boost_up 은 급등장에서
                        # +2.07%p 벌고 급락장에서 -2.39%p 잃는다. 낙폭도 급락장에서
                        # 14.78% → 17.74% 로 커진다. 공짜 개선이 아니라 국면에 건 방향
                        # 베팅이라, 기본값은 off 다.
                        trend = ""
                        if self.params.raoerTrendMode != "off":
                            trend = backtest._trend_of(bars, i, self.params)
                            if self.params.raoerTrendMode == "pause_down" and trend == "down":
                                self.last_decision = (
                                    f"하락추세라 {self.pos.turn + 1}회차 매수를 쉽니다 "
                                    f"(종가 {price:,.0f} < {self.params.slowMa}봉 평균, 평균선 하락){ai_reason}")
                                self._persist()
                                time.sleep(poll)
                                continue
                            if self.params.raoerTrendMode == "boost_up" and trend == "up":
                                sizing_mult *= self.params.raoerMaxMultiplier
                                ai_reason += f" [상승추세 {self.params.raoerMaxMultiplier}x]"

                        chunk_krw = base_chunk_krw * sizing_mult

                        # 1회 매수금 상한. V4 잔금비례가 후반에 눈덩이처럼 커지는
                        # 것을 막는다 (실측: 0.5x 지속 시 마지막 회차가 기본
                        # 분할금의 3.6배). 상한은 AI 배수 상한과 같은 값이다.
                        cap = self.params.raoer_chunk_cap(self.initial_krw)
                        cap_note = ""
                        if chunk_krw > cap:
                            cap_note = (f" [1회 상한 {cap:,.0f}원 적용 · "
                                        f"산출 {chunk_krw:,.0f}원]")
                            chunk_krw = cap

                        version_tag = " [V4]" if self.params.raoerVersion == "v4" else ""
                        if not self.pos.open:
                            self.last_decision = f"무한매수{version_tag} 1/{self.params.splitCount}회차 첫 매수{ai_reason}{cap_note}"
                            if loc_native:
                                self._place_loc_orders(price, chunk_krw, _win["sessionDate"],
                                                       self.last_decision)
                            else:
                                self._enter_chunk(price, chunk_krw, self.last_decision)
                        elif self.pos.turn < self.params.splitCount:
                            pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                            self.last_decision = f"무한매수{version_tag} {self.pos.turn + 1}/{self.params.splitCount}회차 매수 (평단 대비 {pnl_pct:+.2f}%){ai_reason}{cap_note}"
                            if loc_native:
                                self._place_loc_orders(price, chunk_krw, _win["sessionDate"],
                                                       self.last_decision)
                            else:
                                self._enter_chunk(price, chunk_krw, self.last_decision)
                        else:
                            pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                            rev_note = " (V4 리버스 모드: 쿼터 매도 후 롤백)" if self.params.raoerVersion == "v4" else ""
                            self.last_decision = f"무한매수 {self.params.splitCount}회 소진 쿼터매도 방어{rev_note} ({pnl_pct:+.2f}%)"
                            self._exit_quarter(price, self.last_decision)
                    else:
                        if self.pos.open:
                            pnl_pct = (price - self.pos.entryPrice) / self.pos.entryPrice * 100.0
                            ai_badge = f" · AI 목표 +{target_tp:.1f}%" if self.params.raoerUseAi else ""
                            ver_label = "V4 " if self.params.raoerVersion == "v4" else ""
                            self.last_decision = f"{ver_label}무한매수 진행 중 (T={self.pos.turn}/{self.params.splitCount}, 평단 {self.pos.entryPrice:,.0f}원, 손익 {pnl_pct:+.2f}%{ai_badge})"
                        else:
                            self.last_decision = "무한매수 다음 캔들 1회차 대기 중"

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
        min_invest = 10.0 if self.currency == "USD" else 5000.0
        if invest < min_invest:
            return
        fee = self.params.feePct / 100.0
        if self.currency == "USD":
            cost_per_unit = price * (1 + fee)
            units = float(int(invest // cost_per_unit))
            if units < 1:
                self.log("WARNING", f"자본 부족으로 1주 미만 매수 불가 (${invest:,.2f} < ${cost_per_unit:,.2f})")
                return
            invest = units * price
        else:
            units = invest * (1 - fee) / price

        if self.mode == "LIVE":
            if self.broker == "namuh":
                if not (self.namuh_account and self.namuh_account.configured):
                    self.log("WARNING", "나무증권 실주문 보류 — 나무증권 API 키가 등록되지 않았습니다.")
                    return
                try:
                    res = self.namuh_account.market_buy(self.coin, amount_usd=invest, units=units)
                    units = float(res.get("units") or units)
                    self.log("ORDER", f"나무증권 실주문 매수 접수 (주문번호 {res.get('orderId')})")
                except namuh.NamuhError as e:
                    self.log("ERROR", f"나무증권 실주문 매수 실패: {e.message}")
                    return
            else:
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
        self.cash = max(0.0, self.cash - (invest * (1 + fee) if self.currency == "USD" else invest))
        self.budget_carryover = 0.0
        self._record_trade("BUY", price, units, invest, pnl=0.0, return_pct=0.0, reason=reason)
        p_str = f"{price:,.2f}$" if self.currency == "USD" else f"{price:,.0f}원"
        inv_str = f"{invest:,.2f}$" if self.currency == "USD" else f"{invest:,.0f}원"
        self.log("BUY", f"매수 {units:.4f} {self.coin} @ {p_str} ({inv_str}) | 사유: {reason}")
        self._persist()

    # ── 라오어 원전 LOC (장마감 지정가) ──────────────────────────
    #
    # LOC 는 마감 동시호가에서만 체결된다. 그래서 '주문' 과 '체결' 이
    # 분리된다. 주문은 마감 20분 전에 내고, 체결은 그 세션이 끝난 뒤
    # 잔고 변화로 정산한다. 그 사이 봇이 꺼져도 주문은 거래소에 살아
    # 있으므로 pending_orders 를 디스크에 남긴다 (취소 API 가 없다).

    def _loc_targets(self, price: float, chunk_budget: float) -> List[Dict[str, Any]]:
        """이번 세션에 낼 LOC 주문들을 계산한다. 주문은 내지 않는다."""
        fee = self.params.feePct / 100.0
        cost_per_share = price * (1 + fee)
        total_budget = min(self.cash, chunk_budget) + self.budget_carryover
        if cost_per_share <= 0:
            return []

        if not self.pos.open:
            # 1회차는 기준 평단이 없다. 그날 종가에 사도록 상한을 넉넉히 둔다.
            qty = int(total_budget // cost_per_share)
            if qty < 1:
                return []
            return [{"leg": "1회차", "units": qty, "limit": round(price * 1.05, 2)}]

        avg = self.pos.entryPrice
        if self.pos.turn <= self.params.splitCount // 2:
            # 전반전: 예산 반씩 나눠 평단 / 평단+5% 두 다리
            half = total_budget / 2.0
            a = int(half // cost_per_share)
            b = int(half // cost_per_share)
            if a == 0 and b == 0 and total_budget >= cost_per_share:
                a = 1                       # 소액 자본 단주 방어
            out = []
            if a > 0:
                out.append({"leg": "평단", "units": a, "limit": round(avg, 2)})
            if b > 0:
                out.append({"leg": "평단+5%", "units": b, "limit": round(avg * 1.05, 2)})
            return out

        # 후반전: 전액 평단 한 다리
        qty = int(total_budget // cost_per_share)
        if qty < 1:
            return []
        return [{"leg": "평단", "units": qty, "limit": round(avg, 2)}]

    def _place_loc_orders(self, price: float, chunk_budget: float,
                          session: str, reason: str) -> None:
        """마감 전 LOC 접수. 장부는 건드리지 않는다 (체결 전이다)."""
        # 세션 소진 표시는 '판단이 끝났을 때' 만 찍는다.
        #
        # 예전에는 이 함수에 들어오자마자 찍었다. 그래서 잔고 조회가
        # HTTP 429 로 한 번 튕기자 그날 주문을 영영 못 냈다 — 접수 창이
        # 8분이나 남아 있었는데도. 통신 오류처럼 다시 해보면 되는 실패는
        # 세션을 소진하지 않고, 다음 틱에 재시도한다.
        targets = self._loc_targets(price, chunk_budget)

        if not targets:
            self.loc_session = session      # 살 돈이 없다 — 이건 판단이 끝난 것
            self.last_decision = (
                f"LOC 보류 — 가용 예산이 1주 값(${price:,.2f})에 못 미칩니다 "
                f"· 회차 유지 {self.pos.turn}/{self.params.splitCount}")
            self.budget_carryover = min(self.cash, chunk_budget) + self.budget_carryover
            self.log("INFO", self.last_decision)
            self._persist()
            return

        if self.mode != "LIVE":
            # 모의투자는 접수/정산을 흉내 내지 않는다. 종가를 알 수 없으니
            # 현재가로 즉시 체결시킨다 (화면 확인용).
            self._enter_chunk(price, chunk_budget, reason)
            return

        if not (self.namuh_account and self.namuh_account.configured):
            self.log("WARNING", "나무증권 실주문 보류 — API 키가 등록되지 않았습니다.")
            return

        try:
            bal = self.namuh_account.get_balance()
            qty_before = float(bal.get("qtyByTicker", {}).get(self.coin, 0.0))
            hold = (bal.get("holdings") or {}).get(self.coin) or {}
            avg_before = float(hold.get("avgPrice") or 0.0)
        except Exception as e:
            # 세션을 소진하지 않는다. 접수 창이 남아 있으면 다음 틱에 다시 한다.
            self.log("ERROR", f"LOC 접수 전 잔고 조회 실패 — 접수 창 안에서 다시 시도합니다: {e}")
            return

        placed = []
        for t in targets:
            try:
                res = self.namuh_account.market_buy(
                    self.coin, units=t["units"], order_type=namuh.ORD_LOC,
                    limit_price=t["limit"], await_fill=False)
                placed.append({
                    "orderId": res.get("orderId"), "leg": t["leg"],
                    "units": t["units"], "limit": t["limit"],
                    "session": session, "qtyBefore": qty_before,
                    "avgBefore": avg_before, "budget": chunk_budget,
                    "reason": reason,
                })
                self.log("ORDER", f"LOC 접수 {t['leg']} ${t['limit']:,.2f} × {t['units']}주 "
                                  f"(주문번호 {res.get('orderId')})")
            except namuh.NamuhError as e:
                self.log("ERROR", f"LOC 접수 실패 ({t['leg']}): {e.message}")

        if placed:
            self.pending_orders.extend(placed)
            self.loc_session = session      # 접수됐다 — 오늘은 여기까지
            legs = " + ".join(f"{p['leg']} {p['units']}주@${p['limit']:,.2f}" for p in placed)
            self.last_decision = f"LOC 접수 완료 ({legs}) · 마감 동시호가 체결 대기"
        else:
            # 한 다리도 못 냈다. 거래소가 거부한 것이므로 창이 남아 있으면
            # 다시 해본다 (일시적 오류일 수 있다).
            self.last_decision = "LOC 접수 실패 — 접수 창 안에서 다시 시도합니다"
        self._persist()

    def _cancel_pending_loc(self, why: str) -> None:
        """미체결 LOC 를 거둬들인다.

        이게 없으면 익절로 포지션을 닫은 뒤에도 매수 LOC 가 마감 동시호가에
        체결돼 포지션이 되살아난다. 봇은 다 팔았다고 알고 있는데 계좌에는
        주식이 생긴다 — 거래소 대조가 어긋나고, 아무도 그 주식을 관리하지
        않는다.

        거래소 접수 마감(15:50 ET) 뒤에는 취소가 거부된다. 그때는 취소가
        안 됐다는 사실을 남겨서, 다음 세션 정산이 그 체결을 주워 담게 한다.
        """
        if not self.pending_orders:
            return
        if not (self.namuh_account and self.namuh_account.configured):
            self.pending_orders = []
            return

        left = []
        for o in list(self.pending_orders):
            try:
                self.namuh_account.cancel_order(o.get("orderId"), self.coin)
                self.log("ORDER", f"미체결 LOC 취소 ({o.get('leg')} {o.get('units')}주 "
                                  f"@${o.get('limit')}) — {why}")
            except Exception as e:
                left.append(o)
                self.log("WARNING",
                         f"미체결 LOC 취소 실패 ({o.get('leg')}, 주문번호 {o.get('orderId')}): {e}. "
                         f"거래소 마감 뒤에는 취소되지 않습니다 — 다음 세션에 정산합니다.")
        self.pending_orders = left
        self._persist()

    def _settle_pending_loc(self) -> None:
        """지난 세션의 LOC 를 잔고 변화로 정산한다.

        주문 조회 API 가 없어서 체결 수량을 직접 물어볼 수 없다. 대신
        잔고가 수량과 매입단가를 같이 주므로 체결가를 역산할 수 있다.

            체결가 = (새수량×새평단 − 옛수량×옛평단) ÷ (새수량 − 옛수량)
        """
        if not self.pending_orders:
            return
        from services import market_schedule as ms
        now_session = ms.session_date()
        win = ms.loc_window()
        # 정산은 **장 마감 뒤**다. 접수 창이 닫힌 것(15:48)과 체결된 것
        # (16:00 동시호가)은 다르다. 접수 마감 기준으로 정산하면 12분 일찍
        # 잔고를 보고 '미체결' 로 지운 뒤, 마감에 들어온 물량을 놓친다.
        ripe = [o for o in self.pending_orders
                if o.get("session") != now_session or win.get("pastClose")]
        if not ripe:
            return
        if not (self.namuh_account and self.namuh_account.configured):
            return

        try:
            bal = self.namuh_account.get_balance()
        except Exception as e:
            self.log("WARNING", f"LOC 정산 보류 — 잔고 조회 실패: {e}")
            return

        qty_now = float(bal.get("qtyByTicker", {}).get(self.coin, 0.0))
        hold = (bal.get("holdings") or {}).get(self.coin) or {}
        avg_now = float(hold.get("avgPrice") or 0.0)

        qty_before = float(ripe[0].get("qtyBefore") or 0.0)
        avg_before = float(ripe[0].get("avgBefore") or 0.0)
        budget = float(ripe[0].get("budget") or 0.0)
        legs = " + ".join(f"{o['leg']} {o['units']}주@${o['limit']:,.2f}" for o in ripe)
        filled = qty_now - qty_before

        # 정산했으니 목록에서 뺀다. 실패해도 같은 주문을 두 번 반영하지 않는다.
        self.pending_orders = [o for o in self.pending_orders if o not in ripe]

        if filled < 1:
            self.budget_carryover = min(self.cash, budget) + self.budget_carryover
            self.last_decision = (
                f"LOC 미체결 (종가가 상한 위) · 회차 유지 "
                f"{self.pos.turn}/{self.params.splitCount}")
            self.log("INFO", f"[LOC 정산] {legs} 미체결 — 종가가 상한을 넘었습니다. "
                             f"회차를 소진하지 않고 ${self.budget_carryover:,.2f} 를 이월합니다.")
            self._persist()
            return

        if filled > 0 and qty_now > 0:
            fill_price = ((qty_now * avg_now) - (qty_before * avg_before)) / filled
        else:
            fill_price = self.last_price or self.pos.entryPrice
        if fill_price <= 0:
            fill_price = self.last_price or self.pos.entryPrice

        fee = self.params.feePct / 100.0
        invest = filled * fill_price
        spent = invest * (1 + fee)

        u0, p0 = self.pos.units, self.pos.entryPrice
        u_total = u0 + filled
        self.pos.units = u_total
        self.pos.entryPrice = ((u0 * p0) + invest) / u_total if u_total > 0 else fill_price
        self.pos.peakPrice = max(self.pos.peakPrice, fill_price)
        self.pos.turn += 1
        self.pos.totalInvested += invest
        self.budget_carryover = max(0.0, min(self.cash, budget) + self.budget_carryover - spent)
        self.cash = max(0.0, self.cash - spent)

        reason = ripe[0].get("reason") or "LOC 체결"
        self._record_trade("BUY_CHUNK", fill_price, filled, invest,
                           pnl=0.0, return_pct=0.0, reason=reason)
        self.log("BUY", f"[{self.pos.turn}/{self.params.splitCount}회차 LOC 체결] "
                        f"{legs} → {filled:.0f}주 @ ${fill_price:,.2f} (${invest:,.2f}) | "
                        f"평단 ${self.pos.entryPrice:,.2f} (총 {u_total:.0f}주) | 사유: {reason}")
        self.last_decision = (f"LOC 체결 {filled:.0f}주 @ ${fill_price:,.2f} · "
                              f"평단 ${self.pos.entryPrice:,.2f}")
        self._persist()

    def _skip_turn(self, price: float, total_budget: float, chased: bool,
                   where: str, limit_desc: str, note: str = "") -> None:
        """이번 봉에 못 샀을 때의 처리. **회차(T)는 올리지 않는다.**

        라오어에서 회차는 '몇 번 샀는가' 다. 평단과 분할 소진, 쿼터매도 발동
        시점이 전부 이 값에 걸려 있다. 예전에는 미체결에도 turn 을 올려서,
        평단 +5~12% 구간(익절선엔 못 미치고 상한선은 넘는 구간)에 머물면
        **한 주도 안 사고 40회를 다 태운 뒤 수익 중인 포지션을 쿼터매도**
        했다 (실측: 9회 시도 0주 매수, turn 10/10).

        이월금도 두 경우를 갈라야 한다.

        - chased=True (종가가 상한선 위) : 그냥 안 산 것이다. 배정액은
          현금에서 빠진 적이 없으니 다음 봉에 그대로 다시 배정된다.
          여기서 이월금에 더하면 안 산 돈을 눈덩이처럼 쌓아, 나중에 한 번에
          지르게 된다.
        - chased=False (1주 값에 못 미치는 잔돈) : 이건 진짜 이월이다.
          모아야 언젠가 1주가 된다.
        """
        if chased:
            head = f"{where} 미체결"
            detail = (note or (f"종가 ${price:,.2f} > {limit_desc} — 추격매수 방지"
                               if limit_desc else "미체결"))
            self.last_decision = (
                f"{head} ({detail}) · 회차 유지 {self.pos.turn}/{self.params.splitCount}")
            self.log("INFO", f"[{self.pos.turn}/{self.params.splitCount}회차 {head}] {detail} "
                             f"— 회차를 소진하지 않고 다음 봉에 다시 시도합니다.")
        else:
            self.budget_carryover = total_budget
            self.last_decision = (
                f"1주 미만 예산 이월 (${self.budget_carryover:,.2f} 누적, 주가 ${price:,.2f}) · "
                f"회차 유지 {self.pos.turn}/{self.params.splitCount}")
            self.log("INFO", f"[{self.pos.turn}/{self.params.splitCount}회차 {where} 이월] "
                             f"주가(${price:,.2f}) 대비 가용 예산(${total_budget:,.2f})이 1주 미만 "
                             f"— ${self.budget_carryover:,.2f} 를 다음 봉으로 이월합니다 (회차 유지).")
        self._persist()

    def _enter_chunk(self, price: float, invest_krw: float, reason: str):
        """라오어 무한매수 분할 매수 (원조 반반 매수 · 미국주식 정수 1주 · 잔돈 이월).

        라오어 원전은 LOC(장마감 지정가)를 쓰지만, 이 봇은 봉마다 현재가를
        보고 그 자리에서 판단한다. LOC 는 마감 동시호가에만 붙어서 6초
        체결 확인을 통과하지 못하고, 그 사이 주문은 거래소에 살아 있어
        봉마다 주문이 쌓인다. 그래서 **평단(또는 평단+5%)을 상한으로 건
        지정가**를 쓴다. 현재가가 이미 상한 아래일 때만 주문하므로 즉시
        체결되고, '상한 위로는 안 산다' 는 보장은 그대로다.
        """
        fee = self.params.feePct / 100.0

        if self.currency == "USD":
            cost_per_share = price * (1 + fee)
            # 배정액 + 이전 회차 잔돈 이월금
            allocated_budget = min(self.cash, invest_krw)
            total_budget = allocated_budget + self.budget_carryover

            # 반반 매수 모드 판정 (이미 1회차 이상 보유 중)
            if self.pos.open and self.params.locMode in ("half_half", "half_half_now"):
                avg_price = self.pos.entryPrice
                half_count = self.params.splitCount // 2

                if self.pos.turn <= half_count:
                    # ── [전반전 T <= N/2]: 0.5회 평단 상한 + 0.5회 평단*1.05 상한 ──
                    loc_a_price = avg_price
                    loc_b_price = round(avg_price * 1.05, 2)
                    half_budget = total_budget / 2.0

                    units_a = 0
                    units_b = 0
                    spent_a = 0.0
                    spent_b = 0.0

                    # A 주문 판정: 종가 <= 평단가
                    if price <= loc_a_price:
                        units_a = int(half_budget // cost_per_share)
                        spent_a = units_a * cost_per_share

                    # B 주문 판정: 종가 <= 평단가 * 1.05
                    if price <= loc_b_price:
                        units_b = int(half_budget // cost_per_share)
                        spent_b = units_b * cost_per_share

                    # 소액 자본 단주 방어: 50/50 분할로 각각은 0주이나 전체 예산으로는 1주 매수 가능한 경우
                    if units_a == 0 and units_b == 0 and total_budget >= cost_per_share:
                        if price <= loc_a_price:
                            units_a = 1
                            spent_a = cost_per_share
                        elif price <= loc_b_price:
                            units_b = 1
                            spent_b = cost_per_share

                    total_units_to_buy = units_a + units_b
                    actual_spent = spent_a + spent_b

                    if total_units_to_buy < 1:
                        self._skip_turn(
                            price, total_budget,
                            chased=(price > loc_b_price),
                            where="전반전 반반 지정가",
                            limit_desc=f"평단+5%(${loc_b_price:,.2f})")
                        return

                    fill_desc = []
                    if units_a > 0:
                        fill_desc.append(f"평단상한 {units_a}주")
                    if units_b > 0:
                        fill_desc.append(f"평단+5%상한 {units_b}주")
                    fill_summary = " + ".join(fill_desc)

                    if self.mode == "LIVE":
                        if not (self.namuh_account and self.namuh_account.configured):
                            self.log("WARNING", "나무증권 실주문 보류 — 나무증권 API 키가 등록되지 않았습니다.")
                            return
                        # 두 다리는 **상한이 다르다**. 한 건으로 합쳐 보내면
                        # 그 차이가 사라지고, 예전처럼 지정가를 안 실으면
                        # 시장가가 되어 평단 위로도 사버린다. 각자 낸다.
                        filled = 0
                        for _leg_name, _leg_units, _leg_limit in (
                                ("평단", units_a, loc_a_price),
                                ("평단+5%", units_b, loc_b_price)):
                            if _leg_units < 1:
                                continue
                            try:
                                res = self.namuh_account.market_buy(
                                    self.coin, amount_usd=_leg_units * cost_per_share,
                                    units=_leg_units, order_type=namuh.ORD_LIMIT,
                                    limit_price=_leg_limit)
                                _ru = res.get("units")
                                _got = int(_ru) if _ru is not None else _leg_units
                                filled += max(0, _got)
                                self.log("ORDER",
                                         f"나무증권 실주문 {_leg_name} 지정가 ${_leg_limit:,.2f} "
                                         f"{_got}주 체결 (주문번호 {res.get('orderId')})")
                            except namuh.NamuhError as e:
                                # 한 다리가 실패해도 다른 다리 체결분은 살린다.
                                self.log("ERROR",
                                         f"나무증권 실주문 {_leg_name} 다리 실패: {e.message}")
                        if filled < 1:
                            self._skip_turn(price, total_budget, chased=True,
                                            where="전반전 반반 지정가", limit_desc="",
                                            note="체결 수량 0주")
                            return
                        # 요청과 체결이 다르면 **체결분으로** 정산한다.
                        total_units_to_buy = filled
                        actual_spent = total_units_to_buy * cost_per_share

                    # 장부는 주문이 확정된 뒤에 움직인다. 이 두 줄이 주문보다
                    # 앞에 있으면, 주문이 거부돼 return 하는 순간 현금만 줄고
                    # 주식은 늘지 않는다 (실측: 한 번에 $294 증발).
                    self.budget_carryover = max(0.0, total_budget - actual_spent)
                    self.cash = max(0.0, self.cash - actual_spent)

                    new_units = float(total_units_to_buy)
                    invest = new_units * price

                else:
                    # ── [후반전 T > N/2]: 1.0회 전액 평단 상한 집중 ──
                    loc_price = avg_price
                    if price > loc_price:
                        self._skip_turn(price, total_budget, chased=True,
                                        where="후반전 평단 상한",
                                        limit_desc=f"평단(${loc_price:,.2f})")
                        return

                    units_to_buy = int(total_budget // cost_per_share)
                    if units_to_buy < 1:
                        self._skip_turn(price, total_budget, chased=False,
                                        where="후반전 평단 상한", limit_desc="")
                        return

                    new_units = float(units_to_buy)

                    if self.mode == "LIVE":
                        if not (self.namuh_account and self.namuh_account.configured):
                            self.log("WARNING", "나무증권 실주문 보류 — 나무증권 API 키가 등록되지 않았습니다.")
                            return
                        try:
                            res = self.namuh_account.market_buy(
                                self.coin, amount_usd=new_units * cost_per_share,
                                units=units_to_buy, order_type=namuh.ORD_LIMIT,
                                limit_price=loc_price)
                            _ru = res.get("units")
                            new_units = float(_ru) if _ru is not None else new_units
                            self.log("ORDER", f"나무증권 실주문 후반전 평단 지정가 ${loc_price:,.2f} 매수 접수 (주문번호 {res.get('orderId')})")
                        except namuh.NamuhError as e:
                            self.log("ERROR", f"나무증권 실주문 후반전 평단 지정가 매수 실패: {e.message}")
                            return
                        if new_units < 1:
                            self._skip_turn(price, total_budget, chased=True,
                                            where="후반전 평단 상한", limit_desc="",
                                            note="체결 수량 0주")
                            return

                    # 장부는 주문이 확정된 뒤에, 실제 체결 수량으로 움직인다.
                    spent_total = new_units * cost_per_share
                    invest = new_units * price
                    self.budget_carryover = max(0.0, total_budget - spent_total)
                    self.cash = max(0.0, self.cash - spent_total)

            else:
                # 1회차 첫 매수이거나 단일 매수(single) 모드
                units_to_buy = int(total_budget // cost_per_share)
                if units_to_buy < 1:
                    self._skip_turn(price, total_budget, chased=False,
                                    where="분할 매수", limit_desc="")
                    return

                new_units = float(units_to_buy)

                if self.mode == "LIVE":
                    if not (self.namuh_account and self.namuh_account.configured):
                        self.log("WARNING", "나무증권 실주문 보류 — 나무증권 API 키가 등록되지 않았습니다.")
                        return
                    try:
                        res = self.namuh_account.market_buy(self.coin, amount_usd=new_units * cost_per_share, units=units_to_buy)
                        _ru = res.get("units")
                        new_units = float(_ru) if _ru is not None else new_units
                        self.log("ORDER", f"나무증권 실주문 매수 접수 (주문번호 {res.get('orderId')})")
                    except namuh.NamuhError as e:
                        self.log("ERROR", f"나무증권 실주문 분할 매수 실패: {e.message}")
                        return
                    if new_units < 1:
                        self._skip_turn(price, total_budget, chased=True,
                                        where="분할 매수", limit_desc="",
                                        note="체결 수량 0주")
                        return

                # 장부는 주문이 확정된 뒤에, 실제 체결 수량으로 움직인다.
                spent_total = new_units * cost_per_share
                invest = new_units * price
                self.budget_carryover = max(0.0, total_budget - spent_total)
                self.cash = max(0.0, self.cash - spent_total)
        else:
            invest = min(self.cash, invest_krw)
            min_invest = 5000.0
            if invest < min_invest:
                self.log("WARNING", f"분할 매수 잔여 현금 부족 ({self.cash:,.0f}원 < {min_invest:,.0f}원). 매수 보류.")
                return
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

            self.cash = max(0.0, self.cash - invest)

        u0 = self.pos.units
        p0 = self.pos.entryPrice
        u_total = u0 + new_units
        p_avg = (u0 * p0 + new_units * price) / u_total if u_total > 0 else price

        self.pos.units = u_total
        self.pos.entryPrice = p_avg
        self.pos.peakPrice = max(self.pos.peakPrice, price)
        self.pos.turn += 1
        self.pos.totalInvested += invest

        self._record_trade("BUY_CHUNK", price, new_units, invest, pnl=0.0, return_pct=0.0, reason=reason)
        p_str = f"{price:,.2f}$" if self.currency == "USD" else f"{price:,.0f}원"
        inv_str = f"{invest:,.2f}$" if self.currency == "USD" else f"{invest:,.0f}원"
        avg_str = f"{p_avg:,.2f}$" if self.currency == "USD" else f"{p_avg:,.0f}원"
        carry_note = f" (잔돈 이월금 ${self.budget_carryover:,.2f})" if self.currency == "USD" and self.budget_carryover > 0 else ""
        self.log("BUY", f"[{self.pos.turn}/{self.params.splitCount}회차 분할매수] {new_units:.4f} {self.coin} @ {p_str} "
                        f"({inv_str}) | 평단가 {avg_str} (총 {u_total:.4f} {self.coin}){carry_note} | 사유: {reason}")
        self._persist()

    def _exit(self, price: float, reason: str):
        units = self.pos.units
        if units <= 0:
            return
        # 파는 순간, 살아 있는 매수 LOC 를 먼저 거둬들인다. 안 그러면
        # 마감 동시호가에 체결돼 방금 비운 포지션이 되살아난다.
        if self.mode == "LIVE" and self.pending_orders:
            self._cancel_pending_loc(f"청산: {reason}")
        fee = self.params.feePct / 100.0

        if self.mode == "LIVE":
            if self.broker == "namuh":
                if not (self.namuh_account and self.namuh_account.configured):
                    self.log("WARNING", "나무증권 실주문 보류 — 나무증권 API 키가 등록되지 않았습니다.")
                    return
                try:
                    res = self.namuh_account.market_sell(self.coin, units)
                    self.log("ORDER", f"나무증권 실주문 매도 접수 (주문번호 {res.get('orderId')})")
                except namuh.NamuhError as e:
                    self.log("ERROR", f"나무증권 실주문 매도 실패: {e.message}")
                    return
            else:
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
        self.budget_carryover = 0.0
        self.loc_session = None

        p_str = f"{price:,.2f}$" if self.currency == "USD" else f"{price:,.0f}원"
        pnl_str = f"{pnl:+,.2f}$" if self.currency == "USD" else f"{pnl:+,.0f}원"
        self.log("SELL", f"전량 매도 {units:.4f} {self.coin} @ {p_str} | "
                         f"손익 {pnl_str} ({pnl_pct:+.2f}%) | 사유: {reason}")
        self._persist()

    def _exit_quarter(self, price: float, reason: str):
        """라오어 무한매수 소진 시 25% 쿼터 매도 방어 (미국 주식은 정수 1주 단위)."""
        cut_ratio = self.params.quarterCutPct / 100.0
        if self.currency == "USD":
            units_to_sell = float(max(1, int(round(self.pos.units * cut_ratio))))
            if units_to_sell > self.pos.units:
                units_to_sell = self.pos.units
        else:
            units_to_sell = self.pos.units * cut_ratio

        if units_to_sell <= 0:
            return
        fee = self.params.feePct / 100.0

        if self.mode == "LIVE":
            if self.broker == "namuh":
                if not (self.namuh_account and self.namuh_account.configured):
                    self.log("WARNING", "나무증권 실주문 보류 — 나무증권 API 키가 등록되지 않았습니다.")
                    return
                try:
                    res = self.namuh_account.market_sell(self.coin, units_to_sell)
                    self.log("ORDER", f"나무증권 실주문 쿼터 매도 접수 (주문번호 {res.get('orderId')})")
                except namuh.NamuhError as e:
                    self.log("ERROR", f"나무증권 실주문 쿼터 매도 실패: {e.message}")
                    return
            else:
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
        ver_tag = "[V4 리버스 모드 방어] " if self.params.raoerVersion == "v4" else "[쿼터매도 방어] "
        p_str = f"{price:,.2f}$" if self.currency == "USD" else f"{price:,.0f}원"
        proc_str = f"{proceeds:,.2f}$" if self.currency == "USD" else f"{proceeds:,.0f}원"
        pnl_str = f"{pnl:+,.2f}$" if self.currency == "USD" else f"{pnl:+,.0f}원"
        self.log("SELL", f"{ver_tag}{units_to_sell:.4f} {self.coin} 매도 ({proc_str} 확보) | "
                         f"손익 {pnl_str} ({pnl_pct:+.2f}%) | 회차 조정: T={self.pos.turn} | 사유: {reason}")
        self._persist()

    def snapshot(self) -> Dict[str, Any]:
        """디스크에 저장할 최소 상태. 로그와 지표는 저장하지 않는다(재계산 가능)."""
        return {
            "botId": self.bot_id, "coin": self.coin, "interval": self.interval,
            "mode": self.mode, "initialKrw": self.initial_krw,
            "broker": self.broker, "market": self.market, "currency": self.currency,
            "params": self.params.to_dict(),
            "cash": self.cash,
            "budgetCarryover": round(self.budget_carryover, 2),
            "pendingOrders": list(self.pending_orders),
            "locSession": self.loc_session,
            "units": self.pos.units, "entryPrice": self.pos.entryPrice,
            "peakPrice": self.pos.peakPrice,
            "turn": self.pos.turn,
            "totalInvested": self.pos.totalInvested,
            "realizedPnl": self.realized_pnl,
            "totalTrades": self.total_trades, "winningTrades": self.winning_trades,
            "tradeHistory": self.trade_history,
            "createdAt": self.created_at, "wasRunning": self.is_running,
            # 마지막으로 회차를 소비한 봉. 이걸 저장하지 않으면 재시작할 때마다
            # '새 봉' 으로 보여 즉시 한 회차를 더 산다. 실측: 오늘 배포로 12번
            # 재시작했더니 6시간봉 봇이 8시간 만에 T1 → T19 까지 갔다.
            "lastBarTime": self._last_bar_time,
        }

    @classmethod
    def restore(cls, d: Dict[str, Any],
                account: Optional[bithumb.BithumbAccount],
                namuh_account: Optional[namuh.NamuhAccount] = None) -> "TradingBot":
        broker = d.get("broker", "namuh" if d.get("coin") in NAMUH_STOCKS else "bithumb")
        bot = cls(d["botId"], d["coin"], d["interval"], d["mode"],
                  float(d["initialKrw"]), StrategyParams.from_dict(d.get("params")),
                  account=account, namuh_account=namuh_account, broker=broker)
        bot.cash = float(d.get("cash", d["initialKrw"]))
        bot.budget_carryover = float(d.get("budgetCarryover", 0.0))
        # 취소 API 가 없어 봇이 꺼져도 주문은 거래소에 살아 있다.
        # 이 두 줄을 빼먹으면 재시작 뒤 같은 세션에 주문을 또 내고,
        # 이미 체결된 것을 영영 정산하지 못한다.
        bot.pending_orders = list(d.get("pendingOrders") or [])
        bot.loc_session = d.get("locSession")
        lbt = d.get("lastBarTime")
        bot._last_bar_time = int(lbt) if lbt else None
        # 이 값이 없던 시절에 저장된 봇: 이미 포지션을 들고 있다면 어느 봉에서
        # 샀는지 알 수 없다. 그 경우 첫 판단에서 현재 봉을 '이미 소비했다' 로
        # 잡아 중복 매수를 막는다. 포지션이 없으면 새로 시작해도 되므로 둔다.
        bot._adopt_bar_on_start = (lbt is None and float(d.get("units", 0.0)) > 0)
        bot.pos = Position(units=float(d.get("units", 0.0)),
                           entryPrice=float(d.get("entryPrice", 0.0)),
                           peakPrice=float(d.get("peakPrice", 0.0)),
                           turn=int(d.get("turn", 0)),
                           totalInvested=float(d.get("totalInvested", 0.0)),
                           )
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

        fx_rate = 1.0
        equity_krw_conv = equity
        cash_krw_conv = self.cash
        inv_krw_conv = self.pos.totalInvested
        unreal_krw_conv = unreal
        realized_krw_conv = self.realized_pnl

        if self.currency == "USD":
            try:
                from services.fx import get_official_fx
                fx_info = get_official_fx()
                fx_rate = float(fx_info.get("rate") or 1380.0)
            except Exception:
                fx_rate = 1380.0
            equity_krw_conv = round(equity * fx_rate, 0)
            cash_krw_conv = round(self.cash * fx_rate, 0)
            inv_krw_conv = round(self.pos.totalInvested * fx_rate, 0)
            unreal_krw_conv = round(unreal * fx_rate, 0)
            realized_krw_conv = round(self.realized_pnl * fx_rate, 0)

        return {
            "botId": self.bot_id,
            "coin": self.coin,
            "coinName": self.coin_name,
            "broker": self.broker,
            "market": self.market,
            "currency": self.currency,
            "currSymbol": self.curr_symbol,
            "interval": self.interval,
            "mode": self.mode,
            "strategyType": self.params.strategyType,
            "raoerVersion": self.params.raoerVersion,
            "turn": self.pos.turn,
            "splitCount": self.params.splitCount,
            "targetProfitPct": self.params.targetProfitPct,
            "isRunning": self.is_running,
            "createdAt": self.created_at,
            "initialKrw": round(self.initial_krw, 2 if self.currency == "USD" else 0),
            "equityKrw": round(equity, 2 if self.currency == "USD" else 0),
            "cashKrw": round(self.cash, 2 if self.currency == "USD" else 0),
            "budgetCarryover": round(self.budget_carryover, 2),
            "pendingLocOrders": len(self.pending_orders),
            "locSession": self.loc_session,
            "fxRate": round(fx_rate, 2),
            "equityKrwConverted": equity_krw_conv,
            "cashKrwConverted": cash_krw_conv,
            "investedKrwConverted": inv_krw_conv,
            "unrealizedPnlKrwConverted": unreal_krw_conv,
            "realizedPnlKrwConverted": realized_krw_conv,
            # 실제로 시장에 들어간 원금. 화면이 '무엇 대비 수익률인지' 를
            # 밝히려면 배정자본(initialKrw)과 이 값이 둘 다 필요하다.
            "investedKrw": round(self.pos.totalInvested, 2 if self.currency == "USD" else 0),
            "units": round(self.pos.units, 4 if self.currency == "USD" else 8),
            "entryPrice": round(self.pos.entryPrice, 2 if self.currency == "USD" else 0),
            "currentPrice": round(price, 2 if self.currency == "USD" else 0),
            "unrealizedPnlKrw": round(unreal, 2 if self.currency == "USD" else 0),
            "unrealizedPnlPct": round((price - self.pos.entryPrice) / self.pos.entryPrice * 100, 2)
                                if self.pos.open and self.pos.entryPrice else 0.0,
            "realizedPnlKrw": round(self.realized_pnl, 2 if self.currency == "USD" else 0),
            "totalReturnPct": round((equity - self.initial_krw) / self.initial_krw * 100, 2)
                              if self.initial_krw > 0 else 0.0,
            "totalTrades": self.total_trades,
            "winRatePct": round(self.winning_trades / self.total_trades * 100, 2)
                          if self.total_trades else 0.0,
            "rsi": round(self.last_rsi, 1) if self.last_rsi is not None else None,
            "priceAgeSec": round(time.time() - self.last_price_at, 1) if self.last_price_at else None,
            "pricePollSec": PRICE_POLL_SEC,
            "lastDecision": self.last_decision,
            "lastAiAnalysis": self.last_ai_analysis,
            "macroRegime": self.last_macro_regime,
            "priceFailures": self.price_failures,
            "params": self.params.to_dict(),
            "recentLogs": self.logs[:20],
        }


class BotManager:
    def __init__(self):
        self.bots: Dict[str, TradingBot] = {}
        self._lock = threading.Lock()
        # 상태 파일을 읽지 못해 복원을 포기했다면 그 사유. 화면에 계속 띄운다.
        self.restore_error: Optional[str] = None

    def active_count(self) -> int:
        return sum(1 for b in self.bots.values() if b.is_running)

    def _new_id(self, coin: str) -> str:
        with self._lock:
            while True:
                bid = f"{coin}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                if bid not in self.bots:
                    return bid

    def deploy(self, coin: str, interval: str, mode: str, capital_krw: float,
               params: Dict[str, Any], account: Optional[bithumb.BithumbAccount],
               namuh_account: Optional[namuh.NamuhAccount] = None,
               broker: str = "bithumb") -> TradingBot:
        if self.active_count() >= MAX_ACTIVE_BOTS:
            raise TooManyBots(f"동시 가동 봇 상한({MAX_ACTIVE_BOTS}개)에 도달했습니다. "
                              f"기존 봇을 정지한 뒤 다시 시도하세요.")
        p = StrategyParams.from_dict(params)
        bot = TradingBot(self._new_id(coin), coin, interval, mode, capital_krw, p,
                         account=account, namuh_account=namuh_account, broker=broker)
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

        # 지우기 전에 팔 수 있는 상태인지 먼저 본다. 여기서 막으면 돌고 있는
        # 봇을 건드리지 않고 그대로 둘 수 있다.
        ok, why = bot.can_liquidate()
        if not ok:
            raise LiquidationFailed(
                f"{bot.coin} 봇을 지우지 않았습니다 — {why} "
                f"지금 지우면 보유 {bot.pos.units} {bot.coin} 가 계좌에 주인 없이 남습니다. "
                f"장이 열린 뒤에 다시 시도하세요.")

        cleared = bot.stop(liquidate=True)
        if not cleared:
            # 여기까지 왔는데 못 팔았다면 주문이 거부된 것이다. 봇은 정지됐지만
            # 장부는 살아 있으므로, 지우지 않고 남겨 대조가 계속 맞게 한다.
            raise LiquidationFailed(
                f"{bot.coin} 봇을 지우지 않았습니다 — 청산 주문이 체결되지 않았습니다. "
                f"봇은 정지 상태로 남겨 두었습니다(보유 {bot.pos.units} {bot.coin}"
                + (f", 미체결 주문 {len(bot.pending_orders)}건" if bot.pending_orders else "")
                + "). 로그에서 실패 사유를 확인하고 다시 시도하세요.")

        del self.bots[bot_id]
        self.persist()
        return True

    def all_status(self) -> List[Dict[str, Any]]:
        return [b.status() for b in self.bots.values()]

    def all_trade_history(self) -> Dict[str, Any]:
        """전체 체결 일지와 누적 손익 정산.

        - 암호화폐(KRW)와 미국주식(USD)의 손익을 정확히 통화별로 분리 집계
        - 서울외환시장 공시환율을 적용하여 미국 주식 실현익을 원화로 환산 합산
        - 연간 250만 원 해외주식 양도소득세 비과세 트래커(소진율, 잔여한도, 예상세액) 제공
        """
        rows = tradelog.all_rows()

        fx_rate = 1380.0
        try:
            from services.fx import get_official_fx
            fx_info = get_official_fx()
            fx_rate = float(fx_info.get("rate") or 1380.0)
        except Exception:
            pass

        # 전체 및 통화별 집계
        crypto_pnl_krw = 0.0
        crypto_trades = 0
        crypto_winning = 0
        crypto_buy_krw = 0.0
        crypto_sell_krw = 0.0

        stocks_pnl_usd = 0.0
        stocks_trades = 0
        stocks_winning = 0
        stocks_buy_usd = 0.0
        stocks_sell_usd = 0.0

        current_year = datetime.now().year
        annual_stock_pnl_usd = 0.0

        by_coin: Dict[str, Dict[str, Any]] = {}

        for t in rows:
            act = t.get("action", "") or ""
            try:
                amt = float(t.get("amountKrw", 0.0) or 0.0)
                pnl = float(t.get("pnlKrw", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            coin = t.get("coin") or "?"
            currency = t.get("currency", "USD" if coin in NAMUH_STOCKS else "KRW")
            t_time = str(t.get("time", ""))

            c_name = t.get("coinName") or bithumb.COINS.get(coin, NAMUH_STOCKS.get(coin, {}).get("name", coin))
            stats = by_coin.setdefault(coin, {
                "coin": coin,
                "coinName": c_name,
                "currency": currency,
                "realizedPnl": 0.0,
                "realizedPnlKrw": 0.0,
                "realizedPnlKrwConverted": 0.0,
                "totalTrades": 0,
                "winningTrades": 0,
                "winRatePct": 0.0,
            })

            is_sell = "SELL" in act
            is_buy = "BUY" in act

            if currency == "USD":
                if is_sell:
                    stocks_sell_usd += amt
                    stocks_pnl_usd += pnl
                    stocks_trades += 1
                    stats["realizedPnl"] += pnl
                    stats["totalTrades"] += 1
                    if pnl > 0:
                        stocks_winning += 1
                        stats["winningTrades"] += 1
                    if t_time.startswith(str(current_year)):
                        annual_stock_pnl_usd += pnl
                elif is_buy:
                    stocks_buy_usd += amt
            else:
                if is_sell:
                    crypto_sell_krw += amt
                    crypto_pnl_krw += pnl
                    crypto_trades += 1
                    stats["realizedPnl"] += pnl
                    stats["totalTrades"] += 1
                    if pnl > 0:
                        crypto_winning += 1
                        stats["winningTrades"] += 1
                elif is_buy:
                    crypto_buy_krw += amt

        for stats in by_coin.values():
            tot = stats["totalTrades"]
            stats["winRatePct"] = round(stats["winningTrades"] / tot * 100, 2) if tot > 0 else 0.0
            if stats["currency"] == "USD":
                stats["realizedPnl"] = round(stats["realizedPnl"], 2)
                stats["realizedPnlKrw"] = stats["realizedPnl"]
                stats["realizedPnlKrwConverted"] = round(stats["realizedPnl"] * fx_rate, 0)
            else:
                stats["realizedPnl"] = round(stats["realizedPnl"], 0)
                stats["realizedPnlKrw"] = stats["realizedPnl"]
                stats["realizedPnlKrwConverted"] = stats["realizedPnl"]

        coin_summary = list(by_coin.values())
        coin_summary.sort(key=lambda x: x["realizedPnlKrwConverted"], reverse=True)

        rows.sort(key=lambda x: x.get("time", ""), reverse=True)

        annual_stock_pnl_krw = round(annual_stock_pnl_usd * fx_rate, 0)
        deduction_limit_krw = 2_500_000.0
        used_deduction_krw = max(0.0, min(annual_stock_pnl_krw, deduction_limit_krw))
        remaining_deduction_krw = max(0.0, deduction_limit_krw - annual_stock_pnl_krw)
        usage_pct = round((annual_stock_pnl_krw / deduction_limit_krw * 100.0), 1) if annual_stock_pnl_krw > 0 else 0.0
        taxable_krw = max(0.0, annual_stock_pnl_krw - deduction_limit_krw)
        estimated_tax_krw = round(taxable_krw * 0.22, 0)

        total_trades = crypto_trades + stocks_trades
        winning_trades = crypto_winning + stocks_winning
        total_pnl_krw_combined = round(crypto_pnl_krw + (stocks_pnl_usd * fx_rate), 0)
        win_rate = round(winning_trades / total_trades * 100, 2) if total_trades > 0 else 0.0

        return {
            "summary": {
                "totalRealizedPnlKrw": total_pnl_krw_combined,
                "totalTrades": total_trades,
                "winningTrades": winning_trades,
                "winRatePct": win_rate,
                "totalBuyKrw": round(crypto_buy_krw + (stocks_buy_usd * fx_rate), 0),
                "totalSellKrw": round(crypto_sell_krw + (stocks_sell_usd * fx_rate), 0),
                "crypto": {
                    "realizedPnlKrw": round(crypto_pnl_krw, 0),
                    "totalTrades": crypto_trades,
                    "winningTrades": crypto_winning,
                    "winRatePct": round(crypto_winning / crypto_trades * 100, 2) if crypto_trades > 0 else 0.0,
                    "totalBuyKrw": round(crypto_buy_krw, 0),
                    "totalSellKrw": round(crypto_sell_krw, 0),
                },
                "stocks": {
                    "realizedPnlUsd": round(stocks_pnl_usd, 2),
                    "realizedPnlKrwConverted": round(stocks_pnl_usd * fx_rate, 0),
                    "fxRate": round(fx_rate, 2),
                    "totalTrades": stocks_trades,
                    "winningTrades": stocks_winning,
                    "winRatePct": round(stocks_winning / stocks_trades * 100, 2) if stocks_trades > 0 else 0.0,
                    "totalBuyUsd": round(stocks_buy_usd, 2),
                    "totalSellUsd": round(stocks_sell_usd, 2),
                },
                "taxTracker": {
                    "year": current_year,
                    "fxRate": round(fx_rate, 2),
                    "annualStockPnlUsd": round(annual_stock_pnl_usd, 2),
                    "annualStockPnlKrw": annual_stock_pnl_krw,
                    "deductionLimitKrw": deduction_limit_krw,
                    "usedDeductionKrw": used_deduction_krw,
                    "remainingDeductionKrw": remaining_deduction_krw,
                    "usagePct": usage_pct,
                    "taxableKrw": taxable_krw,
                    "estimatedTaxKrw": estimated_tax_krw,
                },
                "byCoin": coin_summary,
            },
            "trades": rows[:500],
            "ledgerWarning": tradelog.warning(),
        }

    # ── 영속화 / 복원 ──

    def persist(self) -> None:
        botstore.save([b.snapshot() for b in self.bots.values()])

    def restore(self, account: Optional[bithumb.BithumbAccount],
                namuh_account: Optional[namuh.NamuhAccount] = None) -> Dict[str, Any]:
        """저장된 봇을 복원한다.

        LIVE 봇이 포지션을 들고 있었다면 거래소 실제 보유량과 대조한다.
        내부 장부가 거래소보다 많다고 주장하면(= 팔 수 없는 수량) 자동으로
        재가동하지 않는다. 그 상태로 매도를 걸면 주문이 거부되거나
        의도하지 않은 수량이 나가기 때문이다. 판단은 사용자에게 맡긴다.
        """
        # 봇이 하나도 없어도 장부는 올려둔다. 봇을 전부 지운 뒤에도
        # 매매 일지와 누적 손익은 계속 보여야 한다.
        # 일지를 못 읽어도 봇 복원은 계속한다 — 포지션 감시가 먼저다.
        # 파일 자체는 tradelog 의 빗장이 지켜 주고, 사유는 화면에 뜬다.
        try:
            tradelog.load()
        except jsonfile.StoreReadError as e:
            logger.error(f"체결 일지 복원 실패 (봇 복원은 계속합니다): {e}")

        # 여기서부터가 핵심이다. '봇이 0개' 와 '봇 목록을 못 읽었다' 는
        # 절대 같지 않다. 후자를 0 개로 취급하면 거래소에 포지션을 남긴 채
        # 감시 주체가 사라지고, 다음 저장이 그 기록마저 지운다.
        try:
            records = botstore.load()
        except jsonfile.StoreReadError as e:
            msg = (f"봇 상태 파일을 읽지 못해 복원을 중단했습니다 — {e} "
                   "봇을 하나도 가동하지 않았고, 이 파일에 다시 쓰지도 않습니다. "
                   "포지션이 남아 있다면 지금은 감시되지 않는 상태입니다. "
                   "원인(주로 data/ 권한)을 고친 뒤 서비스를 재시작하세요.")
            logger.error(msg)
            self.restore_error = msg
            return {"restored": 0, "resumed": 0, "held": 0,
                    "notes": [msg], "fatal": True}

        if not records:
            return {"restored": 0, "resumed": 0, "held": 0, "notes": []}

        # 빗썸 잔고 조회
        exchange: Dict[str, float] = {}
        balance_known = False
        balance_error = ""
        need_bithumb_check = any(r.get("broker", "bithumb") != "namuh" and
                                 float(r.get("units", 0)) > 0 and
                                 (r.get("mode") == "LIVE" or
                                  bithumb.normalize_coin(r.get("coin", "")) is None)
                                 for r in records)
        if need_bithumb_check:
            if not (account and account.configured):
                balance_error = "빗썸 API 키가 등록되지 않았습니다."
            else:
                try:
                    exchange = account.get_balance().get("coins", {})
                    balance_known = True
                except bithumb.BithumbError as e:
                    balance_error = e.message
                    logger.error(f"복원 중 빗썸 잔고 조회 실패: {e.message}")

        # 나무증권 잔고 조회
        namuh_exchange: Dict[str, float] = {}
        namuh_balance_known = False
        namuh_balance_error = ""
        need_namuh_check = any((r.get("broker") == "namuh" or r.get("coin") in NAMUH_STOCKS) and
                               float(r.get("units", 0)) > 0 and
                               r.get("mode") == "LIVE"
                               for r in records)
        if need_namuh_check:
            if not (namuh_account and namuh_account.configured):
                namuh_balance_error = "나무증권 API 키가 등록되지 않았습니다."
            else:
                try:
                    namuh_bal = namuh_account.get_balance()
                    # get_balance() 가 정하는 형식을 그대로 쓴다.
                    # 예전에는 dict 인 holdings 를 list 로 순회해 AttributeError
                    # 가 났고(키 이름도 symbol/quantity 로 달랐다), 거래소 대조가
                    # 통째로 동작하지 않았다. 형식은 namuh.get_balance 한 곳에서
                    # 정하고 여기서는 받아 쓰기만 한다.
                    qty_map = namuh_bal.get("qtyByTicker")
                    if qty_map is None:
                        qty_map = {t: (v or {}).get("qty", 0.0)
                                   for t, v in (namuh_bal.get("holdings") or {}).items()}
                    for tkr, qty in qty_map.items():
                        namuh_exchange[str(tkr).upper()] = float(qty or 0.0)
                    namuh_balance_known = True
                except Exception as e:
                    namuh_balance_error = str(e)
                    logger.error(f"복원 중 나무증권 잔고 조회 실패: {e}")

        notes: List[str] = []
        resumed = held = 0
        allocated_units: Dict[str, float] = {}

        for r in records:
            try:
                bot = TradingBot.restore(r, account, namuh_account)
            except Exception as e:
                logger.error(f"봇 복원 실패 {r.get('botId')}: {e}")
                continue
            self.bots[bot.bot_id] = bot

            if bot.broker == "namuh":
                if bot.coin not in NAMUH_STOCKS:
                    msg = f"{bot.coin} 는 지원하지 않는 해외주식 종목이라 재가동하지 않습니다."
                    bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                    bot.is_running = False
                    held += 1
                    continue
            else:
                if bithumb.normalize_coin(bot.coin) is None:
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
                if bot.broker == "namuh":
                    if not namuh_balance_known:
                        msg = (f"{bot.coin} 포지션 {bot.pos.units:.4f} 를 들고 있는데 "
                               f"나무증권 잔고를 조회하지 못해 대조할 수 없습니다. "
                               f"재가동을 보류합니다. (사유: {namuh_balance_error})")
                        bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                        held += 1
                        continue
                    actual = float(namuh_exchange.get(bot.coin, 0.0))
                    req_total = allocated_units.get(f"namuh:{bot.coin}", 0.0) + bot.pos.units
                    if actual + 1e-4 < req_total:
                        msg = (f"내부 장부 누적({req_total:.4f} {bot.coin})이 나무증권 실제 "
                               f"보유량({actual:.4f})보다 많습니다. 재가동을 보류합니다.")
                        bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                        held += 1
                        continue
                    allocated_units[f"namuh:{bot.coin}"] = req_total
                    bot.log("INFO", f"나무증권 대조 통과 (봇 장부 {bot.pos.units:.4f} / 계좌 잔고 {actual:.4f} {bot.coin})")
                else:
                    if not balance_known:
                        msg = (f"{bot.coin} 포지션 {bot.pos.units:.8f} 를 들고 있는데 "
                               f"빗썸 잔고를 조회하지 못해 대조할 수 없습니다. "
                               f"재가동을 보류합니다. (사유: {balance_error})")
                        bot.log("ERROR", msg); notes.append(f"[{bot.bot_id}] {msg}")
                        held += 1
                        continue
                    actual = float(exchange.get(bot.coin, 0.0))
                    req_total = allocated_units.get(bot.coin, 0.0) + bot.pos.units
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
                p_str = f"{bot.pos.entryPrice:,.2f}$" if bot.currency == "USD" else f"{bot.pos.entryPrice:,.0f}원"
                u_str = f"{bot.pos.units:.4f}" if bot.currency == "USD" else f"{bot.pos.units:.8f}"
                bot.log("WARNING",
                        f"포지션을 들고 재시작되었습니다 — 진입가 {p_str} · "
                        f"{u_str} {bot.coin}. 손절·익절 감시를 재개합니다.")
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
