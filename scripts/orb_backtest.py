#!/usr/bin/env python3
"""장 초반 돌파 전략 비교 백테스트 — ① ORB · ①+② ORB+갭 앤 고 · ③ 조기 변동성 돌파.

데이터   scripts/orb_bt_fetch.py 가 서버에서 받은 나무증권 1분봉 · 일봉 (data/bt_cache)
         분봉은 2026-08-13 부터만 있다 → 약 6주. 표본이 작다는 것을 늘 같이 본다.

감시 목록 (세 전략 공통 — 진입 · 청산 규칙만 비교한다)
  스캐너의 매일 자동 선정을 흉내 낸다. 장 전 예상체결 기록은 과거에 없으므로
  실제 시가와 09:01 봉(시가 동시호가 체결 + 첫 1분) 거래량으로 대신한다.
    거르기  1주 값 ≤ 종목당 자본 · 갭 +1~+15% · 시가 × 09:01 거래량 ≥ 3억
    순위    09:01 거래량 ÷ 전일 거래량 → 상위 28
  실전(08:48 예상체결)보다 정보가 정확하다 → 선정은 실전보다 조금 유리하게 나온다.

체결 (보수적으로)
  1분봉 안의 순서는 모른다. 같은 봉에서 손절과 익절이 둘 다 닿으면 손절로 본다.
  ①·②  돌파는 '봉 종가' 로 확인하고 다음 봉 시가에 산다 (윗꼬리 돌파는 거른다).
  ③      목표가는 시가에 이미 정해지므로 닿는 순간 산다: max(봉 시가, 목표가).
  매수 +1틱 · 매도 −1틱 슬리피지, 수수료 0.015% × 2, 매도세 0.20%.
  VI(2분 단일가)는 흉내 내지 못한다 — 봉 시가가 손절선 아래면 그 시가에 판다.

포트폴리오  종목당 100만 원 · 동시 3종목(스캐너와 같다) · 신호가 먼저 난 순서.

  python3 scripts/orb_backtest.py [data/bt_cache] [--json out.json]
"""

import gzip
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

FEE = 0.015 / 100
TAX = 0.20 / 100
SLIP = 1                    # 틱
SLOT = 1_000_000
SLOTS = 3
TOP_N = 28
WARMUP = 5                  # 앞 5거래일은 기준(전일 거래량 등)만 쌓는다
INDEX = {"kospi": "069500", "kosdaq": "229200"}


# ── 호가 단위 (services/krx.py 와 같다 · 이 스크립트만 따로 돌리려고 옮겨 둔다) ──
def tick_size(p: float) -> int:
    for bound, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if p < bound:
            return t
    return 1000


def shift(p: float, n: int) -> float:
    step = 1 if n > 0 else -1
    for _ in range(abs(n)):
        p += step * tick_size(p if step > 0 else p - 1)
    return p


def ceil_tick(p: float) -> float:
    t = tick_size(p)
    return math.ceil(p / t - 1e-9) * t


def floor_tick(p: float) -> float:
    t = tick_size(p)
    return math.floor(p / t + 1e-9) * t


@dataclass
class Spec:
    name: str
    kind: str = "orb"               # orb | vb
    # ORB
    rangeMin: int = 5
    entryEnd: int = 25
    tp: Optional[float] = 2.0       # None = 익절 없음
    sl: Optional[float] = 1.0       # None = 손절 없음
    orLowStop: bool = True
    volSurge: float = 1.5
    vwap: bool = True
    rvolMin: float = 2.0
    market: bool = True
    exit: str = "timecut"           # timecut(09:30) | eod(15:15)
    entry: str = "close"            # close = 봉 종가 확인 뒤 다음 봉 시가 · touch = 돌파 순간 (실시간 봇과 같다)
    # ② 갭 앤 고에서 가져온 것
    gapGate: Optional[tuple] = None  # 실제 시가 갭 범위 (%)
    openHold: bool = False           # 첫 5분 1분 종가가 모두 시가 이상 + 5분 양봉
    # ③
    k: float = 0.25
    vbEnd: int = 10
    selGap: tuple = (1, 15)          # 감시 목록을 고를 때의 갭 범위 (스캐너 자동 선정)


# ── 데이터 ──
def load(dirpath: str):
    meta = json.load(open(os.path.join(dirpath, "meta.json"), encoding="utf-8"))
    data = {}
    for fn in os.listdir(dirpath):
        if not fn.endswith(".json.gz"):
            continue
        d = json.load(gzip.open(os.path.join(dirpath, fn), "rt", encoding="utf-8"))
        bars = {day: {b[0]: b[1:] for b in rows} for day, rows in d["m1"].items()}
        data[d["code"]] = {"daily": {x["d"]: x for x in d["daily"]}, "m1": bars}
    return meta, data


def minutes(start: str = "0901", end: str = "1530") -> List[str]:
    out, h, m = [], int(start[:2]), int(start[2:])
    while f"{h:02d}{m:02d}" <= end:
        out.append(f"{h:02d}{m:02d}")
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


MIN = minutes()


def hhmm(offset_min: int) -> str:
    t = 9 * 60 + offset_min
    return f"{t // 60:02d}{t % 60:02d}"


def prev_daily(daily: Dict[str, dict], day: str) -> Optional[dict]:
    ks = [k for k in daily if k < day]
    return daily[max(ks)] if ks else None


# ── 감시 목록 ──
def watchlist(meta, data, day: str, sel_gap=(1, 15)) -> List[dict]:
    rows = []
    for code, d in data.items():
        if meta.get(code, {}).get("market") == "index" or day not in d["m1"]:
            continue
        bars, pd_ = d["m1"][day], prev_daily(d["daily"], day)
        if "0901" not in bars or not pd_ or pd_["c"] <= 0 or pd_["v"] <= 0:
            continue
        op = d["daily"].get(day, {}).get("o") or bars["0901"][0]
        gap = (op / pd_["c"] - 1) * 100
        v0 = bars["0901"][4]
        if op * 1.01 > SLOT or not (sel_gap[0] <= gap <= sel_gap[1]) or op * v0 < 3e8:
            continue
        rows.append({"code": code, "open": op, "gap": gap, "prev": pd_, "score": v0 / pd_["v"]})
    rows.sort(key=lambda r: -r["score"])
    return rows[:TOP_N]


def prev_or_vol(d, day: str, range_min: int) -> Optional[int]:
    """전 거래일 09:00~09:05 누적 거래량 (스캐너의 RVOL 분모와 같다 — 하루치)."""
    ks = [k for k in d["m1"] if k < day]
    if not ks:
        return None
    bars = d["m1"][max(ks)]
    return sum(bars[t][4] for t in minutes("0901", hhmm(range_min)) if t in bars) or None


# ── 한 종목 · 하루 ──
def _exit_scan(s: Spec, bars, start_i: int, entry: float, stop: Optional[float], tp_px: Optional[float],
               touch_bar: bool = False):
    last = hhmm(30) if s.exit == "timecut" else "1515"
    for t in MIN[start_i:]:
        if t not in bars:
            continue
        o, h, l, c = bars[t][:4]
        # 닿는 순간 산 봉(③)은 봉 안에서 산 시각 앞뒤를 모른다. 그 봉의 저가는 대개
        # 산 '앞' 에 찍힌 것이라 손절로 세면 안 된다 → 그 봉은 종가로만 판단한다.
        first = touch_bar and t == MIN[start_i]
        if stop is not None and (c <= stop if first else l <= stop):
            px = stop if first else (o if o <= stop else stop)
            return t, shift(px, -SLIP), "손절"
        if tp_px is not None and h >= tp_px and (not first or c >= tp_px):
            px = o if (o >= tp_px and not first) else tp_px
            return t, shift(px, -SLIP), "익절"
        if t >= last:
            return t, shift(c, -SLIP), "타임컷" if s.exit == "timecut" else "종가 청산"
    t = max(k for k in bars)
    return t, shift(bars[t][3], -SLIP), "데이터 끝"


def sim_orb(s: Spec, d, idx_bars, day: str, w: dict) -> Optional[dict]:
    bars = d["m1"][day]
    op = w["open"]
    rng = [t for t in minutes("0901", hhmm(s.rangeMin)) if t in bars]
    if len(rng) < s.rangeMin - 1:
        return None
    orH = max(bars[t][1] for t in rng)
    orL = min(min(bars[t][2] for t in rng), op)
    orH = max(orH, op)
    if s.gapGate and not (s.gapGate[0] <= w["gap"] <= s.gapGate[1]):
        return None
    # 시초가 지지: OR 구간 1분 종가가 모두 시가 이상 + 5분 끝 종가가 시가 위 (services/orb.py 와 같다)
    if s.openHold and not (min(bars[t][3] for t in rng) >= op and bars[rng[-1]][3] > op):
        return None
    if s.rvolMin > 0:
        base = prev_or_vol(d, day, s.rangeMin)
        if not base or sum(bars[t][4] for t in rng) / base < s.rvolMin:
            return None
    # OR 평균 속도 — 첫 봉(시가 동시호가 체결이 한 번에 잡힌다)은 뺀다 (스캐너와 같다)
    rate_bars = [bars[t][4] for t in rng[1:]] or [bars[rng[0]][4]]
    or_rate = sum(rate_bars) / len(rate_bars)
    idx_open = idx_bars["0901"][0] if idx_bars and "0901" in idx_bars else None
    cum_v = cum_a = 0.0
    for t in rng:
        o, h, l, c, v, a = bars[t]
        cum_v += v
        cum_a += a if a > 0 else (h + l + c) / 3 * v
    end_i = MIN.index(hhmm(s.entryEnd))
    prev_bar, prev_vwap, prev_t = bars[rng[-1]], (cum_a / cum_v if cum_v else 0), rng[-1]
    for i in range(MIN.index(rng[-1]) + 1, end_i + 1):
        t = MIN[i]
        if t not in bars:
            continue
        o, h, l, c, v, a = bars[t]
        if s.entry == "touch":
            # 돌파 순간에 산다. 그 순간에 아는 것은 앞 봉까지다 (VWAP · 거래속도 · 지수).
            ok = h > orH
            ok = ok and (not s.vwap or orH > prev_vwap)
            ok = ok and (s.volSurge <= 0 or prev_bar[4] >= or_rate * s.volSurge)
            if s.market:
                ib = idx_bars.get(prev_t) if idx_bars else None
                ok = ok and bool(ib and idx_open and ib[3] >= idx_open)
            cum_v += v
            cum_a += a if a > 0 else (h + l + c) / 3 * v
            if ok:
                entry = shift(max(o, shift(orH, 1)), SLIP)
                stops = ([entry * (1 - s.sl / 100)] if s.sl is not None else []) + \
                        ([shift(orL, -1)] if s.orLowStop else [])
                stop = floor_tick(max(stops)) if stops else None
                tp_px = ceil_tick(entry * (1 + s.tp / 100)) if (s.tp is not None and s.exit == "timecut") else None
                xt, xp, why = _exit_scan(s, bars, i, entry, stop, tp_px, touch_bar=True)
                return {"in": t, "out": xt, "entry": entry, "exit": xp, "why": why}
            prev_bar, prev_vwap, prev_t = bars[t], (cum_a / cum_v if cum_v else 0), t
            continue
        cum_v += v
        cum_a += a if a > 0 else (h + l + c) / 3 * v
        vwap = cum_a / cum_v if cum_v else 0
        ok = c > orH
        ok = ok and (not s.vwap or c > vwap)
        ok = ok and (s.volSurge <= 0 or v >= or_rate * s.volSurge)
        if s.market:
            ib = idx_bars.get(t) if idx_bars else None
            ok = ok and bool(ib and idx_open and ib[3] >= idx_open)
        if not ok:
            continue
        # 다음 봉 시가에 산다
        j = next((k for k in range(i + 1, len(MIN)) if MIN[k] in bars), None)
        if j is None or MIN[j] > hhmm(s.entryEnd + 1):
            return None
        entry = shift(bars[MIN[j]][0], SLIP)
        stops = []
        if s.sl is not None:
            stops.append(entry * (1 - s.sl / 100))
        if s.orLowStop:
            stops.append(shift(orL, -1))
        stop = floor_tick(max(stops)) if stops else None
        tp_px = ceil_tick(entry * (1 + s.tp / 100)) if (s.tp is not None and s.exit == "timecut") else None
        xt, xp, why = _exit_scan(s, bars, j, entry, stop, tp_px)
        return {"in": MIN[j], "out": xt, "entry": entry, "exit": xp, "why": why}
    return None


def sim_vb(s: Spec, d, day: str, w: dict) -> Optional[dict]:
    bars, pd_ = d["m1"][day], w["prev"]
    op = w["open"]
    target = ceil_tick(op + s.k * (pd_["h"] - pd_["l"]))
    if target <= op:
        return None
    for i, t in enumerate(MIN):
        if t > hhmm(s.vbEnd):
            return None
        if t not in bars or bars[t][1] < target:
            continue
        entry = shift(max(bars[t][0], target), SLIP)
        stop = floor_tick(entry * (1 - s.sl / 100)) if s.sl is not None else None
        tp_px = ceil_tick(entry * (1 + s.tp / 100)) if s.tp is not None else None
        xt, xp, why = _exit_scan(s, bars, i, entry, stop, tp_px, touch_bar=True)
        return {"in": t, "out": xt, "entry": entry, "exit": xp, "why": why}
    return None


def net_pct(entry: float, exit_: float) -> float:
    return ((exit_ * (1 - FEE - TAX)) / (entry * (1 + FEE)) - 1) * 100


# ── 돌리기 ──
def run(meta, data, specs: List[Spec]):
    days = sorted(data[INDEX["kospi"]]["m1"])
    test_days = days[WARMUP:]
    out = {}
    for s in specs:
        trades, daily_pnl = [], {}
        for day in test_days:
            cands = []
            for w in watchlist(meta, data, day, s.selGap):
                d = data[w["code"]]
                mk = meta.get(w["code"], {}).get("market", "kospi")
                idx = data.get(INDEX.get(mk, INDEX["kospi"]), {}).get("m1", {}).get(day)
                r = sim_orb(s, d, idx, day, w) if s.kind == "orb" else sim_vb(s, d, day, w)
                if r:
                    r.update(code=w["code"], name=meta.get(w["code"], {}).get("name", w["code"]),
                             day=day, gap=round(w["gap"], 2), score=w["score"],
                             net=net_pct(r["entry"], r["exit"]))
                    cands.append(r)
            # 슬롯 3개 — 먼저 신호가 난 종목부터. 판 뒤에는 슬롯이 빈다.
            cands.sort(key=lambda r: (r["in"], -r["score"]))
            held: List[dict] = []
            pnl = 0.0
            for r in cands:
                held = [h for h in held if h["out"] < r["in"]]
                if len(held) >= SLOTS:
                    continue
                qty = int(SLOT // (r["entry"] * (1 + FEE)))
                if qty < 1:
                    continue
                r["qty"] = qty
                r["pnl"] = qty * r["exit"] * (1 - FEE - TAX) - qty * r["entry"] * (1 + FEE)
                held.append(r)
                trades.append(r)
                pnl += r["pnl"]
            daily_pnl[day] = pnl
        out[s.name] = {"spec": s, "trades": trades, "daily": daily_pnl, "days": test_days}
    return out


def stats(res) -> dict:
    tr = res["trades"]
    nets = [t["net"] for t in tr]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for day in res["days"]:
        eq += res["daily"].get(day, 0.0)
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    why: Dict[str, int] = {}
    for t in tr:
        why[t["why"]] = why.get(t["why"], 0) + 1
    total = sum(t["pnl"] for t in tr)
    return {
        "trades": len(tr),
        "daysTraded": sum(1 for d in res["days"] if any(t["day"] == d for t in tr)),
        "winRate": round(len(wins) / len(nets) * 100, 1) if nets else 0.0,
        "avgNet": round(statistics.mean(nets), 3) if nets else 0.0,
        "medianNet": round(statistics.median(nets), 3) if nets else 0.0,
        "avgWin": round(statistics.mean(wins), 2) if wins else 0.0,
        "avgLoss": round(statistics.mean(losses), 2) if losses else 0.0,
        "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "pnl": round(total), "retPct": round(total / (SLOT * SLOTS) * 100, 2),
        "mdd": round(mdd), "exits": why,
        "best": round(max(nets), 2) if nets else 0.0, "worst": round(min(nets), 2) if nets else 0.0,
    }


SPECS = [
    Spec("① ORB (지금 스캐너)"),
    Spec("①+② ORB + 시초가 지지 · 갭 2~5%", gapGate=(2, 5), openHold=True, selGap=(1.5, 6)),
    Spec("①+② 돌파 순간 진입 (실시간 봇 방식)", gapGate=(2, 5), openHold=True, selGap=(1.5, 6), entry="touch"),
    Spec("①+② 익절 없이 15:15 까지", gapGate=(2, 5), openHold=True, selGap=(1.5, 6), exit="eod"),
    Spec("①+② 필터 완화 (RVOL·거래속도 끔)", gapGate=(2, 5), openHold=True, selGap=(1.5, 6),
         rvolMin=0, volSurge=0),
    Spec("③ 조기 변동성 돌파 (손절 없음)", kind="vb", tp=1.5, sl=None, k=0.25),
    Spec("③ 조기 변동성 돌파 (손절 −1%)", kind="vb", tp=1.5, sl=1.0, k=0.25),
]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dirpath = args[0] if args else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                "data", "bt_cache")
    meta, data = load(dirpath)
    res = run(meta, data, SPECS)
    days = next(iter(res.values()))["days"]
    print(f"종목 {len(data) - 2} · 시험 {len(days)}거래일 ({days[0]} ~ {days[-1]}) · 앞 {WARMUP}일은 준비")
    rows = []
    for name, r in res.items():
        st = stats(r)
        rows.append((name, st))
        print(f"\n{name}\n  거래 {st['trades']} ({st['daysTraded']}일) · 승률 {st['winRate']}% · 평균 {st['avgNet']:+.3f}% · "
              f"중앙 {st['medianNet']:+.3f}% · 평균 이익 {st['avgWin']:+.2f}% / 손실 {st['avgLoss']:+.2f}% · "
              f"PF {st['profitFactor']}\n  손익 {st['pnl']:+,}원 ({st['retPct']:+.2f}% / 300만) · "
              f"최대 낙폭 {st['mdd']:,}원 · 최고 {st['best']:+.2f}% · 최악 {st['worst']:+.2f}% · 청산 {st['exits']}")
    if "--json" in sys.argv:
        path = sys.argv[sys.argv.index("--json") + 1]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"days": days, "results": [
                {"name": n, "stats": st, "trades": [{k: v for k, v in t.items()} for t in res[n]["trades"]],
                 "daily": res[n]["daily"]} for n, st in rows]}, f, ensure_ascii=False, indent=1)
        print(f"\n저장 {path}")


if __name__ == "__main__":
    main()
