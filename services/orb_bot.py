"""국내주식 ORB 봇 — 판단(services/orb.py)을 받아 주문을 내고 장부를 든다.

무한매수 봇(TradingBot)과 규칙이 전혀 달라 따로 둔다. 그 클래스에는 이미
'빗썸이냐 나무냐 · 원화냐 달러냐' 분기가 54군데 있고, 국내주식은 '원화인데
정수 주식' 이라 어느 갈래에도 맞지 않는다. 억지로 끼우면 소수점 수량이나
24시간 시장 가정 같은 버그가 조용히 생긴다.

BotManager 가 다루는 겉모습(start · stop · can_liquidate · status · snapshot ·
restore · trade_history · pos.units · pending_orders)만 같게 맞췄다.

주문
  매수  최우선 매도호가 + 2호가 지정가 (시장성 지정가 — 모의가 지정가만 받는다)
  매도  최우선 매수호가 - 2호가 지정가
  체결  접수 뒤 잔고 변화로 확인한다. 매수 평단은 잔고 평단으로 역산한다.
  재시도 주문은 다시 보내지 않는다. 실패하면 20초 쉬고 다음 판단에서 새로 본다.
"""

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services import krx, orb, tradelog
from services.namuh import NamuhAccount, NamuhError

logger = logging.getLogger(__name__)

RETRY_SEC = 20.0


class _Pos:
    def __init__(self, units: int = 0, entryPrice: float = 0.0, invested: float = 0.0, date: str = "",
                 peak: float = 0.0):
        self.units, self.entryPrice, self.invested, self.date = int(units), float(entryPrice), float(invested), date
        self.peak = float(peak or entryPrice)          # 트레일링 스탑 기준 고점
        self.turn = 1 if units else 0
        self.totalInvested = self.invested

    @property
    def open(self) -> bool:
        return self.units > 0


class _ParamView:
    """다른 코드가 bot.params.xxx 로 읽는 몇 칸만 흉내 낸다."""

    def __init__(self, p: orb.OrbParams):
        self._p = p
        self.strategyType = "orb"
        self.splitCount = 1
        self.targetProfitPct = p.takeProfitPct
        self.raoerVersion = ""
        self.locMode = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"strategyType": "orb", **self._p.to_dict()}


class OrbBot:
    def __init__(self, bot_id: str, code: str, mode: str, capital_krw: float,
                 params: Optional[Dict[str, Any]], namuh_account: Optional[NamuhAccount]):
        self.bot_id = bot_id
        self.coin = code
        self.coin_name = krx.KRX_STOCKS.get(code, {}).get("name", code)
        self.etf = krx.KRX_STOCKS.get(code, {}).get("etf", False)
        self.broker, self.market, self.currency, self.curr_symbol = "namuh", "KRX", "KRW", "원"
        self.interval = "ORB"
        self.mode = mode
        self.initial_krw = float(capital_krw)
        self.cash = float(capital_krw)
        self.orb = orb.OrbParams.from_dict(params)
        self.params = _ParamView(self.orb)
        self.namuh_account = namuh_account
        self.pos = _Pos()
        self.pending_orders: List[Dict[str, Any]] = []
        self.realized_pnl = 0.0
        self.total_trades = self.winning_trades = 0
        self.trade_history: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, Any]] = []
        self.created_at = int(time.time() * 1000)
        self.is_running = False
        self.last_price = 0.0
        self.last_price_at = 0.0
        self.last_quote: Dict[str, Any] = {}
        self.last_decision = "대기"
        self.price_failures = 0
        self.day: Optional[orb.OrbDay] = None
        self._last_reason = ""
        self._retry_after = 0.0
        # RVOL 기준: 날짜 → OR 끝 누적 거래량. 매일 봇이 직접 적는다.
        self.or_vol_history: Dict[str, int] = {}
        self._index_q: Dict[str, Any] = {}
        self._index_at = 0.0
        self._peak_saved = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ── 기록 ──
    def log(self, level: str, message: str):
        with self._lock:
            self.logs.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message})
            del self.logs[200:]
        logger.info(f"[{self.bot_id}] {level}: {message}")

    def _record(self, action: str, price: float, units: int, amount: float, pnl: float = 0.0,
                ret: float = 0.0, reason: str = ""):
        item = {"id": f"t-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}", "botId": self.bot_id,
                "coin": self.coin, "coinName": self.coin_name, "broker": "namuh", "market": "KRX",
                "currency": "KRW", "mode": self.mode, "action": action, "turn": self.pos.turn,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "price": round(price),
                "units": int(units), "amountKrw": round(amount), "pnlKrw": round(pnl),
                "returnPct": round(ret, 2), "reason": reason}
        with self._lock:
            self.trade_history.insert(0, item)
            del self.trade_history[500:]
        try:
            tradelog.append(item)
        except Exception as e:
            logger.error(f"[{self.bot_id}] 체결 일지 기록 실패: {e}")

    def _persist(self):
        from services.trader import bot_manager
        try:
            bot_manager.persist()
        except Exception as e:
            logger.error(f"[{self.bot_id}] 상태 저장 실패: {e}")

    # ── 수명주기 ──
    def start(self):
        self.is_running = True
        self._stop.clear()
        p = self.orb
        self.log("INFO", f"{'실전(LIVE)' if self.mode == 'LIVE' else '모의투자(PAPER)'} [나무증권 국내주식] ORB 봇 시작 · "
                         f"{self.coin_name}({self.coin}) · 운용자본 {self.initial_krw:,.0f}원")
        self.log("INFO", f"📐 OR 09:00~09:{p.rangeMin:02d} · 진입 ~09:{p.entryEndMin:02d} · "
                         f"익절 +{p.takeProfitPct}% · 손절 -{p.stopLossPct}%"
                         f"{' 또는 OR 저가 이탈' if p.useOrLowStop else ''} · 타임컷 09:{p.cutoffMin:02d}"
                         f"{' · VWAP 위' if p.useVwap else ''}"
                         f"{f' · 거래속도 ×{p.volSurge:g}' if p.volSurge > 0 else ''} · 하루 1회")
        self.log("INFO", "🧰 필터 · "
                 + (f"RVOL ≥ {p.rvolMin * 100:.0f}% (전일 같은 시각)" if p.rvolMin > 0 else "RVOL 끔")
                 + (f" · 지수 {krx.INDEX_PROXY[self._index_key()][1]} 시가 위" if p.marketFilter else " · 지수 필터 끔")
                 + (f" · 청산 트레일링 -{p.trailPct}% (최종 {orb.hm(p.finalCutMin)})" if p.exitMode == "trailing"
                    else f" · 청산 {orb.hm(p.cutoffMin)} 타임컷"))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._persist()

    def _index_key(self) -> str:
        return krx.KRX_STOCKS.get(self.coin, {}).get("index", "kospi")

    def _index_quote(self) -> Optional[Dict[str, Any]]:
        """지수 ETF 시세. 10초 캐시 — 진입 구간에서만 부른다."""
        if time.time() - self._index_at < 10 and self._index_q:
            return self._index_q
        code, name = krx.INDEX_PROXY[self._index_key()]
        try:
            q = krx.quote(self.namuh_account, code)
            self._index_q = {"name": name, "price": q["price"], "open": q["open"]}
        except Exception as e:
            self.log("WARNING", f"지수 ETF({code}) 시세 실패 — 지수 필터를 통과시키지 않습니다: {e}")
            self._index_q = {}
        self._index_at = time.time()
        return self._index_q or None

    def _prev_or_vol(self, today: str) -> Optional[int]:
        prior = sorted(d for d in self.or_vol_history if d < today)
        return self.or_vol_history[prior[-1]] if prior else None

    def _session_open(self) -> Tuple[bool, str]:
        now = datetime.now(orb.KST)
        if now.weekday() >= 5:
            return False, "주말이라 국내 장이 닫혀 있습니다."
        m = orb.minutes_since_open(now)
        if not (0 <= m < 390):
            return False, "국내 정규장(09:00~15:30)이 아닙니다."
        try:
            q = krx.quote(self.namuh_account, self.coin)
        except NamuhError as e:
            return False, f"시세를 받지 못했습니다: {e.message}"
        if not orb._hoga_fresh(q.get("hogaTime", ""), now):
            return False, f"호가가 멈춰 있습니다({q.get('hogaTime')}) — 휴장일일 수 있습니다."
        return True, ""

    def can_liquidate(self) -> Tuple[bool, str]:
        if not self.pos.open or self.mode != "LIVE":
            return True, ""
        if not (self.namuh_account and self.namuh_account.configured):
            return False, "나무증권 API 키가 등록되지 않았습니다."
        return self._session_open()

    def stop(self, liquidate: bool = True) -> bool:
        self.is_running = False
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)
        if liquidate and self.pos.open:
            try:
                ok, why = (True, "") if self.mode != "LIVE" else self._session_open()
                if not ok:
                    raise NamuhError(why)
                self._sell(krx.quote(self.namuh_account, self.coin), "사용자 정지 명령 (청산)")
            except Exception as e:
                self.log("ERROR", f"청산 실패 — 포지션이 남아 있습니다: {e}")
        self.log("WARNING", "봇이 정지되었습니다.")
        self._persist()
        return not (liquidate and self.pos.open)

    # ── 루프 ──
    def _loop(self):
        while not self._stop.is_set():
            now = datetime.now(orb.KST)
            date = now.strftime("%Y-%m-%d")
            if self.day is None or self.day.date != date:
                self.day = orb.OrbDay(date)
            m = orb.minutes_since_open(now)
            holding = self.pos.open
            # 볼 일이 없는 시간에는 시세도 부르지 않는다 (호출 한도를 해외 봇과 같이 쓴다)
            end_min = self.orb.entryEndMin
            idle = not holding and (now.weekday() >= 5 or self.day.done
                                    or m < -1.0 or m >= end_min + 1)
            if idle:
                if m >= end_min + 1 and not self.day.done:
                    self.day.done = True
                self._set_decision("오늘 ORB 끝 — 다음 거래일 09:00 에 다시 봅니다"
                                   if (self.day.done or m >= 0) else "개장 전 대기 (09:00 OR 측정)")
                self._stop.wait(orb.next_wake(self.orb, now, False, self.day.done))
                continue
            try:
                q = krx.quote(self.namuh_account, self.coin)
                self.last_quote, self.last_price, self.last_price_at = q, q["price"], time.time()
                self.price_failures = 0
            except Exception as e:
                self.price_failures += 1
                if self.price_failures in (1, 5) or self.price_failures % 30 == 0:
                    self.log("ERROR", f"국내 시세 조회 실패 ({self.price_failures}회째): {e}")
                self._stop.wait(5)
                continue
            if holding and q["price"] > self.pos.peak:
                self.pos.peak = float(q["price"])
                # 고점은 자주 바뀐다. 30초에 한 번만 디스크에 남긴다(재시작 때 스탑이 느슨해지지 않게).
                if time.time() - self._peak_saved > 30:
                    self._peak_saved = time.time()
                    self._persist()
            hold = ({"entryPrice": self.pos.entryPrice, "date": self.pos.date, "peak": self.pos.peak}
                    if holding else None)
            want_index = (self.orb.marketFilter and not holding and not self.day.done
                          and self.orb.rangeMin <= m < self.orb.entryEndMin)
            d = orb.decide(self.orb, self.day, q, now, hold,
                           index=self._index_quote() if want_index else None,
                           prev_or_vol=self._prev_or_vol(date))
            # 오늘 OR 끝 거래량을 적는다 — 내일의 RVOL 기준이 된다.
            if (m >= self.orb.rangeMin and self.day.orVol1 and not self.day.stale
                    and date not in self.or_vol_history):
                self.or_vol_history[date] = int(self.day.orVol1[1])
                for old in sorted(self.or_vol_history)[:-10]:
                    del self.or_vol_history[old]
                self.log("INFO", f"📒 {orb.hm(self.orb.rangeMin)} 누적 거래량 {self.day.orVol1[1]:,}주 기록 (내일 RVOL 기준)")
                self._persist()
            self._set_decision(d["reason"], log=d["phase"] in ("entry", "exit", "done"))
            if d["action"] and time.time() >= self._retry_after:
                try:
                    if d["action"] == "buy":
                        self._buy(q, d["reason"])
                    else:
                        self._sell(q, d["reason"])
                except Exception as e:
                    self._retry_after = time.time() + RETRY_SEC
                    self.log("ERROR", f"{'매수' if d['action'] == 'buy' else '매도'} 실패 — "
                                      f"{RETRY_SEC:.0f}초 뒤 다시 판단합니다: {e}")
                    if d["action"] == "buy":
                        if self.pending_orders:
                            # 거래소에 주문이 살아 있을 수 있다. 또 내면 두 번 산다.
                            self.day.done = True
                        else:
                            self.day.entered = False  # 주문이 안 나갔으면 진입을 쓴 것이 아니다
            self._stop.wait(orb.next_wake(self.orb, now, self.pos.open, self.day.done))

    def _set_decision(self, reason: str, log: bool = False):
        self.last_decision = reason
        if log and reason != self._last_reason:
            self.log("INFO", reason)
        self._last_reason = reason

    # ── 주문 ──
    def _buy(self, q: Dict[str, Any], reason: str):
        base = q.get("ask") or q["price"]
        limit = krx.shift_ticks(base, self.etf, 2)
        if q.get("upperLimit"):
            limit = min(limit, q["upperLimit"])
        fee = krx.FEE_PCT / 100
        qty = int(self.cash // (limit * (1 + fee)))
        if qty < 1:
            self.day.done = True
            self.log("WARNING", f"운용자본 {self.cash:,.0f}원으로 {limit:,}원짜리 1주를 살 수 없어 오늘은 쉽니다.")
            return
        if self.mode == "LIVE":
            acc = self.namuh_account
            before = krx.balance(acc, fresh=True)["holdings"].get(self.coin) or {}
            q0, a0 = int(before.get("qty") or 0), float(before.get("avg") or 0)
            order_no = krx.order(acc, "buy", self.coin, qty, limit)
            r = krx.await_fill(acc, self.coin, q0, qty, "buy")
            filled = int(r["filled"])
            if filled <= 0:
                self.pending_orders.append({"orderNo": order_no, "side": "buy", "qty": qty, "limit": limit,
                                            "at": time.time()})
                raise NamuhError(f"매수 주문({order_no} · {qty}주 @ {limit:,}원)이 8초 안에 체결되지 않았습니다. "
                                 f"거래소에 주문이 남아 있을 수 있으니 나무증권 앱에서 확인하세요.")
            # 잔고 평단으로 이번 체결가를 역산한다. 못 하면 지정가(최악값)로 둔다.
            qa, aa = int(r["qtyAfter"]), float(r["avgAfter"])
            fill = (qa * aa - q0 * a0) / filled if aa and qa > q0 else float(limit)
            if not (0 < fill <= limit * 1.001):
                fill = float(limit)
        else:
            filled, fill = qty, float(base)
        cost = filled * fill * (1 + fee)
        self.cash -= cost
        self.pos = _Pos(filled, fill, cost, self.day.date, peak=fill)
        self.last_decision = reason
        self.log("INFO", f"🟢 매수 {filled}주 @ {fill:,.0f}원 ({cost:,.0f}원) — {reason}")
        self._record("BUY", fill, filled, cost, reason=reason)
        self._persist()

    def _sell(self, q: Dict[str, Any], reason: str):
        units = self.pos.units
        base = q.get("bid") or q["price"]
        limit = krx.shift_ticks(base, self.etf, -2)
        if q.get("lowerLimit"):
            limit = max(limit, q["lowerLimit"])
        if self.mode == "LIVE":
            acc = self.namuh_account
            held = int((krx.balance(acc, fresh=True)["holdings"].get(self.coin) or {}).get("qty") or 0)
            qty = min(units, held)
            if qty < 1:
                raise NamuhError(f"계좌에 {self.coin} 가 없습니다 (장부 {units}주). 장부를 바꾸지 않습니다.")
            krx.order(acc, "sell", self.coin, qty, limit)
            r = krx.await_fill(acc, self.coin, held, qty, "sell")
            filled = int(r["filled"])
            if filled <= 0:
                raise NamuhError(f"매도 주문({qty}주 @ {limit:,}원)이 8초 안에 체결되지 않았습니다.")
            # 매도 체결가는 잔고로 알 수 없다. 접수 순간의 최우선 매수호가로 추정한다.
            fill = float(base)
        else:
            filled, fill = units, float(base)
        fee = krx.FEE_PCT / 100
        tax = 0.0 if self.etf else krx.SELL_TAX_PCT / 100
        proceeds = filled * fill * (1 - fee - tax)
        cost = self.pos.invested * filled / units
        pnl = proceeds - cost
        ret = pnl / cost * 100 if cost else 0.0
        self.cash += proceeds
        self.realized_pnl += pnl
        self.total_trades += 1
        self.winning_trades += 1 if pnl > 0 else 0
        left = units - filled
        self.pos = (_Pos(left, self.pos.entryPrice, self.pos.invested - cost, self.pos.date, self.pos.peak)
                    if left else _Pos())
        if self.day:
            self.day.done = True
        self.log("INFO", f"🔴 매도 {filled}주 @ {fill:,.0f}원 · 손익 {pnl:+,.0f}원 ({ret:+.2f}%) — {reason}"
                         + (f" · {left}주 남음" if left else ""))
        self._record("SELL", fill, filled, proceeds, pnl=pnl, ret=ret, reason=reason)
        self._persist()

    # ── 상태 ──
    def status(self) -> Dict[str, Any]:
        price = self.last_price or self.pos.entryPrice
        equity = self.cash + self.pos.units * price
        unreal = (price - self.pos.entryPrice) * self.pos.units if self.pos.open else 0.0
        q = self.last_quote or {}
        return {
            "botId": self.bot_id, "coin": self.coin, "coinName": self.coin_name,
            "broker": "namuh", "market": "KRX", "currency": "KRW", "currSymbol": "원",
            "interval": "ORB", "mode": self.mode, "strategyType": "orb", "raoerVersion": "",
            "turn": self.pos.turn, "splitCount": 1, "targetProfitPct": self.orb.takeProfitPct,
            "isRunning": self.is_running, "createdAt": self.created_at,
            "initialKrw": round(self.initial_krw), "equityKrw": round(equity), "cashKrw": round(self.cash),
            "budgetCarryover": 0, "pendingLocOrders": len(self.pending_orders), "locSession": None,
            "fxRate": 1.0, "equityKrwConverted": round(equity), "cashKrwConverted": round(self.cash),
            "investedKrwConverted": round(self.pos.invested), "unrealizedPnlKrwConverted": round(unreal),
            "realizedPnlKrwConverted": round(self.realized_pnl),
            "investedKrw": round(self.pos.invested), "units": self.pos.units,
            "entryPrice": round(self.pos.entryPrice), "currentPrice": round(price),
            "unrealizedPnlKrw": round(unreal),
            "unrealizedPnlPct": round((price / self.pos.entryPrice - 1) * 100, 2) if self.pos.open and self.pos.entryPrice else 0.0,
            "realizedPnlKrw": round(self.realized_pnl),
            "totalReturnPct": round((equity - self.initial_krw) / self.initial_krw * 100, 2) if self.initial_krw else 0.0,
            "totalTrades": self.total_trades,
            "winRatePct": round(self.winning_trades / self.total_trades * 100, 2) if self.total_trades else 0.0,
            "rsi": None, "priceAgeSec": round(time.time() - self.last_price_at, 1) if self.last_price_at else None,
            "pricePollSec": 2, "lastDecision": self.last_decision, "lastAiAnalysis": None, "macroRegime": None,
            "priceFailures": self.price_failures, "params": self.params.to_dict(), "recentLogs": self.logs[:20],
            "orb": {**(self.day.to_dict() if self.day else {}), "vwap": q.get("vwap"),
                    "hogaTime": q.get("hogaTime"), "peak": self.pos.peak if self.pos.open else None,
                    "exitMode": self.orb.exitMode,
                    "prevOrVol": self._prev_or_vol(self.day.date) if self.day else None,
                    "index": self._index_q or None},
        }

    def snapshot(self) -> Dict[str, Any]:
        return {"botId": self.bot_id, "coin": self.coin, "interval": "ORB", "mode": self.mode,
                "initialKrw": self.initial_krw, "broker": "namuh", "market": "KRX", "currency": "KRW",
                "params": self.params.to_dict(), "cash": self.cash, "pendingOrders": list(self.pending_orders),
                "units": self.pos.units, "entryPrice": self.pos.entryPrice, "totalInvested": self.pos.invested,
                "posDate": self.pos.date, "posPeak": self.pos.peak, "turn": self.pos.turn,
                "orVolHistory": dict(self.or_vol_history), "realizedPnl": self.realized_pnl,
                "totalTrades": self.total_trades, "winningTrades": self.winning_trades,
                "tradeHistory": self.trade_history, "createdAt": self.created_at, "wasRunning": self.is_running,
                # 오늘 이미 진입했는지는 저장한다. 재시작으로 하루 2번 들어가지 않게.
                "orbDay": self.day.to_dict() if self.day else None}

    @classmethod
    def restore(cls, d: Dict[str, Any], namuh_account: Optional[NamuhAccount]) -> "OrbBot":
        bot = cls(d["botId"], d["coin"], d.get("mode", "PAPER"), float(d["initialKrw"]),
                  d.get("params"), namuh_account)
        bot.cash = float(d.get("cash", d["initialKrw"]))
        bot.pending_orders = list(d.get("pendingOrders") or [])
        bot.pos = _Pos(int(d.get("units") or 0), float(d.get("entryPrice") or 0),
                       float(d.get("totalInvested") or 0), d.get("posDate") or "", float(d.get("posPeak") or 0))
        bot.or_vol_history = {str(k): int(v) for k, v in (d.get("orVolHistory") or {}).items()}
        bot.realized_pnl = float(d.get("realizedPnl", 0.0))
        bot.total_trades = int(d.get("totalTrades", 0))
        bot.winning_trades = int(d.get("winningTrades", 0))
        bot.trade_history = list(d.get("tradeHistory", []))
        bot.created_at = d.get("createdAt", bot.created_at)
        od = d.get("orbDay") or {}
        if od.get("date"):
            day = orb.OrbDay(od["date"])
            day.orHigh, day.orLow = od.get("orHigh"), od.get("orLow")
            day.entered, day.done, day.stale, day.note = (bool(od.get("entered")), bool(od.get("done")),
                                                          bool(od.get("stale")), od.get("note") or "")
            day.rvol, day.rvolChecked = od.get("rvol"), bool(od.get("rvolChecked"))
            if od.get("orVolEnd"):
                day.orVol1 = (0.0, int(od["orVolEnd"]))
            bot.day = day
        return bot

    def reconcile(self) -> Optional[str]:
        """LIVE 로 들고 있던 봇을 다시 띄워도 되는지 국내 잔고로 본다. 문제면 사유."""
        if self.mode != "LIVE" or not self.pos.open:
            return None
        if not (self.namuh_account and self.namuh_account.configured):
            return f"{self.coin} {self.pos.units}주를 들고 있는데 나무증권 키가 없어 대조할 수 없습니다."
        try:
            held = int((krx.balance(self.namuh_account, fresh=True)["holdings"].get(self.coin) or {}).get("qty") or 0)
        except Exception as e:
            return f"{self.coin} {self.pos.units}주를 들고 있는데 국내 잔고를 받지 못해 대조할 수 없습니다: {e}"
        if held < self.pos.units:
            return f"장부 {self.pos.units}주가 계좌 {held}주보다 많습니다. 재가동을 보류합니다."
        self.log("INFO", f"국내 잔고 대조 통과 (장부 {self.pos.units}주 / 계좌 {held}주)")
        return None
