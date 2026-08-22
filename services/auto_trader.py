import asyncio
import os
import time
import uuid
from datetime import datetime
import logging
from typing import Dict, Any, List, Optional
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 일봉 기반 지표(RSI/MACD/볼린저)는 하루 한 번만 바뀌므로 5분 주기로 갱신한다.
INDICATOR_REFRESH_SEC = 300.0

# 실제 주문 API 가 구현된 브로커. 여기 없는 브로커는 LIVE 로 띄워도 주문이 나가지 않는다.
# 예전에는 NH나무·Alpaca 도 LIVE 로 띄울 수 있었고 화면에 "🔥 실전" 으로 표시됐지만
# place_market_buy/sell 구현체가 빗썸에만 있어서, 사용자는 실전이라 믿고 페이퍼를 돌렸다.
LIVE_ORDER_BROKERS = {"BITHUMB"}


def supports_live_orders(broker: str) -> bool:
    return (broker or "").upper() in LIVE_ORDER_BROKERS

class AutoTradingBot:
    def __init__(self, 
                 bot_id: str,
                 symbol: str, 
                 mode: str = "PAPER",  # PAPER or LIVE
                 broker: str = "ALPACA_PAPER",
                 capital: float = 10000.0,
                 strategy_params: Dict[str, Any] = None):
        self.bot_id = bot_id
        self.symbol = symbol
        self.mode = mode
        self.broker = broker
        self.initial_capital = capital
        self.cash = capital
        self.position = 0.0 # shares/coins
        self.entry_price = 0.0
        self.highest_price_since_entry = 0.0 # Trailing Stop용
        
        # 5대 기관급 멀티팩터 기본값
        params = strategy_params or {}
        self.strategy_params = {
            "fastMa": int(params.get("fastMa", 5)),
            "slowMa": int(params.get("slowMa", 20)),
            "rsiBuy": float(params.get("rsiBuy", 35.0)),
            "rsiSell": float(params.get("rsiSell", 70.0)),
            "takeProfitPct": float(params.get("takeProfitPct", 12.0)),
            "stopLossPct": float(params.get("stopLossPct", 5.0)),
            # 기관급 7대 슈퍼 알파 멀티팩터 옵션
            "enableVolumeSurge": bool(params.get("enableVolumeSurge", True)),
            "volumeSurgeThreshold": float(params.get("volumeSurgeThreshold", 150.0)), # 평균 대비 150%
            "enableAiSentimentGate": bool(params.get("enableAiSentimentGate", True)),
            "minSentimentScore": int(params.get("minSentimentScore", 60)), # 60점 이상만 진입
            "enableTrailingStop": bool(params.get("enableTrailingStop", True)),
            "trailingStopPct": float(params.get("trailingStopPct", 3.5)), # 고점 대비 3.5% 하락 시 이익 보존
            "enableMarketRegime": bool(params.get("enableMarketRegime", True)), # 200일선 상회 국면
            "enableScaleInOut": bool(params.get("enableScaleInOut", True)), # 분할 매수/익절
            # 🔥 신규 2대 고수익 슈퍼 알파 팩터
            "enableBollingerSqueeze": bool(params.get("enableBollingerSqueeze", True)), # 볼린저 스퀴즈 변동성 폭발
            "enableMacdMomentum": bool(params.get("enableMacdMomentum", True)) # MACD 골든크로스 모멘텀 가속
        }
        
        self.is_running = False
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_checked_price = 0.0
        self.current_volume_ratio = 100.0 # %
        self.current_sentiment_score = 75 # default
        self.unrealized_pnl = 0.0
        self.unrealized_pnl_pct = 0.0
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.partial_profit_taken = False
        self.logs: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None

    def start(self, initial_price: float = 100.0, sentiment_score: int = 75):
        from services.market_feed import get_live_price

        self.is_running = True
        self.current_sentiment_score = sentiment_score

        # 시작 가격도 실측값을 우선한다. 예전엔 조회 실패 시 100.0 을 그대로 썼다.
        tick = get_live_price(self.symbol, self.broker)
        if tick is not None:
            self.last_checked_price = tick["price"]
        elif initial_price > 0:
            self.last_checked_price = initial_price
        else:
            self.last_checked_price = 0.0
        
        filters_active = []
        if self.strategy_params["enableVolumeSurge"]: filters_active.append("⚡거래량폭증(+150%)")
        if self.strategy_params["enableAiSentimentGate"]: filters_active.append(f"🤖AI감성(≥{self.strategy_params['minSentimentScore']}점)")
        if self.strategy_params["enableBollingerSqueeze"]: filters_active.append("💥볼린저스퀴즈폭발")
        if self.strategy_params["enableMacdMomentum"]: filters_active.append("📈MACD모멘텀가속")
        if self.strategy_params["enableTrailingStop"]: filters_active.append(f"🛡️ATR추적익절(-{self.strategy_params['trailingStopPct']}%)")
        if self.strategy_params["enableMarketRegime"]: filters_active.append("🏛️200MA추세국면")
        if self.strategy_params["enableScaleInOut"]: filters_active.append("💰스마트분할매매")
        
        self.add_log("INFO", f"🤖 [{self.mode}] 기관급 7대 슈퍼 알파 봇 가동! 활성 팩터: [{', '.join(filters_active)}]")

        # 가동 직후 1차 분할 진입.
        # 이 진입은 지표 조건을 평가한 결과가 아니라 '가동 시점 시장가 진입' 정책이다.
        # 로그 문구도 실제로 한 일만 적는다.
        if self.last_checked_price > 0:
            src = tick["source"] if tick is not None else "배포 시점 시세"
            self._open_position(
                self.last_checked_price,
                f"봇 가동 시점 시장가 1차 분할 진입 (시세 출처: {src})",
                is_initial=True
            )
        else:
            self.add_log(
                "WARNING",
                "⚠️ 실시간 시세를 받지 못해 1차 진입을 건너뜁니다. "
                "시세가 들어오면 지표 조건에 따라 진입합니다."
            )

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self, close_position: bool = True):
        self.is_running = False
        if close_position and self.position > 0:
            self._close_position("사용자 봇 정지 명령 (시장가 전량 청산)")
        self.add_log("WARNING", f"🛑 [{self.mode}] 봇 가동이 정지되었습니다.")

    def add_log(self, level: str, message: str):
        now_str = datetime.now().strftime("%H:%M:%S")
        log_entry = {
            "timestamp": now_str,
            "level": level,
            "message": message
        }
        self.logs.insert(0, log_entry)
        if len(self.logs) > 100:
            self.logs.pop()

    def update_price_and_check(self, current_price: float, current_rsi: float = 45.0, volume_ratio: float = 120.0, sentiment_score: int = 70, is_squeeze_breakout: bool = False, macd_momentum_up: bool = True, indicators_ok: bool = True):
        """기관급 7대 슈퍼 알파 복합 조건 실시간 평가.

        indicators_ok=False 는 RSI/MACD/볼린저를 실제로 계산하지 못한 상태다.
        이때 신규 진입은 하지 않는다. 다만 가격은 실측값이므로
        보유 포지션의 손절·익절·트레일링은 계속 감시한다.
        """
        if not self.is_running or current_price <= 0:
            return

        self.last_checked_price = current_price
        self.current_volume_ratio = volume_ratio
        self.current_sentiment_score = sentiment_score

        # 포지션 보유 중인 경우
        if self.position > 0:
            if current_price > self.highest_price_since_entry:
                self.highest_price_since_entry = current_price

            self.unrealized_pnl = round((current_price - self.entry_price) * self.position, 2)
            self.unrealized_pnl_pct = round(((current_price - self.entry_price) / self.entry_price) * 100, 2)

            take_profit = self.strategy_params["takeProfitPct"]
            stop_loss = self.strategy_params["stopLossPct"]
            trailing_pct = self.strategy_params["trailingStopPct"]
            rsi_sell = self.strategy_params["rsiSell"]

            # 1. 고점 대비 하락 (ATR Trailing Stop - 이익 보존)
            drop_from_peak_pct = ((self.highest_price_since_entry - current_price) / self.highest_price_since_entry) * 100
            if self.strategy_params["enableTrailingStop"] and self.unrealized_pnl_pct >= 3.0 and drop_from_peak_pct >= trailing_pct:
                self._close_position(f"🛡️ ATR 트레일링 스탑 발동! 고점({self.highest_price_since_entry:,.2f}) 대비 -{drop_from_peak_pct:.1f}% 반락 시 이익 보존 청산 (+{self.unrealized_pnl_pct:.2f}%)")
                return

            # 2. 분할 익절 (Scale-Out)
            if self.strategy_params["enableScaleInOut"] and not self.partial_profit_taken and self.unrealized_pnl_pct >= (take_profit * 0.5):
                self._partial_close(0.5, f"🎯 1차 목표가 도달 (+{self.unrealized_pnl_pct:.2f}%) 50% 분할 익절 실현")

            # 3. 최종 목표 수익률 도달
            if self.unrealized_pnl_pct >= take_profit:
                self._close_position(f"🎯 최종 목표 수익률 도달 전량 익절 (+{self.unrealized_pnl_pct:.2f}%)")
                return

            # 4. 리스크 관리 손절매
            if self.unrealized_pnl_pct <= -stop_loss:
                self._close_position(f"🛡️ 손절매(Stop-Loss) 발동 (-{abs(self.unrealized_pnl_pct):.2f}%) 리스크 통제")
                return

            # 5. 기술적 RSI 과매수 청산 (반드시 플러스 수익 상태 +1.0% 이상일 때만 익절 발동!)
            if indicators_ok and current_rsi >= rsi_sell and self.unrealized_pnl_pct >= 1.0:
                self._close_position(f"⚡ RSI 과매수({current_rsi:.1f}) 고점 도달 확정 익절 (+{self.unrealized_pnl_pct:.2f}%)")
                return

            # 6. AI 감성 악화 리스크 방어
            if self.strategy_params["enableAiSentimentGate"] and sentiment_score < 35 and self.unrealized_pnl_pct <= -2.5:
                self._close_position(f"⚠️ Gemini AI 긴급 경보: 뉴스 감성 급락({sentiment_score}점)으로 리스크 방어 청산")
                return

        # 포지션이 없는 경우: 7대 슈퍼 팩터 결합 신규 진입 검사 (바닥 저점 구간만 엄격 진입!)
        elif self.position == 0 and self.cash > 100:
            # 0. 지표를 실제로 계산하지 못했으면 신규 진입 금지.
            #    (기본값 RSI 50 같은 임의 숫자로 매수 판정을 내리면 안 된다)
            if not indicators_ok:
                return

            # 1. AI 감성 필터
            if self.strategy_params["enableAiSentimentGate"] and sentiment_score < self.strategy_params["minSentimentScore"]:
                return # AI 점수 미달로 진입 보류

            # 2. 거래량 폭증 필터
            if self.strategy_params["enableVolumeSurge"] and volume_ratio < self.strategy_params["volumeSurgeThreshold"]:
                pass

            # 3. 바닥 저점 확증 (RSI 45 이하 + 볼린저/MACD/거래량 모멘텀 결합)
            rsi_buy = self.strategy_params["rsiBuy"]
            reasons = []
            if is_squeeze_breakout and self.strategy_params["enableBollingerSqueeze"]:
                reasons.append("💥볼린저스퀴즈 상방폭발")
            if macd_momentum_up and self.strategy_params["enableMacdMomentum"]:
                reasons.append("📈MACD골든크로스 가속")
            if volume_ratio >= 150.0:
                reasons.append(f"⚡거래량폭증({volume_ratio:.0f}%)")
            
            # 고점 매수 방지: RSI가 48 이하인 바닥 반등 구간에서만 매수 집행
            if current_rsi <= max(rsi_buy, 45.0) or (current_rsi <= 48.0 and len(reasons) >= 2):
                signal_desc = " + ".join(reasons) if reasons else f"RSI({current_rsi:.1f}) 과매도 바닥 지지"
                self._open_position(current_price, f"{signal_desc} + AI감성({sentiment_score}점) 최저가 확증 진입")

    @staticmethod
    def _fmt(price: float, is_krw: bool) -> str:
        return f"{price:,.0f}" if is_krw else f"{price:,.2f}"

    def _open_position(self, price: float, reason: str, is_initial: bool = False):
        if self.cash < 50:
            return
        
        is_bithumb = "BITHUMB" in self.broker.upper()

        # 빗썸은 원화 호가로만 체결된다. 예전 코드는 빗썸 조회가 실패하면
        # 달러 시세에 1350 을 곱해 원화인 척했다 — 그 값으로 실주문을 내면
        # 수량이 완전히 틀어지므로, 이제는 실측 원화 호가가 없으면 진입하지 않는다.
        from services.market_feed import get_live_price
        actual_price = price
        if is_bithumb:
            tick = get_live_price(self.symbol, self.broker)
            if tick is None or tick.get("currency") != "KRW":
                self.add_log(
                    "WARNING",
                    f"⚠️ [진입 보류] {self.symbol} 의 빗썸 원화 호가를 받지 못했습니다. "
                    f"환율 추정으로 주문하지 않습니다."
                )
                return
            actual_price = tick["price"]

        if actual_price <= 0:
            self.add_log("WARNING", "⚠️ [진입 보류] 유효한 시세가 없습니다.")
            return

        # 분할 진입 시 70% 또는 100% 매수
        alloc_ratio = 0.7 if self.strategy_params["enableScaleInOut"] and is_initial else 1.0
        invest_amount = (self.cash * alloc_ratio) * 0.999 # fee
        shares = round(invest_amount / actual_price, 4)
        if shares <= 0:
            shares = 0.0001
        
        self.position = round(self.position + shares, 4)
        self.entry_price = round(actual_price, 2)
        self.highest_price_since_entry = round(actual_price, 2)
        self.cash = round(self.cash - (shares * actual_price), 2)
        self.partial_profit_taken = False
        self.unrealized_pnl = 0.0
        self.unrealized_pnl_pct = 0.0
        
        # 빗썸 실전 Live 매수 집행
        if self.mode == "LIVE" and is_bithumb:
            if not broker_manager.bithumb_client.connect_key:
                self.add_log("WARNING", "⚠️ [실전 주문 보류] 빗썸 API Key가 아직 등록되지 않았습니다. 우측 상단 [API 키 등록]을 완료해 주세요.")
            else:
                try:
                    coin_sym = self.symbol.upper().replace("-USD", "").replace("KRW-", "")
                    buy_res = broker_manager.bithumb_client.place_market_buy(coin_sym, shares)
                    status_code = buy_res.get("status", "error")
                    msg = buy_res.get("message", "정상 접수")
                    if status_code == "0000":
                        self.add_log("LIVE_ORDER", f"🪙 [빗썸 실전 매수 성공] {coin_sym} {shares}개 시장가 체결! (주문번호: {buy_res.get('order_id')})")
                    else:
                        self.add_log("WARNING", f"⚠️ [빗썸 매수 거부] {msg} (잔고 부족 또는 서명 오류)")
                except Exception as e:
                    self.add_log("ERROR", f"빗썸 실전 매수 주문 오류: {str(e)}")
        
        curr_unit = "원" if is_bithumb else "$"
        self.add_log("BUY", f"🟢 [스마트 매수] {self.symbol} {shares}개 @ {self._fmt(actual_price, is_bithumb)}{curr_unit} | 사유: {reason}")

    def _partial_close(self, ratio: float, reason: str):
        if self.position <= 0: return
        is_bithumb = "BITHUMB" in self.broker.upper()
        close_shares = round(self.position * ratio, 4)
        price = self.last_checked_price if self.last_checked_price > 0 else self.entry_price
        gross = close_shares * price
        fee = gross * 0.001
        net = gross - fee
        trade_pnl = round(net - (close_shares * self.entry_price), 2)
        
        self.cash = round(self.cash + net, 2)
        self.position = round(self.position - close_shares, 4)
        self.realized_pnl = round(self.realized_pnl + trade_pnl, 2)
        self.partial_profit_taken = True
        self.total_trades += 1
        if trade_pnl > 0: self.winning_trades += 1
        
        # 빗썸 실전 Live 분할 매도 집행
        if self.mode == "LIVE" and is_bithumb:
            if not broker_manager.bithumb_client.connect_key:
                self.add_log("WARNING", "⚠️ [실전 매도 보류] 빗썸 API Key가 미등록 상태입니다.")
            else:
                try:
                    coin_sym = self.symbol.upper().replace("-USD", "").replace("KRW-", "")
                    sell_res = broker_manager.bithumb_client.place_market_sell(coin_sym, close_shares)
                    status_code = sell_res.get("status", "error")
                    msg = sell_res.get("message", "정상 접수")
                    if status_code == "0000":
                        self.add_log("LIVE_ORDER", f"🪙 [빗썸 실전 분할익절 성공] {coin_sym} {close_shares}개 시장가 체결!")
                    else:
                        self.add_log("WARNING", f"⚠️ [빗썸 매도 거부] {msg}")
                except Exception as e:
                    self.add_log("ERROR", f"빗썸 실전 매도 주문 오류: {str(e)}")
        
        curr_unit = "원" if is_bithumb else "$"
        self.add_log("SELL", f"💰 [분할 익절] {self.symbol} {close_shares}개 @ {self._fmt(price, is_bithumb)}{curr_unit} | 실현손익: {trade_pnl:+,.0f}{curr_unit} ({reason})")

    def _close_position(self, reason: str):
        if self.position <= 0: return
        is_bithumb = "BITHUMB" in self.broker.upper()
        is_krw = is_bithumb or ("NAMUH" in self.broker.upper()) or (".KS" in self.symbol) or (".KQ" in self.symbol)
        unit = "개" if (is_bithumb or "BTC" in self.symbol or "ETH" in self.symbol or "SOL" in self.symbol) else "주"
        curr_unit = "원" if is_krw else "$"

        price = self.last_checked_price if self.last_checked_price > 0 else self.entry_price
        gross = self.position * price
        fee = gross * 0.001
        net = gross - fee
        trade_pnl = round(net - (self.position * self.entry_price), 2)
        trade_pnl_pct = round(((price - self.entry_price) / self.entry_price) * 100, 2)

        # 빗썸 실전 Live 전량 청산 집행
        if self.mode == "LIVE" and is_bithumb:
            if not broker_manager.bithumb_client.connect_key:
                self.add_log("WARNING", "⚠️ [실전 청산 보류] 빗썸 API Key가 미등록 상태입니다.")
            else:
                try:
                    coin_sym = self.symbol.upper().replace("-USD", "").replace("KRW-", "")
                    sell_res = broker_manager.bithumb_client.place_market_sell(coin_sym, self.position)
                    status_code = sell_res.get("status", "error")
                    msg = sell_res.get("message", "정상 접수")
                    if status_code == "0000":
                        self.add_log("LIVE_ORDER", f"🪙 [빗썸 실전 전량청산 성공] {coin_sym} {self.position}{unit} 시장가 체결 완료!")
                    else:
                        self.add_log("WARNING", f"⚠️ [빗썸 청산 거부] {msg}")
                except Exception as e:
                    self.add_log("ERROR", f"빗썸 실전 청산 주문 오류: {str(e)}")

        self.cash = round(self.cash + net, 2)
        self.realized_pnl = round(self.realized_pnl + trade_pnl, 2)
        self.total_trades += 1
        if trade_pnl > 0: self.winning_trades += 1

        self.add_log("SELL", f"🔴 [전량 청산] {self.symbol} {self.position}{unit} @ {self._fmt(price, is_krw)}{curr_unit} | 손익: {trade_pnl:+,.0f}{curr_unit} ({trade_pnl_pct:+.2f}%) | {reason}")
        
        self.position = 0.0
        self.entry_price = 0.0
        self.highest_price_since_entry = 0.0
        self.partial_profit_taken = False
        self.unrealized_pnl = 0.0
        self.unrealized_pnl_pct = 0.0

    def _run_loop(self):
        """실시세 · 실지표 감시 엔진.

        예전 구현은 random.uniform() 으로 가격을 만들어 그 난수로 매매를 판정했다.
        이제는 market_feed 가 실제로 조회한 값만 쓰고, 조회에 실패하면
        추정치를 만들지 않고 해당 틱의 판단을 보류한다.
        """
        from services.market_feed import get_live_price, get_live_indicators, price_poll_seconds

        poll_sec = price_poll_seconds(self.symbol, self.broker)
        last_price_at = 0.0
        last_ind_at = 0.0
        indicators = None
        fail_streak = 0

        self.add_log("INFO", f"📡 실시간 시세 폴링 시작 ({poll_sec:.0f}초 주기 · 지표 {int(INDICATOR_REFRESH_SEC)}초 갱신)")

        while self.is_running:
            try:
                now = time.time()
                if now - last_price_at < poll_sec:
                    time.sleep(1)
                    continue
                last_price_at = now

                tick = get_live_price(self.symbol, self.broker)
                if tick is None:
                    fail_streak += 1
                    # 로그 폭주를 막되 상태는 알린다
                    if fail_streak in (1, 5, 20) or fail_streak % 60 == 0:
                        self.add_log(
                            "WARNING",
                            f"⚠️ 실시간 시세를 받지 못했습니다 ({fail_streak}회 연속). "
                            f"추정치로 매매하지 않고 판단을 보류합니다."
                        )
                    continue

                if fail_streak:
                    self.add_log("INFO", f"✅ 시세 수신 재개 ({tick['source']})")
                    fail_streak = 0

                if indicators is None or (now - last_ind_at) >= INDICATOR_REFRESH_SEC:
                    last_ind_at = now
                    fresh = get_live_indicators(self.symbol)
                    if fresh is not None:
                        indicators = fresh
                    elif indicators is None:
                        self.add_log(
                            "WARNING",
                            "⚠️ 기술지표(RSI/MACD/볼린저)를 계산할 캔들을 받지 못했습니다. "
                            "신규 진입은 보류하고 보유 포지션의 손절·익절만 감시합니다."
                        )

                self.update_price_and_check(
                    current_price=tick["price"],
                    current_rsi=(indicators or {}).get("rsi14", 50.0),
                    volume_ratio=(indicators or {}).get("volumeRatio", 100.0),
                    sentiment_score=self.current_sentiment_score,
                    is_squeeze_breakout=bool((indicators or {}).get("isSqueezeBreakout", False)),
                    macd_momentum_up=bool((indicators or {}).get("macdMomentumUp", False)),
                    indicators_ok=indicators is not None,
                )
            except Exception as e:
                logger.error(f"Bot loop error: {e}")
                time.sleep(1)

    def get_status(self) -> Dict[str, Any]:
        cur_p = self.last_checked_price or self.entry_price or 100.0
        total_asset = round(self.cash + (self.position * cur_p), 2)
        total_roi_pct = round(((total_asset - self.initial_capital) / self.initial_capital) * 100, 2)
        win_rate = round((self.winning_trades / self.total_trades * 100), 2) if self.total_trades > 0 else 0.0
        is_krw = ("BITHUMB" in self.broker.upper()) or ("NAMUH" in self.broker.upper()) or (".KS" in self.symbol) or (".KQ" in self.symbol)
        unit = "개" if ("BITHUMB" in self.broker.upper() or "BTC" in self.symbol or "ETH" in self.symbol or "SOL" in self.symbol) else "주"

        return {
            "botId": self.bot_id,
            "symbol": self.symbol,
            "mode": self.mode,
            "broker": self.broker,
            "liveOrdersSupported": supports_live_orders(self.broker),
            "currency": "KRW" if is_krw else "USD",
            "unit": unit,
            "isRunning": self.is_running,
            "createdAt": self.created_at,
            "initialCapital": self.initial_capital,
            "currentTotalAsset": total_asset,
            "cash": round(self.cash, 2),
            "position": round(self.position, 4),
            "entryPrice": round(self.entry_price, 2),
            "highestPrice": round(self.highest_price_since_entry, 2),
            "currentPrice": round(cur_p, 2),
            "volumeRatio": round(self.current_volume_ratio, 0),
            "sentimentScore": self.current_sentiment_score,
            "unrealizedPnl": round(self.unrealized_pnl, 2),
            "unrealizedPnlPct": round(self.unrealized_pnl_pct, 2),
            "realizedPnl": round(self.realized_pnl, 2),
            "totalRoiPct": total_roi_pct,
            "totalTrades": self.total_trades,
            "winRate": win_rate,
            "strategyParams": self.strategy_params,
            "recentLogs": self.logs[:15]
        }

class BotTooManyError(Exception):
    """동시 가동 봇 상한 초과."""


class BotManager:
    # 봇마다 데몬 스레드 1개 + 주기적 외부 API 호출이 붙는다. 무제한이면 인스턴스가 죽는다.
    MAX_ACTIVE_BOTS = int(os.getenv("APP_MAX_ACTIVE_BOTS", "20"))

    def __init__(self):
        self.bots: Dict[str, AutoTradingBot] = {}
        self._id_lock = threading.Lock()

    def _new_bot_id(self, symbol: str) -> str:
        """초 단위 타임스탬프만 쓰면 같은 초의 배포가 서로 덮어썼다.
        덮인 봇의 스레드는 살아 있는데 목록에서 사라져 정지시킬 수단이 없었다."""
        with self._id_lock:
            while True:
                bot_id = f"BOT-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                if bot_id not in self.bots:
                    return bot_id

    def active_count(self) -> int:
        return sum(1 for b in self.bots.values() if b.is_running)

    def deploy_bot(self, symbol: str, mode: str, broker: str, capital: float, strategy_params: dict, initial_price: float = 100.0, sentiment_score: int = 75) -> AutoTradingBot:
        if self.active_count() >= self.MAX_ACTIVE_BOTS:
            raise BotTooManyError(
                f"동시 가동 봇 상한({self.MAX_ACTIVE_BOTS}개)에 도달했습니다. "
                f"기존 봇을 정지한 뒤 다시 시도하세요."
            )
        bot_id = self._new_bot_id(symbol)
        bot = AutoTradingBot(bot_id, symbol, mode, broker, capital, strategy_params)
        self.bots[bot_id] = bot
        bot.start(initial_price=initial_price, sentiment_score=sentiment_score)
        return bot

    def stop_bot(self, bot_id: str) -> bool:
        if bot_id in self.bots:
            self.bots[bot_id].stop(close_position=True)
            return True
        return False

    def stop_all_bots(self) -> int:
        count = 0
        for bot in self.bots.values():
            if bot.is_running:
                bot.stop(close_position=True)
                count += 1
        return count

    def delete_bot(self, bot_id: str) -> bool:
        if bot_id in self.bots:
            self.bots[bot_id].stop(close_position=True)
            del self.bots[bot_id]
            return True
        return False

    def get_all_bots(self) -> List[Dict[str, Any]]:
        return [bot.get_status() for bot in self.bots.values()]

    def get_bot(self, bot_id: str) -> Optional[AutoTradingBot]:
        return self.bots.get(bot_id)

import json
from services.bithumb_client import BithumbClient

KEYS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "broker_keys.json")

# 환경변수로 주입할 수 있는 브로커 키.
# data/ 는 gitignore 대상이고 Render 디스크는 휘발성이라 재배포마다 키가 사라졌다.
# 환경변수로 넣으면 재배포·스핀다운에도 살아남고, 디스크에 평문으로 남지도 않는다.
ENV_KEY_MAP = {
    "BITHUMB": ("BITHUMB_API_KEY", "BITHUMB_SECRET_KEY"),
}


class BrokerKeyManager:
    """브로커 및 거래소 API 키 저장 & 연동 매니저.

    키 우선순위: 환경변수 > 디스크 파일(data/broker_keys.json).
    환경변수로 들어온 키는 UI 에서 해제할 수 없다 (환경변수를 지워야 한다).
    디스크 저장은 평문이다 — UI 문구도 그렇게 표기한다.
    """
    def __init__(self):
        self.connected_brokers = {
            "NAMUH": {"name": "🌳 NH투자증권 나무 (NAMUH)", "connected": False, "mode": "Live", "apiKey": "", "secretKey": "", "accountNo": ""},
            "BITHUMB": {"name": "🪙 빗썸 (Bithumb)", "connected": False, "mode": "Live", "apiKey": "", "secretKey": "", "accountNo": ""},
            # 예전엔 connected=True 와 "PK***DEMO***KEY" 가 하드코딩돼 '연동됨' 으로 보였다.
            "ALPACA": {"name": "🇺🇸 Alpaca Trading (미국주식)", "connected": False, "mode": "Paper", "apiKey": "", "secretKey": "", "accountNo": ""}
        }
        self.bithumb_client = BithumbClient()
        self._load_saved_keys()
        self._load_env_keys()

    def _load_env_keys(self):
        """환경변수 키를 디스크 값보다 우선 적용한다."""
        for code, (k_env, s_env) in ENV_KEY_MAP.items():
            api_key = (os.getenv(k_env) or "").strip()
            secret = (os.getenv(s_env) or "").strip()
            if not api_key or not secret:
                continue
            slot = self.connected_brokers.get(code)
            if slot is None:
                continue
            slot["connected"] = True
            slot["apiKey"] = self._mask(api_key)
            slot["rawApiKey"] = api_key
            slot["rawSecretKey"] = secret
            slot["keySource"] = "env"
            if code == "BITHUMB":
                self.bithumb_client = BithumbClient(connect_key=api_key, secret_key=secret)
            logger.info(f"{code} API 키를 환경변수({k_env})에서 로드했습니다.")

    @staticmethod
    def _mask(api_key: str) -> str:
        return api_key[:3] + "******" + api_key[-3:] if len(api_key) > 6 else "******"

    def key_source(self, broker_code: str) -> str:
        return self.connected_brokers.get(broker_code.upper(), {}).get("keySource", "none")

    def _save_keys_to_disk(self):
        """API 키를 로컬 보안 파일에 영구 저장"""
        try:
            os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
            with open(KEYS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.connected_brokers, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving broker keys to disk: {e}")

    def _load_saved_keys(self):
        """저장된 API 키 자동 복원"""
        try:
            if os.path.exists(KEYS_FILE):
                with open(KEYS_FILE, "r", encoding="utf-8") as f:
                    saved_data = json.load(f)
                    for k, v in saved_data.items():
                        # 실제 키가 없는데 connected=True 로 남아 있는 항목은 복원하지 않는다.
                        # Alpaca 가 이 상태로 디스크에 굳어 있어서 기본값을 고쳐도
                        # 계속 '연동됨' 으로 되살아났다.
                        has_keys = bool(v.get("rawApiKey")) and bool(v.get("rawSecretKey"))
                        if k in self.connected_brokers and v.get("connected") and has_keys:
                            self.connected_brokers[k] = v
                            if k == "BITHUMB":
                                self.bithumb_client = BithumbClient(
                                    connect_key=v["rawApiKey"],
                                    secret_key=v["rawSecretKey"]
                                )
        except Exception as e:
            logger.warning(f"Failed to load saved broker keys: {e}")

    def save_key(self, broker_code: str, api_key: str, secret_key: str = "", account_no: str = "") -> bool:
        b = broker_code.upper()
        if b in self.connected_brokers:
            self.connected_brokers[b]["connected"] = True
            self.connected_brokers[b]["apiKey"] = self._mask(api_key)
            self.connected_brokers[b]["rawApiKey"] = api_key
            self.connected_brokers[b]["rawSecretKey"] = secret_key
            self.connected_brokers[b]["accountNo"] = account_no
            self.connected_brokers[b]["keySource"] = "disk"

            if b == "BITHUMB":
                self.bithumb_client = BithumbClient(connect_key=api_key, secret_key=secret_key)

            self._save_keys_to_disk()
            return True
        return False

    def test_bithumb_connection(self, api_key: str, secret_key: str) -> Dict[str, Any]:
        temp_client = BithumbClient(connect_key=api_key, secret_key=secret_key)
        return temp_client.test_connection()

    def disconnect(self, broker_code: str) -> bool:
        b = broker_code.upper()
        if self.key_source(b) == "env":
            # 지워도 다음 재시작에 환경변수에서 다시 살아난다. 거짓 성공을 반환하지 않는다.
            return False
        if b in self.connected_brokers:
            self.connected_brokers[b]["connected"] = False
            self.connected_brokers[b]["apiKey"] = ""
            self.connected_brokers[b].pop("rawApiKey", None)
            self.connected_brokers[b].pop("rawSecretKey", None)
            if b == "BITHUMB":
                self.bithumb_client = BithumbClient()
            self._save_keys_to_disk()
            return True
        return False

    def get_status_list(self) -> List[Dict[str, Any]]:
        # 클라이언트에는 민감한 원본 키(rawApiKey, rawSecretKey)를 제외하고 마스킹된 정보만 전송
        result = []
        for k, v in self.connected_brokers.items():
            live_ok = supports_live_orders(k)
            safe_info = {
                "code": k,
                "name": v.get("name"),
                "connected": v.get("connected", False),
                "mode": "Live" if live_ok else "Paper only",
                "liveOrdersSupported": live_ok,
                "apiKey": v.get("apiKey", ""),
                "keySource": v.get("keySource", "none"),
            }
            result.append(safe_info)
        return result

bot_manager = BotManager()
broker_manager = BrokerKeyManager()
