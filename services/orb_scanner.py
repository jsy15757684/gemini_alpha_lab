"""국내주식 ORB 스캐너 — 감시 목록 전체를 실시간으로 보다가 신호가 뜬 종목을 산다.

  감시   실시간 체결 스트림(krx_stream · KRX 전용 oc) 으로 목록 전 종목을 동시에
         받는다. 폴링하지 않으므로 목록이 길어도 반응이 늦어지지 않는다 (최대 28종목
         + 지수 ETF 2 = 연결 하나의 구독 한도 30).
  판단   종목마다 services/orb.decide 를 그대로 쓴다 — OR · VWAP · 거래 속도 · RVOL ·
         지수 · 트레일링 규칙이 단일 종목일 때와 같다.
  매수   신호가 먼저 뜬 순서대로 최대 maxPositions 종목. 종목당 운용자본 ÷
         maxPositions. 한 종목은 하루 한 번만. 슬롯이 차면 새 신호는 기다린다.
  주문   주문 직전 REST 시세로 호가 · 상하한가 · ETF 여부를 다시 확인해 시장성
         지정가(±2호가)로 낸다. 체결은 국내 잔고(rsdl_qty)로 확인한다.
  안전   산 종목은 스트림이 10초 넘게 조용하면 REST 로 따로 본다(청산이 먼저다).
         스트림이 20초 넘게 멈추면 다시 붙는다. 장 밖에서는 연결을 닫아
         앱키당 연결 한도(2)를 비운다.
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from services import krx, orb, tradelog
from services.krx_stream import stream
from services.namuh import NamuhAccount, NamuhError

logger = logging.getLogger(__name__)

RETRY_SEC = 20.0
STALE_HELD_SEC = 10.0       # 산 종목 시세가 이만큼 조용하면 REST 로 본다
STALE_STREAM_SEC = 20.0     # 장 초반 스트림 전체가 이만큼 조용하면 다시 붙는다
PREP_MIN = -5.0             # 08:55 부터 연결


class _Pos:
    def __init__(self, units: int = 0, entryPrice: float = 0.0, invested: float = 0.0,
                 date: str = "", peak: float = 0.0):
        self.units, self.entryPrice, self.invested, self.date = int(units), float(entryPrice), float(invested), date
        self.peak = float(peak or entryPrice)

    def to_dict(self) -> Dict[str, Any]:
        return {"units": self.units, "entryPrice": self.entryPrice, "invested": self.invested,
                "date": self.date, "peak": self.peak}


class _Agg:
    """BotManager 가 bot.pos.units · bot.pos.open 으로 보는 합계."""

    def __init__(self, positions: Dict[str, _Pos]):
        self.units = sum(p.units for p in positions.values())
        self.entryPrice = 0.0
        self.turn = len(positions)
        self.totalInvested = sum(p.invested for p in positions.values())

    @property
    def open(self) -> bool:
        return self.units > 0


class _ParamView:
    def __init__(self, bot: "OrbScanner"):
        self._bot = bot
        self.strategyType = "orb"
        self.splitCount = bot.max_positions
        self.targetProfitPct = bot.orb.takeProfitPct
        self.raoerVersion = ""
        self.locMode = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"strategyType": "orb", **self._bot.orb.to_dict(),
                "watchlist": list(self._bot.watch), "maxPositions": self._bot.max_positions}


class OrbScanner:
    def __init__(self, bot_id: str, mode: str, capital_krw: float, params: Optional[Dict[str, Any]],
                 namuh_account: Optional[NamuhAccount], names: Optional[Dict[str, str]] = None):
        params = dict(params or {})
        self.bot_id = bot_id
        self.coin = "ORB"
        self.coin_name = "국내 ORB 스캐너"
        self.broker, self.market, self.currency, self.curr_symbol = "namuh", "KRX", "KRW", "원"
        self.interval = "ORB"
        self.mode = mode
        self.initial_krw = float(capital_krw)
        self.cash = float(capital_krw)
        self.watch: List[str] = [str(c).strip() for c in (params.pop("watchlist", None) or list(krx.KRX_STOCKS))]
        self.max_positions = max(1, int(params.pop("maxPositions", 3) or 3))
        self.orb = orb.OrbParams.from_dict(params)
        self.params = _ParamView(self)
        self.namuh_account = namuh_account
        self.names: Dict[str, str] = {c: krx.KRX_STOCKS.get(c, {}).get("name", c) for c in self.watch}
        self.names.update(names or {})
        self.positions: Dict[str, _Pos] = {}
        self.days: Dict[str, orb.OrbDay] = {}
        self.day_date = ""
        self.or_vol_history: Dict[str, Dict[str, int]] = {}
        self.pending_orders: List[Dict[str, Any]] = []
        self.realized_pnl = 0.0
        self.total_trades = self.winning_trades = 0
        self.trade_history: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, Any]] = []
        self.created_at = int(time.time() * 1000)
        self.is_running = False
        self.last_decision = "대기"
        self.last_price = 0.0
        self.last_price_at = 0.0
        self.price_failures = 0
        self._reasons: Dict[str, str] = {}
        self._retry_after: Dict[str, float] = {}
        self._slot_logged: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ── 겉모습 (BotManager 호환) ──
    @property
    def pos(self) -> _Agg:
        return _Agg(self.positions)

    def log(self, level: str, message: str):
        with self._lock:
            self.logs.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message})
            del self.logs[200:]
        logger.info(f"[{self.bot_id}] {level}: {message}")

    def _name(self, code: str) -> str:
        return self.names.get(code, code)

    def _record(self, code: str, action: str, price: float, units: int, amount: float,
                pnl: float = 0.0, ret: float = 0.0, reason: str = ""):
        item = {"id": f"t-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}", "botId": self.bot_id,
                "coin": code, "coinName": self._name(code), "broker": "namuh", "market": "KRX",
                "currency": "KRW", "mode": self.mode, "action": action, "turn": len(self.positions),
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
        self.log("INFO", f"{'실전(LIVE)' if self.mode == 'LIVE' else '모의투자(PAPER)'} [나무증권 국내주식] ORB 스캐너 시작 · "
                         f"감시 {len(self.watch)}종목 · 최대 {self.max_positions}종목 동시 보유 · "
                         f"운용자본 {self.initial_krw:,.0f}원 (종목당 {self.slot_budget():,.0f}원)")
        self.log("INFO", f"📐 OR 09:00~{orb.hm(p.rangeMin)} · 진입 ~{orb.hm(p.entryEndMin)} · 손절 -{p.stopLossPct}%"
                         f"{' · OR 저가 이탈' if p.useOrLowStop else ''}"
                         + (f" · 트레일링 -{p.trailPct}% (최종 {orb.hm(p.finalCutMin)})" if p.exitMode == "trailing"
                            else f" · 익절 +{p.takeProfitPct}% · {orb.hm(p.cutoffMin)} 타임컷")
                         + (" · VWAP 위" if p.useVwap else "")
                         + (f" · 거래속도 ×{p.volSurge:g}" if p.volSurge > 0 else "")
                         + (f" · RVOL ≥ {p.rvolMin * 100:.0f}%" if p.rvolMin > 0 else "")
                         + (" · 지수 시가 위" if p.marketFilter else ""))
        self._thread = threading.Thread(target=self._loop, name=f"orb-{self.bot_id}", daemon=True)
        self._thread.start()
        self._persist()

    def slot_budget(self) -> float:
        return self.initial_krw / self.max_positions

    def _codes_to_stream(self) -> List[str]:
        idx = {krx.INDEX_PROXY[k][0] for k in krx.INDEX_PROXY} if self.orb.marketFilter else set()
        codes = list(dict.fromkeys(list(self.positions) + self.watch + sorted(idx)))
        return codes[:30]

    def _session_open(self) -> Tuple[bool, str]:
        now = datetime.now(orb.KST)
        if now.weekday() >= 5:
            return False, "주말이라 국내 장이 닫혀 있습니다."
        if not (0 <= orb.minutes_since_open(now) < 390):
            return False, "국내 정규장(09:00~15:30)이 아닙니다."
        return True, ""

    def can_liquidate(self) -> Tuple[bool, str]:
        if not self.positions or self.mode != "LIVE":
            return True, ""
        if not (self.namuh_account and self.namuh_account.configured):
            return False, "나무증권 API 키가 등록되지 않았습니다."
        return self._session_open()

    def stop(self, liquidate: bool = True) -> bool:
        self.is_running = False
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)
        if liquidate:
            for code in list(self.positions):
                try:
                    if self.mode == "LIVE":
                        ok, why = self._session_open()
                        if not ok:
                            raise NamuhError(why)
                    self._sell(code, "사용자 정지 명령 (청산)")
                except Exception as e:
                    self.log("ERROR", f"{self._name(code)} 청산 실패 — 포지션이 남아 있습니다: {e}")
        stream.release()
        self.log("WARNING", "봇이 정지되었습니다.")
        self._persist()
        return not (liquidate and self.positions)

    # ── 루프 ──
    def _index_snap(self, code: str) -> Optional[Dict[str, Any]]:
        key = krx.KRX_STOCKS.get(code, {}).get("index") or (stream.get(code) or {}).get("market") or "kospi"
        pcode, name = krx.INDEX_PROXY.get(key, krx.INDEX_PROXY["kospi"])
        s = stream.get(pcode)
        if not s or (stream.age(pcode) or 999) > 90:
            return None
        return {"name": name, "price": s["price"], "open": s["open"]}

    def _prev_or_vol(self, code: str, today: str) -> Optional[int]:
        h = self.or_vol_history.get(code) or {}
        prior = sorted(d for d in h if d < today)
        return h[prior[-1]] if prior else None

    def _quote_for(self, code: str) -> Optional[Dict[str, Any]]:
        """판단용 시세. 스트림이 우선, 산 종목은 조용하면 REST 로."""
        s = stream.get(code)
        age = stream.age(code)
        if code in self.positions and (s is None or (age or 999) > STALE_HELD_SEC):
            try:
                return krx.quote(self.namuh_account, code)
            except Exception as e:
                self.price_failures += 1
                if self.price_failures in (1, 5) or self.price_failures % 30 == 0:
                    self.log("ERROR", f"{self._name(code)} 시세 조회 실패 ({self.price_failures}회째): {e}")
                return None
        return s

    def _loop(self):
        while not self._stop.is_set():
            now = datetime.now(orb.KST)
            date = now.strftime("%Y-%m-%d")
            m = orb.minutes_since_open(now)
            if date != self.day_date:
                self.day_date, self.days = date, {}
                self._slot_logged.clear()
            weekday = now.weekday() < 5
            entry_open = weekday and PREP_MIN <= m < self.orb.entryEndMin + 1
            watching = entry_open and not all(d.done for d in self.days.values()) if self.days else entry_open
            if not (self.positions or watching):
                if stream.connected:
                    stream.release()
                self.last_decision = ("오늘 ORB 끝 — 다음 거래일 08:55 에 감시를 시작합니다"
                                      if weekday and m >= 0 else "개장 전 대기 (08:55 실시간 감시 시작)")
                self._stop.wait(self._sleep_until_prep(now))
                continue
            try:
                stream.ensure(self.namuh_account, self._codes_to_stream())
            except Exception as e:
                self.log("ERROR", f"실시간 시세를 시작하지 못했습니다: {e}")
            if 0 <= m < self.orb.cutoffMin and stream.last_msg_at and time.time() - stream.last_msg_at > STALE_STREAM_SEC:
                stream.kick(f"{STALE_STREAM_SEC:.0f}초 넘게 체결이 없음")
            try:
                self._tick(now, date, m)
            except Exception as e:
                logger.exception(f"[{self.bot_id}] 판단 중 오류")
                self.log("ERROR", f"판단 중 오류 (계속 봅니다): {e}")
            self._stop.wait(1.0)

    def _sleep_until_prep(self, now: datetime) -> float:
        """다음 08:55 까지. 10분씩 끊어 자서 정지 요청을 받는다."""
        n = now.astimezone(orb.KST)
        prep = n.replace(hour=8, minute=55, second=0, microsecond=0)
        if n >= prep:
            prep += timedelta(days=1)
        return max(1.0, min(600.0, (prep - n).total_seconds()))

    def _tick(self, now: datetime, date: str, m: float):
        # 1) 산 종목 청산부터
        for code in list(self.positions):
            q = self._quote_for(code)
            if not q:
                continue
            pos = self.positions[code]
            if q["price"] > pos.peak:
                pos.peak = float(q["price"])
            day = self.days.setdefault(code, orb.OrbDay(date))
            d = orb.decide(self.orb, day, q, now, {"entryPrice": pos.entryPrice, "date": pos.date, "peak": pos.peak})
            self._note(code, d)
            if d["action"] == "sell" and time.time() >= self._retry_after.get(code, 0):
                try:
                    self._sell(code, d["reason"])
                except Exception as e:
                    self._retry_after[code] = time.time() + RETRY_SEC
                    self.log("ERROR", f"{self._name(code)} 매도 실패 — {RETRY_SEC:.0f}초 뒤 다시 판단: {e}")
        if m < 0:
            self.last_decision = f"개장 전 · 실시간 {'연결됨' if stream.connected else '연결 중'} · {len(self.watch)}종목 대기"
            return
        # 2) 감시 종목
        for code in self.watch:
            if code in self.positions:
                continue
            q = stream.get(code)
            if not q:
                continue
            day = self.days.setdefault(code, orb.OrbDay(date))
            if day.done:
                continue
            d = orb.decide(self.orb, day, q, now, None, index=self._index_snap(code),
                           prev_or_vol=self._prev_or_vol(code, date))
            self._note(code, d)
            if m >= self.orb.rangeMin and day.orVol1 and not day.stale:
                h = self.or_vol_history.setdefault(code, {})
                if date not in h:
                    h[date] = int(day.orVol1[1])
                    for old in sorted(h)[:-10]:
                        del h[old]
                    self._persist_soon()
            if d["action"] != "buy":
                continue
            if len(self.positions) >= self.max_positions:
                day.entered = False                   # 슬롯이 비면 다시 신호를 본다
                if code not in self._slot_logged:
                    self._slot_logged.add(code)
                    self.log("INFO", f"{self._name(code)} 신호 — 슬롯 {self.max_positions}개가 차 있어 기다립니다")
                continue
            if time.time() < self._retry_after.get(code, 0):
                day.entered = False
                continue
            try:
                self._buy(code, d["reason"])
            except Exception as e:
                self._retry_after[code] = time.time() + RETRY_SEC
                self.log("ERROR", f"{self._name(code)} 매수 실패 — {e}")
                if any(o.get("code") == code for o in self.pending_orders):
                    day.done = True                   # 주문이 살아 있을 수 있다 — 두 번 사지 않는다
                else:
                    day.entered = False
        self._summarize(m)

    _persist_due = 0.0

    def _persist_soon(self):
        # 09:05 에 종목마다 기록이 몰린다. 한 번에 모아 쓴다.
        if time.time() >= self._persist_due:
            self._persist_due = time.time() + 5
            self._persist()

    def _note(self, code: str, d: Dict[str, Any]):
        r = d["reason"]
        if d["phase"] in ("entry", "exit") or (d["phase"] == "done" and self._reasons.get(code) != r
                                              and ("RVOL" in r or "기록" in r)):
            self.log("INFO", f"{self._name(code)} — {r}")
        self._reasons[code] = r

    def _summarize(self, m: float):
        done = sum(1 for d in self.days.values() if d.done)
        held = ", ".join(f"{self._name(c)} {p.units}주" for c, p in self.positions.items())
        live = sum(1 for c in self.watch if stream.get(c))
        phase = ("OR 측정" if m < self.orb.rangeMin else
                 "돌파 감시" if m < self.orb.entryEndMin else "진입 마감")
        self.last_decision = (f"{phase} · 실시간 {live}/{len(self.watch)}종목 · 오늘 끝 {done} · "
                              f"보유 {len(self.positions)}/{self.max_positions}" + (f" ({held})" if held else ""))

    # ── 주문 ──
    def _buy(self, code: str, reason: str):
        acc = self.namuh_account
        q = krx.quote(acc, code)                       # 주문가는 REST 호가로 (스트림은 신호용)
        base = q.get("ask") or q["price"]
        limit = krx.shift_ticks(base, q["etf"], 2)
        if q.get("upperLimit"):
            limit = min(limit, q["upperLimit"])
        fee = krx.FEE_PCT / 100
        budget = min(self.cash, self.slot_budget())
        qty = int(budget // (limit * (1 + fee)))
        day = self.days.get(code)
        if qty < 1:
            if day:
                day.done = True
                day.note = f"종목당 {budget:,.0f}원으로 1주({limit:,}원)를 살 수 없어 건너뜁니다"
            self.log("WARNING", f"{self._name(code)} 신호 — {day.note if day else ''}")
            return
        if self.mode == "LIVE":
            before = krx.balance(acc, fresh=True)["holdings"].get(code) or {}
            q0, a0 = int(before.get("qty") or 0), float(before.get("avg") or 0)
            order_no = krx.order(acc, "buy", code, qty, limit)
            r = krx.await_fill(acc, code, q0, qty, "buy")
            filled = int(r["filled"])
            if filled <= 0:
                self.pending_orders.append({"code": code, "orderNo": order_no, "side": "buy", "qty": qty,
                                            "limit": limit, "at": time.time()})
                raise NamuhError(f"매수 주문({order_no} · {qty}주 @ {limit:,}원)이 8초 안에 체결되지 않았습니다. "
                                 f"나무증권 앱에서 확인하세요.")
            qa, aa = int(r["qtyAfter"]), float(r["avgAfter"])
            fill = (qa * aa - q0 * a0) / filled if aa and qa > q0 else float(limit)
            if not (0 < fill <= limit * 1.001):
                fill = float(limit)
        else:
            filled, fill = qty, float(base)
        cost = filled * fill * (1 + fee)
        self.cash -= cost
        self.positions[code] = _Pos(filled, fill, cost, self.day_date, peak=fill)
        self.log("INFO", f"🟢 {self._name(code)} 매수 {filled}주 @ {fill:,.0f}원 ({cost:,.0f}원) — {reason}")
        self._record(code, "BUY", fill, filled, cost, reason=reason)
        self._persist()

    def _sell(self, code: str, reason: str):
        acc = self.namuh_account
        pos = self.positions[code]
        units = pos.units
        q = krx.quote(acc, code)
        base = q.get("bid") or q["price"]
        limit = krx.shift_ticks(base, q["etf"], -2)
        if q.get("lowerLimit"):
            limit = max(limit, q["lowerLimit"])
        if self.mode == "LIVE":
            held = int((krx.balance(acc, fresh=True)["holdings"].get(code) or {}).get("qty") or 0)
            qty = min(units, held)
            if qty < 1:
                raise NamuhError(f"계좌에 {self._name(code)} 가 없습니다 (장부 {units}주). 장부를 바꾸지 않습니다.")
            krx.order(acc, "sell", code, qty, limit)
            r = krx.await_fill(acc, code, held, qty, "sell")
            filled = int(r["filled"])
            if filled <= 0:
                raise NamuhError(f"매도 주문({qty}주 @ {limit:,}원)이 8초 안에 체결되지 않았습니다.")
            # 매도 체결가는 잔고로 알 수 없다(sll_amt 는 평가 금액). 최우선 매수호가로 추정.
            fill = float(base)
        else:
            filled, fill = units, float(base)
        fee = krx.FEE_PCT / 100
        tax = 0.0 if q["etf"] else krx.SELL_TAX_PCT / 100
        proceeds = filled * fill * (1 - fee - tax)
        cost = pos.invested * filled / units
        pnl = proceeds - cost
        ret = pnl / cost * 100 if cost else 0.0
        self.cash += proceeds
        self.realized_pnl += pnl
        self.total_trades += 1
        self.winning_trades += 1 if pnl > 0 else 0
        left = units - filled
        if left:
            self.positions[code] = _Pos(left, pos.entryPrice, pos.invested - cost, pos.date, pos.peak)
        else:
            del self.positions[code]
            if code in self.days:
                self.days[code].done = True
        self.log("INFO", f"🔴 {self._name(code)} 매도 {filled}주 @ {fill:,.0f}원 · 손익 {pnl:+,.0f}원 ({ret:+.2f}%) — {reason}"
                         + (f" · {left}주 남음" if left else ""))
        self._record(code, "SELL", fill, filled, proceeds, pnl=pnl, ret=ret, reason=reason)
        self._persist()

    # ── 상태 ──
    def watch_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for code in list(dict.fromkeys(list(self.positions) + self.watch)):
            s = stream.get(code) or {}
            d = self.days.get(code)
            p = self.positions.get(code)
            rows.append({
                "code": code, "name": self._name(code), "price": s.get("price"), "vwap": s.get("vwap"),
                "orHigh": d.orHigh if d else None, "orLow": d.orLow if d else None,
                "rvol": d.rvol if d else None, "entered": bool(d and d.entered), "done": bool(d and d.done),
                "reason": self._reasons.get(code, ""), "ageSec": round(stream.age(code), 1) if s else None,
                "held": p.to_dict() if p else None,
                "prevOrVol": self._prev_or_vol(code, self.day_date) if self.day_date else None,
            })
        return rows

    def status(self) -> Dict[str, Any]:
        mark = 0.0
        unreal = 0.0
        for code, p in self.positions.items():
            px = (stream.get(code) or {}).get("price") or p.entryPrice
            mark += p.units * px
            unreal += (px - p.entryPrice) * p.units
        equity = self.cash + mark
        invested = sum(p.invested for p in self.positions.values())
        return {
            "botId": self.bot_id, "coin": "ORB", "coinName": self.coin_name,
            "broker": "namuh", "market": "KRX", "currency": "KRW", "currSymbol": "원",
            "interval": "ORB", "mode": self.mode, "strategyType": "orb", "raoerVersion": "",
            "turn": len(self.positions), "splitCount": self.max_positions,
            "targetProfitPct": self.orb.takeProfitPct, "isRunning": self.is_running, "createdAt": self.created_at,
            "initialKrw": round(self.initial_krw), "equityKrw": round(equity), "cashKrw": round(self.cash),
            "budgetCarryover": 0, "pendingLocOrders": len(self.pending_orders), "locSession": None,
            "fxRate": 1.0, "equityKrwConverted": round(equity), "cashKrwConverted": round(self.cash),
            "investedKrwConverted": round(invested), "unrealizedPnlKrwConverted": round(unreal),
            "realizedPnlKrwConverted": round(self.realized_pnl), "investedKrw": round(invested),
            "units": self.pos.units, "entryPrice": 0, "currentPrice": 0,
            "unrealizedPnlKrw": round(unreal),
            "unrealizedPnlPct": round(unreal / invested * 100, 2) if invested else 0.0,
            "realizedPnlKrw": round(self.realized_pnl),
            "totalReturnPct": round((equity - self.initial_krw) / self.initial_krw * 100, 2) if self.initial_krw else 0.0,
            "totalTrades": self.total_trades,
            "winRatePct": round(self.winning_trades / self.total_trades * 100, 2) if self.total_trades else 0.0,
            "rsi": None, "priceAgeSec": None, "pricePollSec": 1, "lastDecision": self.last_decision,
            "lastAiAnalysis": None, "macroRegime": None, "priceFailures": self.price_failures,
            "params": self.params.to_dict(), "recentLogs": self.logs[:20],
            "orbScan": {"watch": self.watch_rows(), "maxPositions": self.max_positions,
                        "slotBudget": round(self.slot_budget()), "stream": stream.status(),
                        "exitMode": self.orb.exitMode},
        }

    def snapshot(self) -> Dict[str, Any]:
        return {"botId": self.bot_id, "coin": "ORB", "interval": "ORB", "mode": self.mode,
                "initialKrw": self.initial_krw, "broker": "namuh", "market": "KRX", "currency": "KRW",
                "params": self.params.to_dict(), "names": dict(self.names), "cash": self.cash,
                "pendingOrders": list(self.pending_orders),
                "positions": {c: p.to_dict() for c, p in self.positions.items()},
                "units": self.pos.units, "realizedPnl": self.realized_pnl,
                "totalTrades": self.total_trades, "winningTrades": self.winning_trades,
                "tradeHistory": self.trade_history, "createdAt": self.created_at, "wasRunning": self.is_running,
                "orVolHistory": {c: dict(h) for c, h in self.or_vol_history.items()},
                # 오늘 진입한 종목은 저장한다. 재시작으로 같은 종목을 하루 두 번 사지 않게.
                "dayDate": self.day_date,
                "days": {c: d.to_dict() for c, d in self.days.items()}}

    @classmethod
    def restore(cls, d: Dict[str, Any], namuh_account: Optional[NamuhAccount]) -> "OrbScanner":
        bot = cls(d["botId"], d.get("mode", "PAPER"), float(d["initialKrw"]), d.get("params"),
                  namuh_account, names=d.get("names"))
        bot.cash = float(d.get("cash", d["initialKrw"]))
        bot.pending_orders = list(d.get("pendingOrders") or [])
        bot.positions = {c: _Pos(v.get("units", 0), v.get("entryPrice", 0), v.get("invested", 0),
                                 v.get("date", ""), v.get("peak", 0))
                         for c, v in (d.get("positions") or {}).items() if int(v.get("units") or 0) > 0}
        bot.realized_pnl = float(d.get("realizedPnl", 0.0))
        bot.total_trades = int(d.get("totalTrades", 0))
        bot.winning_trades = int(d.get("winningTrades", 0))
        bot.trade_history = list(d.get("tradeHistory", []))
        bot.created_at = d.get("createdAt", bot.created_at)
        bot.or_vol_history = {str(c): {str(k): int(v) for k, v in h.items()}
                              for c, h in (d.get("orVolHistory") or {}).items()}
        bot.day_date = d.get("dayDate") or ""
        for c, od in (d.get("days") or {}).items():
            day = orb.OrbDay(od.get("date") or bot.day_date)
            day.orHigh, day.orLow = od.get("orHigh"), od.get("orLow")
            day.entered, day.done, day.stale, day.note = (bool(od.get("entered")), bool(od.get("done")),
                                                          bool(od.get("stale")), od.get("note") or "")
            day.rvol, day.rvolChecked = od.get("rvol"), bool(od.get("rvolChecked"))
            if od.get("orVolEnd"):
                day.orVol1 = (0.0, int(od["orVolEnd"]))
            bot.days[c] = day
        return bot

    def reconcile(self) -> Optional[str]:
        if self.mode != "LIVE" or not self.positions:
            return None
        if not (self.namuh_account and self.namuh_account.configured):
            return f"{len(self.positions)}종목을 들고 있는데 나무증권 키가 없어 대조할 수 없습니다."
        try:
            h = krx.balance(self.namuh_account, fresh=True)["holdings"]
        except Exception as e:
            return f"{len(self.positions)}종목을 들고 있는데 국내 잔고를 받지 못해 대조할 수 없습니다: {e}"
        short = [f"{self._name(c)} 장부 {p.units} > 계좌 {int((h.get(c) or {}).get('qty') or 0)}"
                 for c, p in self.positions.items() if int((h.get(c) or {}).get("qty") or 0) < p.units]
        if short:
            return "장부가 계좌보다 많습니다 — " + " · ".join(short) + ". 재가동을 보류합니다."
        self.log("INFO", "국내 잔고 대조 통과 (" + ", ".join(f"{self._name(c)} {p.units}주" for c, p in self.positions.items()) + ")")
        return None
