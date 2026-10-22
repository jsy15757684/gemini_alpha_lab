"""국내 주식 장초반 스캘퍼 (모의 전용) — local_training/stock_scalper 의 규칙을 서버에 옮겼다.

**실주문은 없다.** 나무증권 국내 시세만 읽고, 매매는 가상 장부(data/scalper.json)에만 적는다.
2~4주 모의 결과를 보고 실계좌로 넘길지 판단한다 (사용자 결정 2026-10-21).

하루 흐름 (한국시간 · 평일)
  08:50     후보(코스피200 ∪ 코스닥150, 위험 종목 제외)를 훑어 예비 목록 (kr_selector.select)
  08:57:50  예비 목록만 다시 조회해 대장주 maxStocks 개 (kr_selector.refine)
            — 동시호가 예상체결 기준. 원본은 다음(daum) 비공식 주소의 '전날' 거래량으로 골랐다.
  09:00     감시 시작. 장이 안 열린 날(휴장)은 호가 시각으로 알아채고 쉰다
  ~09:20    진입: 현재가 > 시가 × (1 + breakoutPct%) 이고 등락률 ≤ maxChangePct
  ~09:30    청산 규칙 (원본 execution.py 와 같은 순서)
              손절      순수익률 ≤ −stopPct            → 전량
              트레일링  고점이 매수가 +partialPct% 를 넘은 뒤 고점 대비 −trailPct% → 전량
              목표 익절  순수익률 ≥ targetPct          → 전량
              1차 익절  순수익률 ≥ partialPct (처음 한 번) → 절반
            순수익률 = (현재가 − 매수가) ÷ 매수가 − costPct(세금 · 수수료 0.20%)
  09:30     남은 물량 전량 청산 · 그날 결과 기록

원본과 다른 점 — 모의 체결가를 살 때 매도1호가 · 팔 때 매수1호가로 적는다 (원본은 현재가).
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from services import kr_selector, krx, krx_master
from services.botstore import JsonStore
from services.namuh import NamuhError

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

store = JsonStore("scalper.json", "국내 스캘퍼(모의) 상태")

SELECT_AT = (8, 50, 0)
REFINE_AT = (8, 57, 50)
OPEN_AT = (9, 0, 0)
HOGA_FRESH_SEC = 180          # 호가 시각이 지금과 이보다 멀면 장이 안 열린 것 (휴장 · 지연 개장)
MAX_TRADES = 3000
MAX_DAYS = 400


@dataclass
class ScalpParams:
    maxStocks: int = 3
    positionKrw: float = 3_000_000
    partialPct: float = 1.8
    targetPct: float = 2.5
    stopPct: float = 1.5
    trailPct: float = 0.8
    breakoutPct: float = 0.5
    maxChangePct: float = 10.0
    costPct: float = krx.SELL_COST_PCT
    entryEnd: str = "09:20"
    forceClose: str = "09:30"
    seedKrw: float = 10_000_000   # 원본 TOTAL_SEED_KRW — 하루 손실 한도의 기준
    maxDayStops: int = 2          # 하루 손절 이 횟수면 그날 신규 진입 끝 (0 = 끔)
    maxDayLossPct: float = 1.0    # 하루 실현 손실이 시드의 이 %면 그날 신규 진입 끝 (0 = 끔)
    # ── 추가 스킬 (사용자 2026-10-21 · 처음부터 켬 · 하나씩 끌 수 있다) ──
    timeStopOn: bool = True       # ② 30초 타임컷: 산 뒤 timeStopSec 안에 +timeStopPct 를 못 찍으면 판다
    timeStopSec: float = 30.0
    timeStopPct: float = 1.5      #   매수가 대비 (비용 빼기 전)
    imbalanceOn: bool = True      # ③ 호가 불균형: 총매도잔량 ÷ 총매수잔량 ≥ imbalanceMin 일 때만 산다
    imbalanceMin: float = 2.0
    pyramidOn: bool = True        # ⑥ 불타기: +pyramidPct 를 넘고 거래량이 몰리면 처음 수량의 pyramidAddPct% 를 더 산다
    pyramidPct: float = 1.5       #   그 뒤 평단(비용 포함) 아래로 내려오면 전량 — 수익 보존선
    pyramidAddPct: float = 30.0
    pyramidVolX: float = 2.0      #   직전 몇 초 거래 속도 ≥ 개장 이후 평균 속도 × 이 배수
    gapMinPct: float = 1.5        # 선정: 동시호가 예상 갭 하한 (원본 MIN_GAP_UP_PCT)
    gapMaxPct: float = 7.0        # 선정: 상한 (원본 MAX_GAP_UP_PCT)
    minExpTurnoverEok: float = 0.5

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ScalpParams":
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k) and v is not None:
                setattr(p, k, type(getattr(p, k))(v))
        p.maxStocks = max(1, min(10, p.maxStocks))
        return p

    def selector(self) -> kr_selector.SelectParams:
        return kr_selector.SelectParams(topN=self.maxStocks, gapMinPct=self.gapMinPct, gapMaxPct=self.gapMaxPct,
                                        minExpTurnoverEok=self.minExpTurnoverEok,
                                        preN=max(self.maxStocks, 50), looseGapPct=3.0)


def _hm(s: str) -> tuple:
    h, m = s.split(":")
    return int(h), int(m), 0


def _at(now: datetime, hms: tuple) -> datetime:
    return now.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)


def net_pct(entry: float, price: float, cost_pct: float) -> float:
    return (price - entry) / entry * 100.0 - cost_pct


def decide_exit(pos: Dict[str, Any], price: float, p: ScalpParams) -> Optional[Dict[str, Any]]:
    """보유 하나 · 현재가 하나로 청산을 판단한다 (원본 evaluate_exit 와 같은 순서). 없으면 None."""
    entry = pos["entry"]
    pos["peak"] = max(pos["peak"], price)
    n = net_pct(entry, price, p.costPct)
    if n <= -p.stopPct:
        return {"qty": pos["remaining"], "kind": "STOP_LOSS", "reason": f"손절 (−{p.stopPct}%)"}
    if pos["peak"] > entry * (1 + p.partialPct / 100.0):
        dd = (pos["peak"] - price) / pos["peak"] * 100.0
        if dd >= p.trailPct:
            return {"qty": pos["remaining"], "kind": "TRAIL", "reason": f"트레일링 (고점 대비 −{dd:.1f}%)"}
    if n >= p.targetPct:
        return {"qty": pos["remaining"], "kind": "TARGET", "reason": f"목표 익절 (+{p.targetPct}%)"}
    if not pos.get("partialDone") and n >= p.partialPct:
        q = pos["remaining"] // 2
        if q > 0:
            return {"qty": q, "kind": "PARTIAL", "reason": f"1차 익절 (+{p.partialPct}%) 절반"}
    return None


def walk_buy(q: Dict[str, Any], budget: float) -> Optional[tuple]:
    """매도호가를 1단계부터 먹어 내려가며 budget 으로 살 수 있는 (수량, 평균 체결가). 10단계로 못 채우면 None.

    호가 잔량이 없으면(옛 응답 · 시험) 매도1호가 한 가격으로 계산한다.
    """
    levels = q.get("asks") or []
    if not levels:
        px = q.get("ask") or q["price"]
        qty = int(budget // px)
        return (qty, float(px)) if qty > 0 else None
    qty = cost = 0
    for px, size in levels:
        take = min(size, int((budget - cost) // px))
        qty, cost = qty + take, cost + take * px
        if take < size:                     # 이 단계에서 예산이 끝났다
            break
    else:
        if budget - cost >= levels[-1][0]:  # 10단계를 다 먹고도 예산이 남았다 — 호가가 너무 얇다
            return None
    return (qty, cost / qty) if qty > 0 else None


def walk_sell(q: Dict[str, Any], qty: int) -> float:
    """매수호가를 먹어 내려가며 qty 를 팔 때의 평균 체결가. 10단계로 모자라면 나머지는 마지막 단계 가격."""
    levels = q.get("bids") or []
    if not levels:
        return float(q.get("bid") or q["price"])
    left, got = qty, 0.0
    for px, size in levels:
        take = min(size, left)
        got, left = got + take * px, left - take
        if left <= 0:
            break
    if left > 0:
        got += left * levels[-1][0]
    return got / qty


def vi_room_ok(q: Dict[str, Any], entry: float, p: "ScalpParams") -> bool:
    """정적 VI 상단까지 목표 익절폭보다 여유가 있나. VI 가 걸리면 2분 단일가라 그동안 손절도 못 한다."""
    up = q.get("viUp") or 0
    return not up or entry * (1 + p.targetPct / 100.0) < up


def imbalance(q: Dict[str, Any]) -> Optional[float]:
    """총매도잔량 ÷ 총매수잔량. 잔량을 모르면 None."""
    a, b = q.get("totalAsk") or 0, q.get("totalBid") or 0
    return round(a / b, 2) if a > 0 and b > 0 else None


def can_enter(q: Dict[str, Any], p: ScalpParams) -> bool:
    """원본 evaluate_entry — 시가 돌파 (+breakoutPct%) · 너무 오른 종목(> maxChangePct) 제외."""
    o, px = q.get("open") or 0, q.get("price") or 0
    return o > 0 and px > o * (1 + p.breakoutPct / 100.0) and (q.get("changePct") or 0) <= p.maxChangePct


class Scalper:
    def __init__(self, namuh_account=None):
        self.acc = namuh_account
        self.lock = threading.RLock()
        self.enabled = True
        self.params = ScalpParams()
        self.day: Dict[str, Any] = {}          # 오늘: date · phase · prelist · chosen · selection · note · done
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trades: List[Dict[str, Any]] = []
        self.days: List[Dict[str, Any]] = []   # 날마다 결과
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.last_error = ""
        self._vol_seen: Dict[str, tuple] = {}
        self._err_streak = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 저장 ──
    def snapshot(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "params": asdict(self.params), "day": self.day,
                "positions": self.positions, "trades": self.trades[-MAX_TRADES:], "days": self.days[-MAX_DAYS:]}

    def save(self) -> None:
        with self.lock:
            store.save([self.snapshot()])

    def load(self) -> None:
        recs = store.load()
        if not recs:
            return
        d = recs[0]
        self.enabled = bool(d.get("enabled", True))
        self.params = ScalpParams.from_dict(d.get("params"))
        self.day = d.get("day") or {}
        self.positions = d.get("positions") or {}
        self.trades = d.get("trades") or []
        self.days = d.get("days") or []

    def log(self, level: str, msg: str) -> None:
        self.logs.append({"time": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "level": level, "message": msg})
        del self.logs[:-200]
        (logger.warning if level in ("WARNING", "ERROR") else logger.info)(f"[국내 스캘퍼] {msg}")

    # ── 가상 체결 ──
    def _skip(self, code: str, why: str, for_day: bool) -> None:
        """사지 않은 이유를 남긴다. for_day=True 면 그날은 그 종목을 더 보지 않는다 (VI),
        False 면 다음 판단에 다시 본다 (개장 직후 호가가 잠깐 얇은 것은 흔하다)."""
        key = "skipped" if for_day else "thin"
        sk = self.day.setdefault(key, {})
        if code not in sk:
            sk[code] = why
            self.log("INFO", f"{code} 진입 보류 — {why}" + ("" if for_day else " (계속 봅니다)"))

    def _buy(self, q: Dict[str, Any], now: datetime) -> None:
        code = q["code"]
        fill = walk_buy(q, self.params.positionKrw)
        if not fill:
            self._skip(code, f"매도호가 10단계 잔량으로 {self.params.positionKrw:,.0f}원을 채우지 못함 (호가 얇음)", False)
            return
        qty, px = fill
        if not vi_room_ok(q, px, self.params):
            self._skip(code, f"정적 VI 상단 {q['viUp']:,}원까지 목표 익절 +{self.params.targetPct}% 여유가 없음", True)
            return
        px = round(px, 2)
        ratio = imbalance(q)
        self.positions[code] = {"code": code, "name": q.get("name") or code, "entry": float(px), "qty": qty,
                                "remaining": qty, "peak": float(q["price"]), "low": float(q["price"]),
                                "partialDone": False, "pnl": 0.0, "at": now.strftime("%H:%M:%S"),
                                "atTs": now.timestamp(), "firstEntry": float(px), "cost": qty * px,
                                "timeOk": not self.params.timeStopOn, "pyramided": False, "floor": 0.0,
                                "skills": [f"호가불균형 {ratio}배"] if self.params.imbalanceOn and ratio else []}
        self.day.setdefault("entered", []).append(code)      # 같은 날 같은 종목은 다시 사지 않는다
        self._record(code, q.get("name"), "BUY", px, qty, 0.0, 0.0,
                     f"시가 {q['open']:,} 돌파 +{(q['price'] / q['open'] - 1) * 100:.2f}% (진입 기준 +{self.params.breakoutPct}%)",
                     now)
        self.log("INFO", f"모의 매수 {q.get('name')}({code}) {qty:,}주 @ {px:,}원")
        self.save()

    def _sell(self, code: str, q: Dict[str, Any], qty: int, kind: str, reason: str, now: datetime) -> None:
        pos = self.positions[code]
        px = round(walk_sell(q, qty), 2)
        cost = 0.0 if kind == "STALE" else self.params.costPct      # 미청산 정리는 손익 0 으로 적는다
        pnl = (px - pos["entry"]) * qty - px * qty * cost / 100.0
        pct = net_pct(pos["entry"], px, cost)
        pos["remaining"] -= qty
        pos["pnl"] += pnl
        if kind == "PARTIAL":
            pos["partialDone"] = True
        self._record(code, pos["name"], kind, px, qty, pct, pnl, reason, now)
        self.log("INFO", f"모의 매도 {pos['name']}({code}) {qty:,}주 @ {px:,}원 · {pnl:+,.0f}원 ({pct:+.2f}%) · {reason}")
        if pos["remaining"] <= 0:
            # 최대 유리 · 불리 이동 (MFE · MAE) — 매수가 대비 %, 비용 빼기 전. 손절 · 익절 값을 근거로 고치려고 남긴다
            mfe = (pos["peak"] / pos["entry"] - 1) * 100.0
            mae = (pos.get("low", pos["entry"]) / pos["entry"] - 1) * 100.0
            self.trades[-1].update(mfePct=round(mfe, 2), maePct=round(mae, 2), skills=list(pos.get("skills") or []))
            self.day.setdefault("closed", []).append({"code": code, "name": pos["name"], "pnl": round(pos["pnl"]),
                                                      "mfePct": round(mfe, 2), "maePct": round(mae, 2),
                                                      "stopped": kind == "STOP_LOSS"})
            if kind == "STOP_LOSS":
                self.day["stops"] = self.day.get("stops", 0) + 1
            del self.positions[code]
        self.save()

    def _record(self, code, name, kind, price, qty, pct, pnl, reason, now) -> None:
        self.trades.append({"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), "code": code,
                            "name": name or code, "kind": kind, "price": price, "qty": qty,
                            "pct": round(pct, 2), "pnl": round(pnl), "reason": reason})
        del self.trades[:-MAX_TRADES]

    # ── 하루 ──
    def _new_day(self, date: str) -> None:
        self.day = {"date": date, "phase": "wait", "prelist": [], "chosen": [], "selection": {}, "note": "",
                    "done": False, "closed": []}
        self.quotes = {}

    def _finish_day(self, note: str) -> None:
        d = self.day
        if d.get("done"):
            return
        d["done"], d["phase"], d["note"] = True, "done", note
        day_trades = [t for t in self.trades if t["date"] == d["date"] and t["kind"] != "BUY"]
        closed = d.get("closed") or []
        self.days.append({"date": d["date"], "chosen": [c.get("name") for c in d.get("chosen") or []],
                          "pnl": round(sum(t["pnl"] for t in day_trades)),
                          "positions": len(closed), "wins": sum(1 for c in closed if c["pnl"] > 0),
                          "stops": d.get("stops", 0), "halt": d.get("halt", ""), "note": note})
        del self.days[:-MAX_DAYS]
        self.log("INFO", f"{d['date']} 마감 — {note}")
        self.save()

    def _day_halt(self) -> str:
        """하루 손실 한도 — 걸리면 그 사유, 아니면 빈 문자열. 걸려도 들고 있는 것은 규칙대로 판다."""
        d, p = self.day, self.params
        if d.get("halt"):
            return d["halt"]
        pnl = sum(t["pnl"] for t in self.trades if t["date"] == d.get("date") and t["kind"] != "BUY")
        why = ""
        if p.maxDayStops and d.get("stops", 0) >= p.maxDayStops:
            why = f"오늘 손절 {d['stops']}번 — 신규 진입 중단"
        elif p.maxDayLossPct and pnl <= -p.seedKrw * p.maxDayLossPct / 100.0:
            why = f"오늘 손실 {pnl:+,.0f}원 (시드의 −{p.maxDayLossPct}%) — 신규 진입 중단"
        if why:
            d["halt"] = why
            self.log("WARNING", why)
            self.save()
        return why

    def _quote(self, code: str) -> Optional[Dict[str, Any]]:
        try:
            q = krx.quote(self.acc, code)
        except NamuhError as e:
            # 연속 실패의 첫 번째만 로그에 남긴다 (1~2초마다 쌓이지 않게). 화면에는 시각 · 횟수와 함께
            if not self._err_streak:
                self.log("WARNING", f"국내 시세 조회 실패 ({code}): {e}")
            self._err_streak += 1
            self.last_error = f"{datetime.now(KST).strftime('%m-%d %H:%M:%S')} · 연속 {self._err_streak}회 · {e}"
            return None
        if self._err_streak:
            self.log("INFO", f"국내 시세 조회 회복 (연속 실패 {self._err_streak}회 뒤)")
            self._err_streak = 0
            self.last_error = ""                # 회복하면 지운다 — 예전에는 한 번 난 오류가 계속 떠 있었다
        self.quotes[code] = q
        return q

    @staticmethod
    def _fresh(q: Dict[str, Any], now: datetime) -> bool:
        try:
            h, m, s = (int(x) for x in q.get("hogaTime", "").split(":"))
        except (ValueError, AttributeError):
            return False
        return abs((now - now.replace(hour=h, minute=m, second=s, microsecond=0)).total_seconds()) <= HOGA_FRESH_SEC

    def step(self, now: datetime) -> float:
        """한 번 판단하고, 다음 판단까지 쉴 초를 돌려준다. 시각을 받아서 테스트가 시계를 정한다."""
        now = now.astimezone(KST)
        date = now.strftime("%Y-%m-%d")
        if not self.enabled:
            return 30.0
        if self.day.get("date") != date:
            if self.positions:                  # 어제 정리 못 한 가상 보유 (서버가 꺼져 있었다) — 진입가로 닫는다
                for code, pos in list(self.positions.items()):
                    self._sell(code, {"price": pos["entry"], "bid": pos["entry"]}, pos["remaining"], "STALE",
                               "지난 거래일 미청산분 정리 (서버 중단 · 손익 0 처리)", now)
            self._new_day(date)
        if now.weekday() >= 5:
            return 600.0
        d, p = self.day, self.params
        if d.get("done"):
            return 600.0
        t_select, t_refine, t_open = _at(now, SELECT_AT), _at(now, REFINE_AT), _at(now, OPEN_AT)
        t_entry_end, t_close = _at(now, _hm(p.entryEnd)), _at(now, _hm(p.forceClose))
        budget = p.positionKrw
        if now < t_select:
            d["phase"] = "wait"
            return max(1.0, min(600.0, (t_select - now).total_seconds()))
        if now < t_refine and d["phase"] == "wait":
            d["phase"] = "select"
            self.log("INFO", "08:50 후보 훑기 시작 (코스피200 ∪ 코스닥150)")
            try:
                res = kr_selector.select(self.acc, krx_master.candidates(krx_master.load()), budget, p.selector(),
                                         deadline=(t_refine - timedelta(seconds=10)).timestamp(),
                                         should_stop=self._stop.is_set)
                d["prelist"], d["selection"] = res.get("prelist") or [], {k: v for k, v in res.items() if k != "prelist"}
                d["chosen"] = res.get("chosen") or []
                self.log("INFO", f"예비 목록 {len(d['prelist'])}종목 (훑은 {res.get('scanned')} · 예상체결 {res.get('withExpected')})")
            except Exception as e:
                self.log("ERROR", f"후보 훑기 실패: {e}")
            d["phase"] = "selected"
            self.save()
            return 1.0
        if now < t_open:
            if d["phase"] in ("wait", "select"):
                self._finish_day("08:50 후보 훑기를 하지 못해 오늘은 쉽니다 (서버가 늦게 떴거나 실패)")
                return 600.0
            if d["phase"] == "selected" and now >= t_refine:
                d["phase"] = "refine"
                try:
                    res = kr_selector.refine(self.acc, d["prelist"], budget, p.selector(),
                                             deadline=(t_open - timedelta(seconds=8)).timestamp(),
                                             should_stop=self._stop.is_set)
                    d["chosen"], d["selection"] = res.get("chosen") or d["chosen"], res
                except Exception as e:
                    self.log("ERROR", f"다시 거르기 실패 — 08:50 결과로 봅니다: {e}")
                d["phase"] = "ready"
                names = ", ".join(f"{c['name']}(갭 {c['gap']:+.2f}%)" for c in d["chosen"]) or "없음"
                self.log("INFO", f"오늘의 대장주: {names}")
                self.save()
            return max(1.0, min(30.0, (t_open - now).total_seconds()))
        if d["phase"] in ("wait", "select", "selected"):
            self._finish_day("장 시작 전에 종목을 고르지 못해 오늘은 쉽니다")
            return 600.0
        if not d["chosen"]:
            self._finish_day("조건에 맞는 대장주가 없어 오늘은 매매하지 않았습니다")
            return 600.0
        d["phase"] = "trade"
        if now >= t_close:
            for code in list(self.positions):
                q = self.quotes.get(code) or self._quote(code)
                if q:
                    self._sell(code, q, self.positions[code]["remaining"], "FORCE",
                               f"{p.forceClose} 강제 전량 청산", now)
            if not self.positions:
                self._finish_day(f"{p.forceClose} 청산 완료")
            return 5.0
        # 들고 있는 종목 · 아직 살 수 있는 종목만 본다 — 3종목을 다 보면 한 바퀴 4초+, 줄이면 손절이 덜 미끄러진다
        can_buy = now < t_entry_end and not d.get("halt") and len(self.positions) < p.maxStocks
        active = [c["code"] for c in d["chosen"]
                  if c["code"] in self.positions
                  or (can_buy and c["code"] not in d.get("entered", []) and c["code"] not in d.get("skipped", {}))]
        if not active and not self.positions:
            done_codes = set(d.get("entered", [])) | set(d.get("skipped", {}))
            if now >= t_entry_end or d.get("halt") or all(c["code"] in done_codes for c in d["chosen"]):
                return max(1.0, min(60.0, (t_close - now).total_seconds()))    # 09:30 마감까지 쉰다
        quotes = [q for q in (self._quote(c) for c in active) if q]
        traded = any(t["date"] == date for t in self.trades)
        # 휴장 판단은 개장 직후(09:02~09:10), 오늘 거래가 없을 때만 — 장중 한산한 종목을 휴장으로 보지 않는다
        if (t_open + timedelta(minutes=2) <= now < t_open + timedelta(minutes=10) and quotes and not traded
                and not self.positions and not any(self._fresh(q, now) for q in quotes)):
            self._finish_day("호가가 갱신되지 않습니다 — 휴장일(또는 지연 개장)로 보고 쉽니다")
            return 600.0
        for q in quotes:
            code = q["code"]
            if code in self.positions:
                self._manage(code, q, now)
            elif (now < t_entry_end and len(self.positions) < p.maxStocks
                  and code not in d.get("entered", []) and not self._day_halt() and can_enter(q, p)):
                ratio = imbalance(q)
                if p.imbalanceOn and ratio is not None and ratio < p.imbalanceMin:
                    self._skip(code, f"호가 불균형 {ratio}배 < {p.imbalanceMin}배 (매도잔량이 무겁지 않음)", False)
                    continue
                self._buy(q, now)
        return 1.0

    def _manage(self, code: str, q: Dict[str, Any], now: datetime) -> None:
        """보유 하나: 보존선 → 30초 타임컷 → 원래 청산 규칙 → 불타기 순서로 본다."""
        p, pos, px = self.params, self.positions[code], q["price"]
        pos["low"] = min(pos.get("low", pos["entry"]), px)
        pos["peak"] = max(pos["peak"], px)
        first = pos.get("firstEntry", pos["entry"])
        if pos.get("floor") and px <= pos["floor"]:
            self._sell(code, q, pos["remaining"], "FLOOR", f"불타기 뒤 수익 보존선 {pos['floor']:,.0f}원 이탈", now)
            return
        if not pos.get("timeOk", True):
            if pos["peak"] >= first * (1 + p.timeStopPct / 100.0):
                pos["timeOk"] = True
            elif now.timestamp() - pos.get("atTs", now.timestamp()) >= p.timeStopSec:
                pos.setdefault("skills", []).append("30초 타임컷")
                self._sell(code, q, pos["remaining"], "TIME",
                           f"{p.timeStopSec:.0f}초 안에 +{p.timeStopPct}% 를 못 찍음 (타임컷)", now)
                return
        act = decide_exit(pos, px, p)
        if act:
            self._sell(code, q, act["qty"], act["kind"], act["reason"], now)
            return
        if p.pyramidOn and not pos.get("pyramided") and px >= first * (1 + p.pyramidPct / 100.0) \
                and self._vol_surge(q, now):
            self._pyramid(code, q, now)

    def _vol_surge(self, q: Dict[str, Any], now: datetime) -> bool:
        """직전 판단 이후 거래 속도(주/초)가 09:00 이후 평균 속도의 pyramidVolX 배 이상인가."""
        code, vol, t = q["code"], q.get("volume") or 0, now.timestamp()
        last = self._vol_seen.get(code)
        self._vol_seen[code] = (t, vol)
        if not last or vol <= 0 or t <= last[0]:
            return False
        since_open = t - _at(now, OPEN_AT).timestamp()
        if since_open <= 0:
            return False
        recent = (vol - last[1]) / (t - last[0])
        return recent >= self.params.pyramidVolX * (vol / since_open)

    def _pyramid(self, code: str, q: Dict[str, Any], now: datetime) -> None:
        p, pos = self.params, self.positions[code]
        add_qty = int(pos["qty"] * p.pyramidAddPct / 100.0)
        invested = sum(x.get("cost", x["entry"] * x["remaining"]) for x in self.positions.values())
        fill = walk_buy(q, add_qty * (q.get("ask") or q["price"]) * 1.01) if add_qty > 0 else None
        if not fill or invested + fill[0] * fill[1] > p.seedKrw:
            pos["pyramided"] = True              # 한 번만 시도한다
            return
        n, px = min(fill[0], add_qty), fill[1]
        old_qty = pos["remaining"]
        pos["entry"] = (pos["entry"] * old_qty + px * n) / (old_qty + n)
        pos["remaining"] += n
        pos["qty"] += n
        pos["cost"] = pos.get("cost", 0) + n * px
        pos["pyramided"] = True
        pos["timeOk"] = True
        pos["floor"] = round(pos["entry"] * (1 + p.costPct / 100.0), 2)     # 비용까지 본전
        pos.setdefault("skills", []).append(f"불타기 +{n}주")
        self._record(code, pos["name"], "PYRAMID", round(px, 2), n, 0.0, 0.0,
                     f"+{p.pyramidPct}% 돌파 · 거래 몰림 → {p.pyramidAddPct:.0f}% 추가 · 보존선 {pos['floor']:,.0f}원", now)
        self.log("INFO", f"모의 불타기 {pos['name']}({code}) +{n}주 @ {px:,.0f}원 · 평단 {pos['entry']:,.0f}원")
        self.save()

    # ── 스레드 ──
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # 잠그지 않는다 — 08:50 훑기가 7분 걸리는 동안 화면(status)이 멈추면 안 된다.
                # 상태를 바꾸는 쪽은 이 스레드 하나뿐이다 (켜고 끄기는 enabled 한 칸).
                wait = self.step(datetime.now(KST))
            except Exception as e:
                logger.exception("국내 스캘퍼 오류")
                self.last_error = str(e)
                wait = 30.0
            self._stop.wait(wait)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self.load()
        except Exception as e:
            self.log("ERROR", f"저장된 상태를 읽지 못했습니다 — 새로 시작합니다: {e}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kr-scalper", daemon=True)
        self._thread.start()
        logger.info("국내 스캘퍼(모의) 시작")

    def stop(self) -> None:
        self._stop.set()

    def set_enabled(self, on: bool) -> None:
        with self.lock:
            self.enabled = bool(on)
            self.log("INFO", "켰습니다" if on else "껐습니다 (가상 보유가 있으면 다음 거래일 정리)")
            self.save()

    SKILLS = ("timeStopOn", "imbalanceOn", "pyramidOn")

    def set_skills(self, flags: Dict[str, Any]) -> None:
        """추가 스킬 켜고 끄기 (② 타임컷 · ③ 호가 불균형 · ⑥ 불타기). 들고 있는 종목에는 다음 판단부터."""
        with self.lock:
            for k in self.SKILLS:
                if k in flags and flags[k] is not None:
                    setattr(self.params, k, bool(flags[k]))
            self.log("INFO", "스킬: " + " · ".join(f"{k}={getattr(self.params, k)}" for k in self.SKILLS))
            self.save()

    def status(self) -> Dict[str, Any]:
        with self.lock:              # 판단 스레드와 겹쳐도 되도록 복사본으로 만든다
            days, trades, logs = list(self.days), list(self.trades[-60:]), list(self.logs[-40:])
            ex = [t for t in self.trades if "mfePct" in t]
            avg = (lambda k: round(sum(t[k] for t in ex) / len(ex), 2) if ex else None)
            closed_days = [x for x in days if x.get("positions")]
            total = sum(x["pnl"] for x in days)
            pos_n = sum(x.get("positions", 0) for x in days)
            wins = sum(x.get("wins", 0) for x in days)
            held = []
            for code, pos in list(self.positions.items()):
                q = self.quotes.get(code) or {}
                px = q.get("price") or pos["entry"]
                held.append({**pos, "price": px, "netPct": round(net_pct(pos["entry"], px, self.params.costPct), 2)})
            return {"enabled": self.enabled, "running": bool(self._thread and self._thread.is_alive()),
                    "paper": True, "params": asdict(self.params), "day": dict(self.day), "positions": held,
                    "trades": trades[::-1], "days": days[-30:][::-1],
                    "summary": {"days": len(days), "tradedDays": len(closed_days), "pnl": round(total),
                                "positions": pos_n, "wins": wins,
                                "winRate": round(wins / pos_n * 100, 1) if pos_n else None,
                                # 종목 매매 1건당 평균 손익 (비용 포함) — 승률보다 이게 플러스여야 한다
                                "perPosition": round(total / pos_n) if pos_n else None,
                                "avgMfePct": avg("mfePct"), "avgMaePct": avg("maePct"),
                                "closedWithMfe": len(ex)},
                    "logs": logs[::-1], "lastError": self.last_error}


scalper: Optional[Scalper] = None


def get(namuh_account=None) -> Scalper:
    global scalper
    if scalper is None:
        scalper = Scalper(namuh_account)
    elif namuh_account is not None:
        scalper.acc = namuh_account
    return scalper
