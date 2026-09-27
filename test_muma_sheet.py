#!/usr/bin/env python3
"""무매법 표가 사용자의 엑셀(무매법.xlsx)과 같은 숫자를 내는지 본다.

기대값은 엑셀이 계산해 둔 값을 그대로 옮겼다 (TQQQ · SOXL · LABU · GDXU 시트,
당일주문검증 시트). 엑셀 파일은 저장소에 없으므로 숫자를 여기 적어 둔다.
엑셀과 한 칸이라도 다르면 이 표를 엑셀 대신 쓸 수 없다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import muma_sheet as m      # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


SEED = 8996.85     # 현황!Z = ROUND(10,000,000 / 1,111.5, 2)

# 시트: (현재가, 평단, 개수) → 매도(가격,개수) · 1.15 LOC · 평단 LOC · 사다리
SHEETS = {
    "TQQQ": ((102.59, 105.748, 20), (116.32, 20), (117.98, 1), (105.75, 1),
             [74.89, 56.42, 45.14, 37.96, 31.8]),
    "SOXL": ((36.24, 37.4386, 56), (41.18, 56), (41.68, 3), (37.44, 3),
             [32.25, 28.27, 25.01, 22.47, 20.29, 18.84, 17.4, 15.95, 14.86, 14.13]),
    "LABU": ((63.59, 71.6448, 21), (78.81, 21), (73.13, 2), (71.64, 2),
             [45.15, 37.52, 32.43, 27.98, 24.8, 22.26, 20.35, 19.08]),
    "GDXU": ((22.44, 22.44, 1), (24.68, 1), (25.81, 4), (22.44, 5),
             [22.44, 20.42, 18.85, 17.28, 16.16, 15.03, 14.14, 13.24, 12.57, 11.89]),
}

print("── 엑셀 반올림 ──")
check("ROUND(0.5) 는 1 (파이썬 round 는 0)", m.xround(0.5) == 1.0, f"{m.xround(0.5)}")
check("ROUND(2.5) 는 3", m.xround(2.5) == 3.0, f"{m.xround(2.5)}")
check("ROUND(-0.78) 는 -1", m.xround(-0.7819908807) == -1.0, f"{m.xround(-0.7819908807)}")
check("ROUND(8996.85/40, 2) = 224.92", m.xround(SEED / 40, 2) == 224.92, f"{m.xround(SEED / 40, 2)}")
check("ROUND(1.005, 2) 가 부동소수 오차로 1.00 이 되지 않는다", m.xround(1.005, 2) == 1.01,
      f"{m.xround(1.005, 2)}")

print("── 당일 주문표 · 엑셀 시트와 대조 ──")
for t, ((px, avg, units), sell, big, avgloc, ladder) in SHEETS.items():
    p = m.order_plan(SEED, units, avg, px)
    got_sell = (p["sell"]["price"], p["sell"]["units"])
    check(f"{t} 매도 평단×1.1 지정가 전량", got_sell == sell, f"{got_sell} (엑셀 {sell})")
    got_big = (p["buys"][0]["price"], p["buys"][0]["units"])
    check(f"{t} 1.15증가 LOC", got_big == big, f"{got_big} (엑셀 {big})")
    got_avg = (p["buys"][1]["price"], p["buys"][1]["units"])
    check(f"{t} 평단 LOC", got_avg == avgloc, f"{got_avg} (엑셀 {avgloc})")
    got_lad = [lv["price"] for lv in p["ladder"]]
    check(f"{t} 추가 사다리 {len(ladder)}단", got_lad == ladder,
          f"{got_lad[:3]}… (엑셀 {ladder[:3]}…)")

p = m.order_plan(SEED, 21, 71.6448, 63.59)
check("사다리가 0.30배 아래로 가면 거기서 끊는다 (엑셀 #N/A)",
      len(p["ladder"]) == 8 and p["ladder"][-1]["level"] == 8, f"{len(p['ladder'])}단")
check("사다리 각 단은 1주", all(lv["units"] == 1 for lv in p["ladder"]), "")
check("1회분할금 · 0.5회분할금", (p["unitBudget"], p["halfBudget"]) == (224.92, 112.46),
      f"{p['unitBudget']} · {p['halfBudget']}")

p = m.order_plan(SEED, 20, 105.748, 102.59, split_count=20, target_pct=15)
check("봇의 분할 수·목표 수익률을 쓴다 (20분할 · 15%)",
      p["unitBudget"] == 449.84 and p["sell"]["price"] == 121.61,
      f"1회 {p['unitBudget']} · 매도 {p['sell']['price']}")

p = m.order_plan(4000, 1, 151.112, 151.11)       # 운영 SOXL: 1회 $100 · 주가 $151
z = p["zeroLegs"]
check("0.5회분할금이 주가 절반보다 작으면 두 다리 모두 0주로 표시",
      z and z["allZero"] and [b["units"] for b in p["buys"]] == [0, 0], f"{z}")
check("1주가 되는 최소 1회분할금 = 비싼 다리 가격 · SEED = ×분할수",
      z["minUnitBudget"] == 173.78 and z["minSeed"] == 6952, f"${z['minUnitBudget']} · ${z['minSeed']}")
q = m.order_plan(z["minSeed"], 1, 151.112, 151.11)
check("그 SEED 면 실제로 두 다리가 1주씩 된다",
      [b["units"] for b in q["buys"]] == [1, 1] and q["zeroLegs"] is None, f"{[b['units'] for b in q['buys']]}")
check("0주 다리가 없으면 안내하지 않는다", m.order_plan(SEED, 20, 105.748, 102.59)["zeroLegs"] is None, "")

p = m.order_plan(SEED, 0, 0, 102.59)
check("보유가 없으면 엑셀처럼 매도·사다리 없이 첫 매수만",
      p["sell"] is None and not p["ladder"] and len(p["buys"]) == 1 and p["buys"][0]["units"] == 2,
      f"{p['buys']}")
p = m.order_plan(SEED, 20, 105.748, 0)
check("현재가가 없으면 주문을 만들지 않는다", p["sell"] is None and not p["buys"], "")

print("── 매수 기록 · 엑셀 TQQQ!A176:I185 ──")
TQQQ_FILLS = [("2021-04-26", 111.22), ("2021-04-27", 109.76), ("2021-04-28", 108.63),
              ("2021-04-29", 109.72), ("2021-04-30", 107.66), ("2021-05-03", 105.9),
              ("2021-05-04", 100.32), ("2021-05-05", 99.15), ("2021-05-06", 101.32),
              ("2021-05-07", 103.8)]
trades = [{"time": f"{d} 05:00:00", "action": "BUY_CHUNK", "price": px, "units": 2}
          for d, px in TQQQ_FILLS]
recs = m.buy_records(list(reversed(trades)))        # 봇은 최신이 앞에 온다
last = recs[-1]
check("10회 · 20주 · 누적 2,114.96", (last["count"], last["shares"], last["cumAmount"]) == (10, 20, 2114.96),
      f"{last['count']}회 · {last['shares']}주 · {last['cumAmount']}")
check("평단 105.748 (엑셀 H185)", last["avg"] == 105.748, f"{last['avg']}")
check("7회차 평단 107.6014 · 손익률 -6.77% (엑셀 H182 · I182)",
      recs[6]["avg"] == 107.6014 and recs[6]["pnlPct"] == -6.77, f"{recs[6]['avg']} · {recs[6]['pnlPct']}")
check("날짜 순서대로 쌓는다", [r["time"][:10] for r in recs] == [d for d, _ in TQQQ_FILLS], "")

cyc = [{"time": "2021-03-01 05:00:00", "action": "BUY", "price": 90, "units": 3},
       {"time": "2021-03-02 05:00:00", "action": "SELL", "price": 99, "units": 3, "pnlKrw": 27},
       {"time": "2021-03-03 05:00:00", "action": "BUY", "price": 100, "units": 4},
       {"time": "2021-03-04 05:00:00", "action": "SELL_QUARTER", "price": 80, "units": 1},
       {"time": "2021-03-05 05:00:00", "action": "BUY_CHUNK", "price": 70, "units": 3}]
recs = m.buy_records(cyc)
check("전량 매도 뒤부터 새 사이클", len(recs) == 3 and recs[0]["price"] == 100, f"{len(recs)}줄")
check("쿼터 매도는 평단을 그대로 두고 주수만 줄인다",
      recs[1]["shares"] == 3 and recs[1]["avg"] == 100 and recs[1]["units"] == -1, f"{recs[1]}")
check("쿼터 매도 뒤 매수는 줄어든 원가에서 평단을 다시 낸다",
      recs[2]["avg"] == 85 and recs[2]["count"] == 2, f"평단 {recs[2]['avg']} · {recs[2]['count']}회")

print("── 월별 실현 (엑셀 '실현' 시트) ──")
rz = m.realized_by_month([
    {"time": "2021-05-10 05:00:00", "coin": "NAIL", "currency": "USD", "action": "SELL", "pnlKrw": 100.5},
    {"time": "2021-05-20 05:00:00", "coin": "NAIL", "currency": "USD", "action": "SELL_QUARTER", "pnlKrw": -8.25},
    {"time": "2021-06-01 05:00:00", "coin": "TQQQ", "currency": "USD", "action": "SELL", "pnlKrw": 40},
    {"time": "2021-06-01 05:00:00", "coin": "TQQQ", "currency": "USD", "action": "BUY", "pnlKrw": 0},
    {"time": "2021-06-02 05:00:00", "coin": "XRP", "currency": "KRW", "action": "SELL", "pnlKrw": 5000},
])
nail = next(r for r in rz["rows"] if r["ticker"] == "NAIL")
check("종목 × 월로 합친다", rz["months"] == ["2021-05", "2021-06"] and nail["byMonth"] == {"2021-05": 92.25},
      f"{rz['months']} · {nail['byMonth']}")
check("원화(코인) 매도는 넣지 않는다", all(r["ticker"] != "XRP" for r in rz["rows"]), "")

print("── 현황 한 줄 · 잔고검증 ──")
bot = {"botId": "b1", "coin": "TQQQ", "coinName": "ProShares", "currency": "USD", "isRunning": True,
       "initialKrw": SEED, "units": 20, "entryPrice": 105.748, "currentPrice": 102.59,
       "investedKrw": 2114.96, "turn": 10, "splitCount": 40, "targetProfitPct": 10.0}
coin_bot = {"botId": "c1", "coin": "XRP", "currency": "KRW"}
out = m.build([bot, coin_bot], {"b1": trades}, {"TQQQ": {"qty": 20, "avgPrice": 105.748}}, [])
r = out["rows"][0]
check("코인 봇은 표에 넣지 않는다", len(out["rows"]) == 1, f"{len(out['rows'])}줄")
check("현평가액 2,051.80 · 손익률 -2.99% (엑셀 I6 · M6)",
      r["value"] == 2051.8 and r["pnlPct"] == -2.99, f"{r['value']} · {r['pnlPct']}")
check("손익금 = 총투자금 × 손익률 (엑셀 N6 -63.16)", r["pnl"] == -63.16, f"{r['pnl']}")
check("계좌와 개수가 같으면 검증 통과", r["verify"]["qtyOk"] and r["verify"]["avgDiff"] == 0, f"{r['verify']}")
out = m.build([bot], {}, {"TQQQ": {"qty": 21, "avgPrice": 105.0}}, [])
check("계좌 개수가 다르면 잡아낸다", not out["rows"][0]["verify"]["qtyOk"], f"{out['rows'][0]['verify']}")
out = m.build([bot], {}, None, [])
check("잔고를 못 받으면 검증 칸을 비운다 (통과로 보이지 않게)",
      out["rows"][0]["verify"] is None and not out["accountChecked"], "")
flags = m.build([{**bot, "currentPrice": 84.0, "turn": 31}], {}, None, [])["rows"][0]
check("-20% 이하면 스플릿법 Y · 30회 이상이면 5%매도",
      flags["splitFlag"] and flags["quarterFlag"] == "5%매도", f"{flags['pnlPct']}% · {flags['turn']}회")
none = m.build([{**bot, "units": 0, "entryPrice": 0, "turn": 0, "investedKrw": 0}], {}, None, [])["rows"][0]
check("보유가 없으면 플래그를 세우지 않는다", not none["splitFlag"] and none["pnl"] == 0, "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
