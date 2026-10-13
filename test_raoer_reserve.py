#!/usr/bin/env python3
"""V4 배수가 마지막 회차 몫을 당겨 쓰지 않는가 — StrategyParams.raoer_reserve.

2026-10 빗썸 실계좌: AI 배수 1.25~1.8배가 이어져 SOL 은 39/40 회차에 현금 615원,
XRP 는 38/40 회차에 4,623원이 남아 최소 주문(5,000원)을 못 내고 멈췄다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services.strategy import StrategyParams   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def run(seed, split, mult, reserve=True, min_order=5000.0, cap_mult=2.0):
    """매 회차 V4 기본 매수금 × mult 로 산다. (산 회차 수, 남은 현금) 을 돌려준다."""
    p = StrategyParams(raoerMaxMultiplier=cap_mult)
    cash, turn = float(seed), 0
    for _ in range(split * 3):              # 매수 보류가 있어도 끝나게
        if turn >= split:
            break
        rem = split - turn
        chunk = min(cash / rem * mult, p.raoer_chunk_cap(seed))
        if reserve:
            chunk, _ = StrategyParams.raoer_reserve(chunk, cash, rem, min_order)
        invest = min(cash, chunk)
        if invest < min_order:
            break                            # 실전: '잔여 현금 부족 · 매수 보류' 로 멈춘다
        cash -= invest
        turn += 1
    return turn, cash


print("── SOL 실계좌 상황 (80만원 · 40분할 · 배수 1.8배가 계속) ──")
t0, c0 = run(800_000, 40, 1.8, reserve=False)
check("예전 규칙은 40회차 전에 현금이 바닥나 멈춘다 (재현)", t0 < 40, f"{t0}/40 · 남은 {c0:,.0f}원")
t1, c1 = run(800_000, 40, 1.8)
check("남은 회차 몫을 남기면 40회차까지 산다", t1 == 40, f"{t1}/40 · 남은 {c1:,.0f}원")
t2, _ = run(400_000, 40, 1.8)
check("XRP(40만원)도 40회차까지", t2 == 40, f"{t2}/40")

print("── 경계 ──")
out, note = StrategyParams.raoer_reserve(18_000, 20_000, 3, 5000)
check("이번에 사도 남은 2회차 × 5,000원은 남긴다", out == 10_000 and "보존" in note, f"{out:,.0f} {note}")
out, note = StrategyParams.raoer_reserve(9_000, 30_000, 3, 5000)
check("남길 돈이 충분하면 그대로", out == 9_000 and note == "", f"{out:,.0f}")
out, note = StrategyParams.raoer_reserve(2_500, 30_000, 3, 5000)
check("배수가 작아 최소 주문 미만이면 최소 주문으로 올린다 (매수 보류에 갇히지 않게)", out == 5000 and "올림" in note, f"{out:,.0f}")
out, _ = StrategyParams.raoer_reserve(20_000, 20_000, 1, 5000)
check("마지막 회차는 남은 현금을 다 쓴다", out == 20_000, f"{out:,.0f}")
out, _ = StrategyParams.raoer_reserve(6_000, 8_000, 3, 5000)
check("현금이 남은 회차 × 최소 주문보다 적으면 최소 주문만", out == 5000, f"{out:,.0f}")
out, _ = StrategyParams.raoer_reserve(615, 615, 1, 5000)
check("최소 주문 미만 현금은 올리지 않는다 (실전에서 매수 보류)", out == 615, f"{out:,.0f}")
out, _ = StrategyParams.raoer_reserve(350.0, 400.0, 10, 10.0)
check("달러(최소 $10)도 같은 규칙", out == 310.0, f"${out:,.2f}")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
