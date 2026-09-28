#!/usr/bin/env python3
"""scripts/orb_backtest.py 의 체결 규칙 — 가짜 1분봉으로 본다 (네트워크 없음)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import orb_backtest as bt          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def day_bars(or_bars, after):
    """or_bars: 09:01~09:05 [o,h,l,c,v] · after: {hhmm: [o,h,l,c,v]} · 나머지는 평평한 봉."""
    bars = {}
    for t, b in zip(bt.minutes("0901", "0905"), or_bars):
        bars[t] = b + [b[3] * b[4]]
    last = or_bars[-1][3]
    for t in bt.minutes("0906", "1530"):
        b = after.get(t) or [last, last, last, last, 100]
        bars[t] = b + [b[3] * b[4]]
        last = b[3]
    return bars


OR = [[10000, 10050, 9990, 10020, 5000], [10020, 10060, 10000, 10040, 1000], [10040, 10080, 10010, 10050, 1000],
      [10050, 10100, 10030, 10060, 1000], [10060, 10090, 10040, 10070, 1000]]
W = {"open": 10000, "gap": 3.0, "prev": {"h": 10000, "l": 9500, "c": 9709, "v": 1_000_000}}
S = bt.Spec("t", rvolMin=0, market=False)


def run(after, spec=S, or_bars=OR, w=W):
    d = {"m1": {"20260901": day_bars(or_bars, after)}, "daily": {}}
    return bt.sim_orb(spec, d, None, "20260901", w)


print("── ORB: 봉 종가로 돌파 확인 → 다음 봉 시가 ──")
r = run({"0907": [10070, 10200, 10060, 10150, 5000], "0908": [10160, 10180, 10150, 10170, 800]})
check("09:07 종가가 OR 고가(10,100) 위 · 거래 붙음 → 09:08 시가 +1틱에 산다",
      r and r["in"] == "0908" and r["entry"] == 10170, f"{r}")
r = run({"0907": [10070, 10200, 10060, 10090, 5000]})
check("윗꼬리만 넘고 종가가 OR 고가 아래면 사지 않는다", r is None, f"{r}")
r = run({"0907": [10070, 10200, 10060, 10150, 1400]})
check("거래가 안 붙은 돌파(OR 평균 1,000 × 1.5 미만)는 사지 않는다", r is None, f"{r}")

print("── 청산 ──")
r = run({"0907": [10070, 10200, 10060, 10150, 5000], "0908": [10160, 10400, 9900, 10300, 800]})
check("같은 봉에서 손절 · 익절이 둘 다 닿으면 손절로 본다", r and r["why"] == "손절", f"{r and r['why']}")
r = run({"0907": [10070, 10200, 10060, 10150, 5000], "0909": [10170, 10400, 10160, 10350, 800]})
check("익절가(10,160 × 1.02 = 10,363 → 호가 올림 10,370)에 닿으면 −1틱(10,360)에 판다",
      r and r["why"] == "익절" and r["exit"] == 10360, f"{r}")
r = run({"0907": [10070, 10200, 10060, 10150, 5000], "0910": [9800, 9820, 9700, 9750, 800]})
check("시가가 손절선 아래로 갭이면 그 시가에 판다 (VI · 급락)", r and r["why"] == "손절" and r["exit"] == 9790, f"{r}")
r = run({"0907": [10070, 10200, 10060, 10150, 5000]})
check("아무것도 안 닿으면 09:30 종가 −1틱 타임컷", r and r["why"] == "타임컷" and r["out"] == "0930", f"{r}")

print("── ① + ② 조건 ──")
g = bt.Spec("g", rvolMin=0, market=False, gapGate=(2, 5), openHold=True)
brk = {"0907": [10070, 10200, 10060, 10150, 5000]}
check("갭 3% · 1분 종가가 모두 시가 이상 → 통과", run(brk, g) is not None, "")
check("갭 6% 는 거른다", run(brk, g, w={**W, "gap": 6.0}) is None, "")
low_wick = [[10000, 10050, 9950, 10020, 5000]] + OR[1:]
check("첫 1분 저가 꼬리만 시가 아래면 통과 (1분 종가 기준)", run(brk, g, or_bars=low_wick) is not None, "")
broke = [[10000, 10050, 9950, 9980, 5000]] + OR[1:]
check("1분 종가가 시가 아래로 한 번이라도 닫히면 거른다", run(brk, g, or_bars=broke) is None, "")

print("── ③ 조기 변동성 돌파 ──")
v = bt.Spec("v", kind="vb", tp=1.5, sl=1.0, k=0.25)
flat = [[10140, 10150, 10130, 10140, 1000]] * 4
d = {"m1": {"20260901": day_bars([[10000, 10150, 9900, 10140, 5000]] + flat, {})}, "daily": {}}
r = bt.sim_vb(v, d, "20260901", W)
check("목표가 = 시가 + 0.25 × 전일 변동폭 = 10,125 → 호가 올림 10,130 · 닿은 봉에서 +1틱에 산다",
      r and r["in"] == "0901" and r["entry"] == 10140, f"{r}")
check("산 봉의 저가(9,900 — 산 앞에 찍힘)로는 손절하지 않는다", r and r["why"] != "손절", f"{r and r['why']}")

print("── 비용 ──")
check("본전(같은 값에 사고팔기)은 −0.23% (수수료 0.015% × 2 + 매도세 0.20%)",
      abs(bt.net_pct(10000, 10000) + 0.23) < 0.001, f"{bt.net_pct(10000, 10000):.4f}%")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
