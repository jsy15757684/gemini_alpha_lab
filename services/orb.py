"""ORB (Opening Range Breakout) + VWAP 필터 — 판단만 한다. 주문은 내지 않는다.

토비 크라벨 · 앤드루 아지즈의 장 초반 돌파 기법을 KRX 정규장(09:00 개장)에 맞췄다.

  09:00 ~ 09:05   오프닝 레인지(OR) 고가·저가를 잰다
  09:05 ~ 09:25   가격이 OR 고가를 넘고 · 당일 VWAP 위이고 · 거래량이 붙으면 매수
  청산            익절 +tp% · 손절 -sl% 또는 OR 저가 이탈 · 09:30 타임컷 (하루 1회 진입)

시세는 몇 초마다 한 번 받는 스냅샷이다(분봉 API 가 없다). 그래서

  OR 고가·저가   스냅샷의 '당일 고가·저가' 를 쓴다. 장이 열린 지 5분 안에는
                 당일 고저가 곧 OR 이라, 폴링 사이에 스친 고저도 놓치지 않는다.
  거래량 급증    누적 거래량의 증가 속도(주/초)로 본다. 최근 60초 속도가
                 OR 구간 평균 속도의 vol_surge 배 이상이면 '붙었다' 로 본다.
                 09:00 시가 동시호가 체결량은 한 번에 크게 잡히므로 OR 첫
                 스냅샷을 기준점으로 삼아 뺀다.
  장 확인        호가 시각(hoga_bsop_hour)이 지금과 90초 넘게 어긋나면 그날은
                 쉰다. 휴장일 · 수능일(10시 개장)처럼 달력이 틀릴 때 막아 준다.

가짜 돌파를 거르는 세 필터 (단순 5분 ORB 의 약점 보완)

  RVOL          OR 끝(09:05) 누적 거래량 ÷ 전 거래일 같은 시각 누적 거래량 ≥ rvolMin.
                전일 값은 봇이 매일 09:05 에 나무증권 시세에서 직접 적어 둔 것이다.
                공개 분봉(야후)은 20분 늦고 같은 시각 누적이 10~20% 적게 나와,
                출처를 섞어 나누면 비율이 틀어진다(2026-09-28 실측). 그래서 기록이
                없는 첫 거래일은 **사지 않고 기록만 한다.**
  트레일링      exitMode="trailing" 이면 09:30 타임컷 대신 고점에서 trailPct 만큼
                내려오면 판다. 늦어도 finalCutMin(15:15) 에는 판다 — 종가 동시호가
                전에 끝내고 밤을 넘기지 않는다. 고정 익절은 쓰지 않는다(추세를 끊는다).
  지수          돌파 순간 지수 ETF(코스피=KODEX 200 · 코스닥=KODEX 코스닥150)가
                당일 시가 아래면 사지 않는다. 나무증권에서 지수 시세 경로를 찾지
                못해 지수를 따라가는 ETF 로 본다.

갭 앤 고(Gap and Go)에서 가져온 두 조건 — OR 이 끝난 첫 판단에서 RVOL 과 같이 한 번 본다

  시초가 갭      실제 시가가 전일 종가보다 gapMinPct ~ gapMaxPct 위에서 열린 날만.
                자동 선정은 08:48 예상체결로 고르는데, 동시호가는 09:00 직전까지
                취소할 수 있어 예상 갭과 실제 갭이 다르다. 실제 시가로 다시 거른다.
                상한을 두면 시가 대비 +10% 정적 VI 에 닿을 일도 줄어든다.
  시초가 지지    첫 5분의 1분 종가가 모두 시가 이상이고 5분 끝 가격이 시가 위(양봉)인
                날만. 갭을 메우며 밀리는 종목을 거른다. 저가(꼬리)로 보면 너무 엄격하다
                — 개장 첫 1분은 거의 늘 시가 아래를 한 번 찍는다(2026-08~09 1분봉:
                갭 2~5% 53건 중 저가 기준 1건 · 종가 기준 12건 통과). 1분 종가는
                그 분의 마지막 스냅샷 가격이다.
"""

from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta
from typing import Any, Deque, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
OPEN = dtime(9, 0)
FRESH_SEC = 90          # 호가 시각이 이만큼 어긋나면 장이 안 열린 것으로 본다
SURGE_WINDOW_SEC = 60   # 거래량 급증을 보는 최근 구간
MIN_SPAN_SEC = 20       # 속도를 계산하려면 이만큼은 떨어진 스냅샷이 필요하다


@dataclass
class OrbParams:
    rangeMin: int = 5            # 09:00 ~ 09:05
    entryEndMin: int = 25        # 09:25 까지만 진입
    cutoffMin: int = 30          # 09:30 타임컷
    takeProfitPct: float = 2.0
    stopLossPct: float = 1.0
    volSurge: float = 1.5        # 최근 거래 속도 ≥ OR 평균 속도 × 이 값
    useVwap: bool = True
    useOrLowStop: bool = True
    rvolMin: float = 2.0         # 전일 같은 시각 대비 거래량 배수 (0 이면 끔)
    exitMode: str = "timecut"    # "timecut" | "trailing"
    trailPct: float = 1.0        # 트레일링: 고점 대비 이만큼 빠지면 판다
    finalCutMin: int = 375       # 트레일링 최종 청산 (09:00 + 375분 = 15:15)
    marketFilter: bool = True    # 지수 ETF 가 당일 시가 아래면 사지 않는다
    gapMinPct: float = 2.0       # 실제 시초가 갭 하한 (하한 · 상한 둘 다 0 이면 끔)
    gapMaxPct: float = 5.0       # 상한
    openHold: bool = True        # 첫 5분 1분 종가가 모두 시가 이상 + 5분 양봉

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "OrbParams":
        d = d or {}
        p = cls()
        for k in asdict(p):
            if k in d and d[k] is not None:
                setattr(p, k, type(getattr(p, k))(d[k]))
        p.validate()
        return p

    def validate(self) -> None:
        if not (1 <= self.rangeMin < self.entryEndMin < self.cutoffMin <= 390):
            raise ValueError("ORB 시간 설정은 레인지 < 진입 마감 < 타임컷 순이어야 합니다.")
        if not (0.1 <= self.takeProfitPct <= 20 and 0.1 <= self.stopLossPct <= 20):
            raise ValueError("익절·손절은 0.1% ~ 20% 사이여야 합니다.")
        if not (0 <= self.volSurge <= 10):
            raise ValueError("거래량 배수는 0 ~ 10 사이여야 합니다 (0 이면 거래량을 보지 않는다).")
        if not (0 <= self.rvolMin <= 20):
            raise ValueError("RVOL 배수는 0 ~ 20 사이여야 합니다 (0 이면 끔).")
        if self.exitMode not in ("timecut", "trailing"):
            raise ValueError("청산 방식은 timecut 또는 trailing 이어야 합니다.")
        if not (0.1 <= self.trailPct <= 20):
            raise ValueError("트레일링 폭은 0.1% ~ 20% 사이여야 합니다.")
        if self.gap_on and not (-30 <= self.gapMinPct < self.gapMaxPct <= 30):
            raise ValueError("시초가 갭 범위가 올바르지 않습니다 (하한 < 상한, ±30% 안 · 둘 다 0 이면 끔).")
        if not (self.entryEndMin < self.finalCutMin <= 380):
            raise ValueError("트레일링 최종 청산은 진입 마감 뒤 ~ 15:20 사이여야 합니다 (종가 동시호가 전).")

    @property
    def gap_on(self) -> bool:
        return bool(self.gapMinPct or self.gapMaxPct)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def open_gap_pct(q: Dict[str, Any]) -> Optional[float]:
    """실제 시가의 갭(%). 실시간 체결에는 전일 종가가 없어 등락률로 되돌려 낸다."""
    op, price = float(q.get("open") or 0), float(q.get("price") or 0)
    prev = float(q.get("prevClose") or 0)
    if not prev and price > 0 and q.get("changePct") is not None:
        prev = price / (1 + float(q["changePct"]) / 100)
    if op <= 0 or prev <= 0:
        return None
    return (op / prev - 1) * 100


@dataclass
class OrbDay:
    """하루치 상태. 날짜가 바뀌면 새로 만든다."""
    date: str
    orHigh: Optional[float] = None
    orLow: Optional[float] = None
    orVol0: Optional[Tuple[float, int]] = None      # OR 첫 스냅샷 (초, 누적거래량)
    orVol1: Optional[Tuple[float, int]] = None      # OR 마지막 스냅샷
    stale: bool = False                              # 장이 안 열렸다고 판단
    rvol: Optional[float] = None                     # OR 끝 누적 거래량 ÷ 전일 같은 시각
    rvolChecked: bool = False
    orMinClose: Optional[float] = None               # OR 구간 1분 종가 중 가장 낮은 값 (시초가 지지)
    minute: Optional[int] = None                     # 지금 담고 있는 1분 칸 (개장 뒤 몇 분째)
    minuteLast: Optional[float] = None               # 그 칸의 마지막 가격 = 그 분의 종가
    entered: bool = False
    done: bool = False
    note: str = ""
    samples: Deque[Tuple[float, int]] = field(default_factory=lambda: deque(maxlen=120))

    def to_dict(self) -> Dict[str, Any]:
        return {"date": self.date, "orHigh": self.orHigh, "orLow": self.orLow,
                "orVolEnd": self.orVol1[1] if self.orVol1 else None, "rvol": self.rvol,
                # 재시작 뒤 거래 속도(OR 평균)를 다시 낼 수 있게 두 점을 다 남긴다
                "orVol0": list(self.orVol0) if self.orVol0 else None,
                "orVol1": list(self.orVol1) if self.orVol1 else None,
                "rvolChecked": self.rvolChecked, "orMinClose": self.orMinClose,
                "minute": self.minute, "minuteLast": self.minuteLast,
                "stale": self.stale, "entered": self.entered, "done": self.done, "note": self.note}

    def close_minute(self) -> None:
        """담고 있던 1분 칸을 닫는다 — 그 분의 마지막 가격을 1분 종가로 친다."""
        if self.minuteLast is not None:
            self.orMinClose = min(self.orMinClose if self.orMinClose is not None else float("inf"),
                                  self.minuteLast)

    @property
    def or_rate(self) -> Optional[float]:
        if not (self.orVol0 and self.orVol1):
            return None
        dt = self.orVol1[0] - self.orVol0[0]
        if dt < MIN_SPAN_SEC:
            return None
        return max(0.0, (self.orVol1[1] - self.orVol0[1]) / dt)

    def recent_rate(self, now_s: float) -> Optional[float]:
        pts = [p for p in self.samples if now_s - p[0] <= SURGE_WINDOW_SEC]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < MIN_SPAN_SEC:
            return None
        return max(0.0, (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]))


def minutes_since_open(now: datetime) -> float:
    n = now.astimezone(KST)
    return (n - n.replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)).total_seconds() / 60.0


def _hoga_fresh(hoga: str, now: datetime) -> bool:
    try:
        h, m, s = (int(x) for x in hoga.split(":"))
    except (ValueError, AttributeError):
        return False
    n = now.astimezone(KST)
    t = n.replace(hour=h, minute=m, second=s, microsecond=0)
    return abs((n - t).total_seconds()) <= FRESH_SEC


def hm(minutes: int) -> str:
    return f"{9 + minutes // 60:02d}:{minutes % 60:02d}"


def decide(p: OrbParams, day: OrbDay, q: Dict[str, Any], now: datetime,
           holding: Optional[Dict[str, Any]], index: Optional[Dict[str, Any]] = None,
           prev_or_vol: Optional[int] = None) -> Dict[str, Any]:
    """스냅샷 하나로 판단한다.

    holding      None 이면 무포지션. 있으면 {"entryPrice", "date", "peak"}.
    index        지수 ETF 시세 {"name", "price", "open"} — marketFilter 에 쓴다
    prev_or_vol  전 거래일 OR 끝 누적 거래량 — RVOL 에 쓴다
    반환         {"action": "buy"|"sell"|None, "reason", "phase"}
    """
    m = minutes_since_open(now)
    now_s = now.timestamp()
    price = float(q["price"])
    date = now.astimezone(KST).strftime("%Y-%m-%d")

    fresh = _hoga_fresh(q.get("hogaTime", ""), now)

    # ── 들고 있으면 청산부터 본다 ──
    if holding:
        entry = float(holding["entryPrice"])
        ret = (price / entry - 1) * 100 if entry > 0 else 0.0
        if not fresh or m < 0:
            # 장이 안 열린 때 낸 매도는 거부되거나 시가 동시호가에 묶인다.
            return {"action": None, "phase": "holding",
                    "reason": f"보유 중 {ret:+.2f}% · 장이 열리면 청산을 판단합니다"}
        if holding.get("date") and holding["date"] != date and m >= 0:
            return {"action": "sell", "phase": "exit",
                    "reason": f"⏰ 전날({holding['date']}) 포지션 — 오늘 장 시작에 정리 ({ret:+.2f}%)"}
        if p.exitMode == "trailing":
            if m >= p.finalCutMin:
                return {"action": "sell", "phase": "exit",
                        "reason": f"⏰ {hm(p.finalCutMin)} 최종 청산 — 밤을 넘기지 않는다 ({ret:+.2f}%)"}
            peak = max(float(holding.get("peak") or entry), price)
            stop = peak * (1 - p.trailPct / 100)
            if price <= stop and peak > entry:
                return {"action": "sell", "phase": "exit",
                        "reason": f"📉 트레일링 — 고점 {peak:,.0f}원에서 -{p.trailPct}% ({stop:,.0f}원) 이탈 ({ret:+.2f}%)"}
        else:
            if m >= p.cutoffMin:
                return {"action": "sell", "phase": "exit",
                        "reason": f"⏰ {hm(p.cutoffMin)} 타임컷 ({ret:+.2f}%)"}
            if ret >= p.takeProfitPct:
                return {"action": "sell", "phase": "exit", "reason": f"🎯 익절 +{ret:.2f}% (목표 +{p.takeProfitPct}%)"}
        if ret <= -p.stopLossPct:
            return {"action": "sell", "phase": "exit", "reason": f"🛑 손절 {ret:.2f}% (한도 -{p.stopLossPct}%)"}
        if p.useOrLowStop and day.orLow and price < day.orLow:
            return {"action": "sell", "phase": "exit",
                    "reason": f"🛑 OR 저가 {day.orLow:,.0f}원 이탈 ({price:,.0f}원 · {ret:+.2f}%)"}
        if p.exitMode == "trailing":
            peak = max(float(holding.get("peak") or entry), price)
            return {"action": None, "phase": "holding",
                    "reason": f"보유 중 {ret:+.2f}% · 고점 {peak:,.0f} · 스탑 "
                              f"{max(peak * (1 - p.trailPct / 100), entry * (1 - p.stopLossPct / 100)):,.0f}원"}
        return {"action": None, "phase": "holding", "reason": f"보유 중 {ret:+.2f}%"}

    if m < 0:
        return {"action": None, "phase": "pre", "reason": "개장 전"}
    if day.done or m >= (p.finalCutMin if p.exitMode == "trailing" else p.cutoffMin):
        day.done = True
        return {"action": None, "phase": "done", "reason": day.note or "오늘 ORB 종료"}

    if m < p.rangeMin:
        if not fresh:
            day.stale = True
            return {"action": None, "phase": "range", "reason": "호가 시각이 멈춰 있음 — 장이 열리지 않은 것으로 봅니다"}
        day.stale = False
        # 개장 5분 안에는 당일 고저가 곧 OR 이다. 폴링 사이에 스친 값도 담긴다.
        day.orHigh = max(day.orHigh or 0, float(q.get("high") or price))
        day.orLow = min(day.orLow or float("inf"), float(q.get("low") or price))
        vol = int(q.get("volume") or 0)
        if day.orVol0 is None:
            day.orVol0 = (now_s, vol)
        day.orVol1 = (now_s, vol)
        day.samples.append((now_s, vol))
        k = int(m)
        if day.minute is not None and k != day.minute:
            day.close_minute()
        day.minute, day.minuteLast = k, price
        return {"action": None, "phase": "range",
                "reason": f"OR 측정 중 · 고 {day.orHigh:,.0f} · 저 {day.orLow:,.0f}"}

    # ── 진입 구간 ──
    if day.stale or day.orHigh is None:
        day.done = True
        day.note = "OR 을 재지 못해 오늘은 쉽니다 (휴장 · 지연 개장 · 기동 지연)"
        return {"action": None, "phase": "done", "reason": day.note}
    if day.entered:
        day.done = True
        day.note = "오늘 진입을 이미 했습니다 (하루 1회)"
        return {"action": None, "phase": "done", "reason": day.note}
    if not fresh:
        return {"action": None, "phase": "watch", "reason": "호가가 멈춰 있어 판단을 미룹니다"}

    # ── OR 이 끝난 첫 판단에서 한 번만 본다: 시초가 갭 · 시초가 지지 · RVOL ──
    if not day.rvolChecked:
        day.rvolChecked = True
        op = float(q.get("open") or 0)
        if p.gap_on:
            gap = open_gap_pct(q)
            if gap is None:
                day.done = True
                day.note = "시가 · 전일 종가를 몰라 시초가 갭을 볼 수 없습니다 — 오늘은 쉽니다"
                return {"action": None, "phase": "done", "reason": day.note}
            if not (p.gapMinPct <= gap <= p.gapMaxPct):
                day.done = True
                day.note = f"시초가 갭 {gap:+.2f}% — 범위 {p.gapMinPct:+g}~{p.gapMaxPct:+g}% 밖이라 쉽니다"
                return {"action": None, "phase": "done", "reason": day.note}
        if p.openHold:
            if op <= 0:
                day.done = True
                day.note = "시가를 몰라 시초가 지지를 볼 수 없습니다 — 오늘은 쉽니다"
                return {"action": None, "phase": "done", "reason": day.note}
            day.close_minute()                           # 마지막 1분(09:04~09:05)도 넣는다
            if day.orMinClose is not None and day.orMinClose < op:
                day.done = True
                day.note = (f"첫 {p.rangeMin}분 중 1분 종가가 시가 {op:,.0f}원 아래({day.orMinClose:,.0f}원)로 "
                            f"밀렸습니다 — 시초가 지지 실패라 쉽니다")
                return {"action": None, "phase": "done", "reason": day.note}
            if price <= op:
                day.done = True
                day.note = f"{hm(p.rangeMin)} 가격 {price:,.0f}원이 시가 {op:,.0f}원 이하(음봉) — 쉽니다"
                return {"action": None, "phase": "done", "reason": day.note}
        if p.rvolMin > 0:
            end_vol = day.orVol1[1] if day.orVol1 else 0
            if not prev_or_vol:
                day.done = True
                day.note = (f"전 거래일 {hm(p.rangeMin)} 거래량 기록이 없어 RVOL 을 볼 수 없습니다 — "
                            f"오늘은 사지 않고 기록만 합니다 (누적 {end_vol:,}주)")
                return {"action": None, "phase": "done", "reason": day.note}
            day.rvol = end_vol / prev_or_vol
            if day.rvol < p.rvolMin:
                day.done = True
                day.note = (f"RVOL {day.rvol * 100:.0f}% < {p.rvolMin * 100:.0f}% — 거래가 안 붙은 날이라 쉽니다 "
                            f"(오늘 {end_vol:,} · 전일 {prev_or_vol:,}주)")
                return {"action": None, "phase": "done", "reason": day.note}

    day.samples.append((now_s, int(q.get("volume") or 0)))
    if m >= p.entryEndMin:
        day.done = True
        day.note = f"09:{p.entryEndMin:02d} 까지 돌파가 없어 오늘은 쉽니다"
        return {"action": None, "phase": "done", "reason": day.note}

    vwap = float(q.get("vwap") or 0)
    checks = [price > day.orHigh]
    why = [f"가격 {price:,.0f} {'>' if checks[0] else '≤'} OR고 {day.orHigh:,.0f}"]
    if p.useVwap:
        ok = vwap > 0 and price > vwap
        checks.append(ok)
        why.append(f"VWAP {vwap:,.0f} {'위' if ok else '아래'}")
    if p.volSurge > 0:
        rate, base = day.recent_rate(now_s), day.or_rate
        ok = bool(rate is not None and base and rate >= base * p.volSurge)
        checks.append(ok)
        why.append(f"거래속도 {rate or 0:,.0f}/초 vs OR {base or 0:,.0f}×{p.volSurge:g}"
                   if base else "거래속도 계산 대기")
    if p.marketFilter:
        ok = bool(index and index.get("price") and index.get("open") and index["price"] >= index["open"])
        checks.append(ok)
        why.append(f"{index.get('name', '지수')} {'시가 위' if ok else '시가 아래 — 하락 압력'}"
                   if index and index.get("open") else "지수 시세 없음 — 사지 않음")
    if day.rvol is not None:
        why.append(f"RVOL {day.rvol * 100:.0f}%")
    if all(checks):
        day.entered = True
        return {"action": "buy", "phase": "entry", "reason": "🚀 ORB 돌파 · " + " · ".join(why)}
    return {"action": None, "phase": "watch", "reason": "돌파 대기 · " + " · ".join(why)}


def next_wake(p: OrbParams, now: datetime, holding: bool, done: bool = False) -> float:
    """다음 판단까지 쉴 초. 장 초반만 촘촘히 본다 — 호출 한도를 아낀다.

    주말은 건너뛴다. 휴장일은 OR 을 못 재면 그날을 끝내므로(done) 그 뒤로는
    내일까지 잔다. 들고 있으면 늘 촘촘히 본다(손절이 먼저다).
    """
    m = minutes_since_open(now)
    n = now.astimezone(KST)
    if holding:
        # 장 초반은 2초, 트레일링으로 오후까지 들고 가면 5초 — 하루 종일 2초면
        # 해외 봇과 같이 쓰는 호출 한도를 혼자 다 쓴다.
        if -1.0 <= m < p.cutoffMin + 1:
            return 2.0
        return 5.0 if -1.0 <= m < 390 else 30.0
    if n.weekday() < 5 and not done and -1.0 <= m < p.cutoffMin + 1:
        return 2.0
    if n.weekday() < 5 and not done and m < -1.0:
        return min(600.0, max(2.0, (-1.0 - m) * 60))     # 08:59 까지 (10분씩 끊어서)
    if m < -1.0:
        return min(30.0, max(2.0, (-1.0 - m) * 60))
    # 오늘은 끝났다 → 내일 08:59 까지 (길게 자지 않고 10분씩 끊어 정지 요청을 받는다)
    tomorrow = (n + timedelta(days=1)).replace(hour=8, minute=59, second=0, microsecond=0)
    return min(600.0, (tomorrow - n).total_seconds())
