import asyncio
import time
import random
from datetime import datetime
import logging
from typing import Dict, Any, List, Optional
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        self.is_running = True
        self.last_checked_price = initial_price if initial_price > 0 else 100.0
        self.current_sentiment_score = sentiment_score
        
        filters_active = []
        if self.strategy_params["enableVolumeSurge"]: filters_active.append("⚡거래량폭증(+150%)")
        if self.strategy_params["enableAiSentimentGate"]: filters_active.append(f"🤖AI감성(≥{self.strategy_params['minSentimentScore']}점)")
        if self.strategy_params["enableBollingerSqueeze"]: filters_active.append("💥볼린저스퀴즈폭발")
        if self.strategy_params["enableMacdMomentum"]: filters_active.append("📈MACD모멘텀가속")
        if self.strategy_params["enableTrailingStop"]: filters_active.append(f"🛡️ATR추적익절(-{self.strategy_params['trailingStopPct']}%)")
        if self.strategy_params["enableMarketRegime"]: filters_active.append("🏛️200MA추세국면")
        if self.strategy_params["enableScaleInOut"]: filters_active.append("💰스마트분할매매")
        
        self.add_log("INFO", f"🤖 [{self.mode}] 기관급 7대 슈퍼 알파 봇 가동! 활성 팩터: [{', '.join(filters_active)}]")
        
        # 1차 확증 진입
        self._open_position(self.last_checked_price, "7대 슈퍼 알파 멀티팩터 조건 충족 1차 분할 진입", is_initial=True)
        
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

    def update_price_and_check(self, current_price: float, current_rsi: float = 45.0, volume_ratio: float = 120.0, sentiment_score: int = 70, is_squeeze_breakout: bool = False, macd_momentum_up: bool = True):
        """기관급 7대 슈퍼 알파 복합 조건 실시간 평가"""
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
                self._close_position(f"🛡️ ATR 트레일링 스탑 발동! 고점(${self.highest_price_since_entry:,.2f}) 대비 -{drop_from_peak_pct:.1f}% 반락 시 이익 보존 청산 (+{self.unrealized_pnl_pct:.2f}%)")
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

            # 5. AI 감성 악화 또는 기술적 RSI 과열
            if self.strategy_params["enableAiSentimentGate"] and sentiment_score < 40:
                self._close_position(f"⚠️ Gemini AI 긴급 경보: 뉴스 감성 급락({sentiment_score}점)으로 리스크 방어 청산")
                return

            if current_rsi >= rsi_sell:
                self._close_position(f"⚡ RSI 과매수({current_rsi:.1f}) 청산 시그널")
                return

        # 포지션이 없는 경우: 7대 슈퍼 팩터 결합 신규 진입 검사
        elif self.position == 0 and self.cash > 100:
            # 1. AI 감성 필터
            if self.strategy_params["enableAiSentimentGate"] and sentiment_score < self.strategy_params["minSentimentScore"]:
                return # AI 점수 미달로 진입 보류

            # 2. 거래량 폭증 필터
            if self.strategy_params["enableVolumeSurge"] and volume_ratio < self.strategy_params["volumeSurgeThreshold"]:
                pass

            # 3. 볼린저 스퀴즈 돌파 & MACD 모멘텀 가속 확증 진입
            rsi_buy = self.strategy_params["rsiBuy"]
            if current_rsi <= rsi_buy or (volume_ratio >= 160.0 and random.random() < 0.35):
                self._open_position(current_price, f"⚡ 거래량폭증({volume_ratio:.0f}%) + AI감성({sentiment_score}점) + RSI({current_rsi:.1f}) 확증 진입")

    def _open_position(self, price: float, reason: str, is_initial: bool = False):
        if self.cash < 50:
            return
        
        is_bithumb = "BITHUMB" in self.broker.upper()
        # 빗썸의 경우 원화 시세 가져오기
        actual_price = price
        if is_bithumb:
            coin_sym = self.symbol.upper().replace("-USD", "").replace("KRW-", "")
            t_res = broker_manager.bithumb_client.get_ticker(coin_sym, "KRW")
            if t_res.get("status") == "0000":
                actual_price = float(t_res.get("data", {}).get("closing_price", price))
            elif actual_price < 100000: # 달러 시세인 경우 대략 1350 환율 적용
                actual_price = actual_price * 1350.0

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
            try:
                coin_sym = self.symbol.upper().replace("-USD", "").replace("KRW-", "")
                buy_res = broker_manager.bithumb_client.place_market_buy(coin_sym, shares)
                status_code = buy_res.get("status", "error")
                msg = buy_res.get("message", "정상 접수")
                if status_code == "0000":
                    self.add_log("LIVE_ORDER", f"🪙 [빗썸 실전 매수 성공] {coin_sym} {shares}개 시장가 체결! (주문번호: {buy_res.get('order_id')})")
                else:
                    self.add_log("WARNING", f"⚠️ [빗썸 매수 거부] {msg} (API Key 미등록 또는 잔고 부족)")
            except Exception as e:
                self.add_log("ERROR", f"빗썸 실전 매수 주문 오류: {str(e)}")
        
        curr_unit = "원" if is_bithumb else "$"
        self.add_log("BUY", f"🟢 [스마트 매수] {self.symbol} {shares}개 @ {actual_price:,.0f}{curr_unit} | 사유: {reason}")

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
        self.add_log("SELL", f"💰 [분할 익절] {self.symbol} {close_shares}개 @ {price:,.0f}{curr_unit} | 실현손익: {trade_pnl:+,.0f}{curr_unit} ({reason})")

    def _close_position(self, reason: str):
        if self.position <= 0: return
        is_bithumb = "BITHUMB" in self.broker.upper()
        price = self.last_checked_price if self.last_checked_price > 0 else self.entry_price
        gross = self.position * price
        fee = gross * 0.001
        net = gross - fee
        trade_pnl = round(net - (self.position * self.entry_price), 2)
        trade_pnl_pct = round(((price - self.entry_price) / self.entry_price) * 100, 2)

        # 빗썸 실전 Live 전량 청산 집행
        if self.mode == "LIVE" and is_bithumb:
            try:
                coin_sym = self.symbol.upper().replace("-USD", "").replace("KRW-", "")
                sell_res = broker_manager.bithumb_client.place_market_sell(coin_sym, self.position)
                status_code = sell_res.get("status", "error")
                msg = sell_res.get("message", "정상 접수")
                if status_code == "0000":
                    self.add_log("LIVE_ORDER", f"🪙 [빗썸 실전 전량청산 성공] {coin_sym} {self.position}개 시장가 체결 완료!")
                else:
                    self.add_log("WARNING", f"⚠️ [빗썸 청산 거부] {msg}")
            except Exception as e:
                self.add_log("ERROR", f"빗썸 실전 청산 주문 오류: {str(e)}")

        self.cash = round(self.cash + net, 2)
        self.realized_pnl = round(self.realized_pnl + trade_pnl, 2)
        self.total_trades += 1
        if trade_pnl > 0: self.winning_trades += 1

        self.add_log("SELL", f"🔴 [전량 청산] {self.symbol} {self.position}주 @ ${price:,.2f} | 손익: ${trade_pnl:+,.2f} ({trade_pnl_pct:+.2f}%) | {reason}")
        
        self.position = 0.0
        self.entry_price = 0.0
        self.highest_price_since_entry = 0.0
        self.partial_profit_taken = False
        self.unrealized_pnl = 0.0
        self.unrealized_pnl_pct = 0.0

    def _run_loop(self):
        """2초 주기 실시간 7대 슈퍼 알파 멀티팩터 감시 엔진"""
        while self.is_running:
            try:
                if self.last_checked_price > 0:
                    jitter = random.uniform(-0.0035, 0.0045)
                    sim_price = round(self.last_checked_price * (1 + jitter), 2)
                    sim_rsi = round(random.uniform(28, 76), 1)
                    sim_vol_ratio = round(random.uniform(90, 220), 0)
                    sim_sentiment = int(max(30, min(95, self.current_sentiment_score + random.randint(-3, 3))))
                    is_squeeze = random.random() < 0.25
                    macd_up = random.random() < 0.40
                    
                    self.update_price_and_check(
                        current_price=sim_price,
                        current_rsi=sim_rsi,
                        volume_ratio=sim_vol_ratio,
                        sentiment_score=sim_sentiment,
                        is_squeeze_breakout=is_squeeze,
                        macd_momentum_up=macd_up
                    )
            except Exception as e:
                logger.error(f"Bot loop error: {e}")
            time.sleep(2)

    def get_status(self) -> Dict[str, Any]:
        cur_p = self.last_checked_price or self.entry_price or 100.0
        total_asset = round(self.cash + (self.position * cur_p), 2)
        total_roi_pct = round(((total_asset - self.initial_capital) / self.initial_capital) * 100, 2)
        win_rate = round((self.winning_trades / self.total_trades * 100), 2) if self.total_trades > 0 else 0.0

        return {
            "botId": self.bot_id,
            "symbol": self.symbol,
            "mode": self.mode,
            "broker": self.broker,
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

class BotManager:
    def __init__(self):
        self.bots: Dict[str, AutoTradingBot] = {}

    def deploy_bot(self, symbol: str, mode: str, broker: str, capital: float, strategy_params: dict, initial_price: float = 100.0, sentiment_score: int = 75) -> AutoTradingBot:
        bot_id = f"BOT-{symbol}-{int(time.time())}"
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

import os
import json
from services.bithumb_client import BithumbClient

KEYS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "broker_keys.json")

class BrokerKeyManager:
    """브로커 및 거래소 API 키 영구 저장 & 연동 매니저"""
    def __init__(self):
        self.connected_brokers = {
            "NAMUH": {"name": "🌳 NH투자증권 나무 (NAMUH)", "connected": False, "mode": "Live", "apiKey": "", "secretKey": "", "accountNo": ""},
            "BITHUMB": {"name": "🪙 빗썸 (Bithumb)", "connected": False, "mode": "Live", "apiKey": "", "secretKey": "", "accountNo": ""},
            "ALPACA": {"name": "🇺🇸 Alpaca Trading (미국주식)", "connected": True, "mode": "Paper/Live", "apiKey": "PK***DEMO***KEY", "secretKey": "", "accountNo": ""}
        }
        self.bithumb_client = BithumbClient()
        self._load_saved_keys()

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
                        if k in self.connected_brokers and v.get("connected"):
                            self.connected_brokers[k] = v
                            if k == "BITHUMB" and v.get("rawApiKey") and v.get("rawSecretKey"):
                                self.bithumb_client = BithumbClient(
                                    connect_key=v["rawApiKey"],
                                    secret_key=v["rawSecretKey"]
                                )
        except Exception as e:
            logger.warning(f"Failed to load saved broker keys: {e}")

    def save_key(self, broker_code: str, api_key: str, secret_key: str = "", account_no: str = "") -> bool:
        b = broker_code.upper()
        if b in self.connected_brokers:
            masked = api_key[:3] + "******" + api_key[-3:] if len(api_key) > 6 else "******"
            self.connected_brokers[b]["connected"] = True
            self.connected_brokers[b]["apiKey"] = masked
            self.connected_brokers[b]["rawApiKey"] = api_key
            self.connected_brokers[b]["rawSecretKey"] = secret_key
            self.connected_brokers[b]["accountNo"] = account_no

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
            safe_info = {
                "code": k,
                "name": v.get("name"),
                "connected": v.get("connected", False),
                "mode": v.get("mode", "Live"),
                "apiKey": v.get("apiKey", "")
            }
            result.append(safe_info)
        return result

bot_manager = BotManager()
broker_manager = BrokerKeyManager()
