"""무매법 표 — 사용자가 손으로 쓰던 엑셀(무매법.xlsx)을 봇 장부로 다시 그린다.

엑셀은 종목마다 세 가지 표를 둔다.

  현황      총SEED · 1회분할금 · 횟수 · 총투자금 · 평가액 · 총개수 · 현재가 ·
            평단 · 손익률 · 손익금 · 스플릿법(-20% 이하) · 분할매도(30회 이상) ·
            잔고검증(증권사 잔고와 개수·평단이 맞는지)
  당일 주문  매도   평단 × (1+목표%) 지정가 · 전량
            매수   현재가 × 1.15 LOC · 0.5회분
                   평단 LOC · 0.5회분
                   추가 사다리 LOC · 1주씩 (하락하면 1회분할금을 다 쓰도록)
  매수 기록  날짜 · 체결가 · 수량 · 총가격 · 누적가격 · 횟수 · 총주수 · 평단 · 손익률

그리고 '실현' 시트는 종목 × 월 실현손익 표다.

숫자는 엑셀 수식을 그대로 옮겼다 (ROUND 는 엑셀식 반올림). 엑셀과 대조할
수 있어야 쓸모가 있어서다. 이 표는 **엑셀의 무매법(V1) 계산** 이고, 봇이
실제로 내는 주문(V4 · 평단/평단+5%)과는 다를 수 있다 — 화면에 둘을 나란히
보여 준다.
"""

import math
from typing import Any, Dict, List, Optional

BIG_LOC_MULT = 1.15        # 엑셀 '1.15증가 LOC'
SPLIT_WARN_PCT = -20.0     # 엑셀 '스플릿법' — 손익률 -20% 이하
QUARTER_SELL_TURN = 30     # 엑셀 '분할매도' — 30회 이상이면 5% 매도
QUARTER_SELL_LABEL = "5%매도"
LADDER_TOP = 1.8           # 엑셀 A23 — 현재가 대비 배수 사다리 시작
LADDER_ROWS = 151          # 엑셀 A23:A173 — 1.80 → 0.30, 0.01 씩
LADDER_LEVELS = 10         # 엑셀 A12:A21 — 1주 ~ 10주 추가
BUY_ACTIONS = ("BUY", "BUY_CHUNK")


def xround(x: float, nd: int = 0) -> float:
    """엑셀 ROUND — 0.5 는 0 에서 먼 쪽으로. 파이썬 round 는 짝수 쪽이라 다르다."""
    m = 10 ** nd
    v = abs(x) * m
    # 부동소수 오차(2.675 → 2.67499…)로 반올림이 한 칸 내려가는 것을 막는다
    r = math.floor(v + 0.5 + 1e-9) / m
    return math.copysign(r, x) if x else 0.0


def _ladder(price: float, unit_budget: float, base_qty: int) -> List[Dict[str, Any]]:
    """엑셀 '현가대비 배수' 사다리 (TQQQ!A12:B21 · A23:H173).

    현재가의 1.80배 ~ 0.30배를 0.01 씩 내려가며, 그 가격에서 1회분할금으로
    기본 두 주문(C9+C10) 외에 몇 주를 더 살 수 있는지(반올림)를 본다.
    k주가 되는 가격들 가운데 1회분할금과 가장 가깝게 맞는 가격을 k번째
    단으로 고른다. 각 단에 1주씩 LOC 를 걸면, 종가가 k번째 단 아래로
    마감할 때 1~k 단이 모두 체결돼 그날 1회분할금을 거의 다 쓴다.
    """
    rows = []
    for i in range(LADDER_ROWS):
        mult = LADDER_TOP + 1e-10 - 0.01 * i
        p = price * mult
        h = unit_budget / p - base_qty
        c = xround(h)
        rows.append((mult, p, int(c), abs(p * (h - c))))
    out = []
    for k in range(1, LADDER_LEVELS + 1):
        cands = [r for r in rows if r[2] == k]
        if not cands:
            break                       # 엑셀은 여기서부터 #N/A
        # MATCH(MIN(IF(C=k,E)),E,0) — 가장 작은 오차, 같으면 위쪽(높은 가격)
        best = min(cands, key=lambda r: r[3])
        out.append({"level": k, "mult": round(best[0], 2),
                    "price": round(best[1], 2), "units": 1})
    return out


def order_plan(seed: float, units: float, avg: float, price: float,
               split_count: int = 40, target_pct: float = 10.0) -> Dict[str, Any]:
    """엑셀 종목 시트 A1:E21 — 오늘 낼 주문표."""
    split_count = int(split_count or 40)
    unit_budget = xround(seed / split_count, 2) if seed else 0.0       # B3
    half_budget = xround(unit_budget / 2, 2) if unit_budget else 0.0   # B4
    plan = {"unitBudget": unit_budget, "halfBudget": half_budget,
            "sell": None, "buys": [], "ladder": [], "hasPosition": units > 0,
            "zeroLegs": None}
    if not price or price <= 0:
        return plan
    if units <= 0 or avg <= 0:
        # 엑셀은 매수 기록이 없으면 주문표를 비운다. 첫날은 1회분할금으로 산다.
        qty = int(unit_budget // price) if unit_budget else 0
        if qty > 0:
            plan["buys"].append({"leg": "첫 매수", "type": "LOC", "price": round(price * 1.05, 2),
                                 "units": qty, "amount": round(qty * price, 2)})
        return plan

    avg4 = xround(avg, 4)                                              # D1
    sell_px = xround(avg4 * (1 + target_pct / 100.0), 4)               # B8
    plan["sell"] = {"leg": f"매도 ×{1 + target_pct / 100.0:g}", "type": "지정가",
                    "price": round(sell_px, 2), "units": units,
                    "amount": round(sell_px * units, 2)}
    big_px = xround(price * BIG_LOC_MULT, 2)                           # B9
    big_qty = int(xround(half_budget / big_px))                        # C9
    avg_qty = int(xround(half_budget / avg4))                          # C10
    plan["buys"] = [
        {"leg": f"{BIG_LOC_MULT:g}증가 LOC", "type": "LOC", "price": big_px,
         "units": big_qty, "amount": round(big_qty * big_px, 2)},
        {"leg": "평단 LOC", "type": "LOC", "price": round(avg4, 2),
         "units": avg_qty, "amount": round(avg_qty * avg4, 2)},
    ]
    plan["ladder"] = _ladder(price, unit_budget, big_qty + avg_qty)
    # 0.5회분할금이 주가의 절반도 안 되면 ROUND 가 0 을 내 그 다리는 사지 않는다.
    # 1주가 되려면 0.5회분 ≥ 가격/2, 즉 1회분할금 ≥ 그 다리 가격이어야 한다.
    zero = [x for x in plan["buys"] if x["units"] == 0]
    if zero:
        need = math.ceil(max(x["price"] for x in zero) * 100) / 100
        plan["zeroLegs"] = {"legs": [x["leg"] for x in zero], "allZero": len(zero) == len(plan["buys"]),
                            "minUnitBudget": need, "minSeed": math.ceil(need * split_count)}
    return plan


def buy_records(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """엑셀 종목 시트 A175:I501 — 이번 사이클(마지막 전량 매도 이후)의 기록.

    엑셀은 매수만 적는다. 봇은 1/4 매도(쿼터 손절)도 하므로 그 줄도 넣고,
    그때는 평단을 그대로 둔 채 주수와 누적가격만 줄인다.
    """
    chrono = sorted(trades, key=lambda t: t.get("time") or "")
    last_exit = max((i for i, t in enumerate(chrono) if t.get("action") == "SELL"), default=-1)
    cycle = chrono[last_exit + 1:]

    rows, cum, shares, n = [], 0.0, 0.0, 0
    for t in cycle:
        act, px, qty = t.get("action"), float(t.get("price") or 0), float(t.get("units") or 0)
        if act in BUY_ACTIONS:
            if qty <= 0:
                continue
            n += 1
            cost = px * qty
            cum += cost
            shares += qty
        elif act == "SELL_QUARTER" and shares > 0:
            avg_before = cum / shares
            qty = min(qty, shares)
            shares -= qty
            cum = avg_before * shares
            cost = -px * qty
        else:
            continue
        avg = cum / shares if shares else 0.0
        rows.append({"time": t.get("time"), "action": act, "price": px,
                     "units": qty if act in BUY_ACTIONS else -qty,
                     "amount": round(cost, 2), "cumAmount": round(cum, 2),
                     "count": n, "shares": shares, "avg": round(avg, 4),
                     "pnlPct": round((px - avg) / avg * 100, 2) if avg else 0.0})
    return rows


def realized_by_month(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """엑셀 '실현' 시트 — 종목 × 월 실현손익(USD)."""
    table: Dict[str, Dict[str, float]] = {}
    months = set()
    for t in trades:
        if t.get("currency") != "USD" or not str(t.get("action", "")).startswith("SELL"):
            continue
        m = str(t.get("time") or "")[:7]
        if not m:
            continue
        months.add(m)
        row = table.setdefault(t.get("coin") or "?", {})
        row[m] = round(row.get(m, 0.0) + float(t.get("pnlKrw") or 0), 2)
    months_sorted = sorted(months)
    return {"months": months_sorted,
            "rows": [{"ticker": k, "byMonth": v, "total": round(sum(v.values()), 2)}
                     for k, v in sorted(table.items())]}


def build(bots: List[Dict[str, Any]], trades_by_bot: Dict[str, List[Dict[str, Any]]],
          holdings: Optional[Dict[str, Dict[str, Any]]] = None,
          all_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """나무증권(USD) 봇마다 현황 한 줄 + 주문표 + 매수 기록을 만든다.

    bots        BotManager.all_status() 의 USD 봇들
    holdings    NamuhAccount.get_balance()["holdings"] — 없으면 잔고검증을 비운다
    """
    out, n = [], 0
    for b in bots:
        if b.get("currency") != "USD":
            continue
        n += 1
        seed = float(b.get("initialKrw") or 0)
        units = float(b.get("units") or 0)
        avg = float(b.get("entryPrice") or 0)
        price = float(b.get("currentPrice") or 0)
        invested = float(b.get("investedKrw") or 0)
        turn = int(b.get("turn") or 0)
        split = int(b.get("splitCount") or 40)
        target = float(b.get("targetProfitPct") or 10.0)
        value = units * price
        pnl_pct = (price - avg) / avg * 100 if units > 0 and avg > 0 else 0.0

        verify = None
        if holdings is not None:
            h = holdings.get(b.get("coin")) or {}
            acc_qty = float(h.get("qty") or 0)
            acc_avg = float(h.get("avgPrice") or 0)
            verify = {"accountQty": acc_qty, "accountAvg": acc_avg,
                      "qtyOk": abs(acc_qty - units) < 1e-6,
                      "avgDiff": round(acc_avg - avg, 4) if acc_qty else None}

        out.append({
            "no": f"{n:03d}", "botId": b.get("botId"), "ticker": b.get("coin"),
            "name": b.get("coinName"), "isRunning": b.get("isRunning"),
            "seed": round(seed, 2), "unitBudget": xround(seed / split, 2) if seed else 0.0,
            "turn": turn, "splitCount": split, "targetProfitPct": target,
            "invested": round(invested, 2), "value": round(value, 2),
            "units": units, "price": price, "avg": avg,
            "pnlPct": round(pnl_pct, 2),
            "pnl": round(invested * pnl_pct / 100, 2) if turn else 0.0,   # 엑셀 N = H × M
            "splitFlag": units > 0 and pnl_pct <= SPLIT_WARN_PCT,
            "quarterFlag": QUARTER_SELL_LABEL if turn >= QUARTER_SELL_TURN else "",
            "verify": verify,
            "plan": order_plan(seed, units, avg, price, split, target),
            "records": buy_records(trades_by_bot.get(b.get("botId")) or []),
        })

    tot_inv = sum(r["invested"] for r in out)
    tot_val = sum(r["value"] for r in out)
    summary = {"seed": round(sum(r["seed"] for r in out), 2),
               "unitBudget": round(sum(r["unitBudget"] for r in out), 2),
               "invested": round(tot_inv, 2), "value": round(tot_val, 2),
               "units": sum(r["units"] for r in out),
               "pnl": round(sum(r["pnl"] for r in out), 2),
               "pnlPct": round((tot_val - tot_inv) / tot_inv * 100, 2) if tot_inv else 0.0}
    return {"rows": out, "summary": summary,
            "realized": realized_by_month(all_trades or []),
            "accountChecked": holdings is not None}
