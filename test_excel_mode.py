#!/usr/bin/env python3
"""무매법 엑셀 방식 (locMode='excel') — 무매법.xlsx 의 매수 · 매도 규칙을 실주문으로 그대로 내는가.

  · 1회분할금 = 운용자본 ÷ 분할수 (고정) · 매크로 기어 · AI 를 쓰지 않는다
  · 보유 없음: 1회분할금으로 살 수 있는 만큼 현재가×1.05 LOC
  · 보유 있음: 0.5회분 현재가×1.15 LOC + 0.5회분 평단 LOC + 사다리(1주씩) LOC
  · 마감 뒤 주문번호마다 체결내역으로 정산 (못 읽으면 잔고 변화로) · 이월 없음
가짜 계좌로 본다.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="excel-test-")
from services import botstore, tradelog              # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

from services import namuh, trader, strategy, muma_sheet   # noqa: E402
from services.namuh import OrderUnknown                     # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def params(**k):
    base = dict(strategyType="raoer_infinite", raoerVersion="v4", splitCount=40, targetProfitPct=10.0,
                locMode="excel", feePct=0.0, useMacroGear=True, raoerUseAi=True)
    base.update(k)
    return strategy.StrategyParams.from_dict(base)          # 배포 · 복원과 같은 길 (검증 포함)


class FakeAcct:
    configured = True

    def __init__(self, qty=0.0, avg=0.0, fail_at=None):
        self.qty, self.avg, self.fail_at, self.calls, self.fills = qty, avg, fail_at, [], {}

    def get_balance(self, fresh=False):
        return {"qtyByTicker": {"TQQQ": self.qty}, "holdings": {"TQQQ": {"avgPrice": self.avg}}}

    def market_buy(self, sym, amount_usd=0.0, units=0.0, order_type="", limit_price=0.0, await_fill=True, **k):
        self.calls.append({"units": units, "type": order_type, "limit": limit_price, "await": await_fill})
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise OrderUnknown("응답 끊김 (시험)", side="buy", ticker=sym, qty=units, price=limit_price, qty_before=self.qty)
        return {"orderId": str(1000 + len(self.calls)), "units": float(units), "fillPrice": None}

    def order_fill(self, oid):
        return self.fills.get(str(oid))

    def held_qty(self, sym):
        return self.qty


def bot(fa, mode="LIVE", seed=4000.0, **pk):
    b = trader.TradingBot(bot_id=f"x-{time.time()}", coin="TQQQ", interval="24h", mode=mode,
                          capital_krw=seed, params=params(**pk), namuh_account=fa, broker="namuh")
    b._persist = lambda: None
    return b


print("── 설정 ──")
p = params()
check("locMode='excel' 을 받는다", p.locMode == "excel", p.locMode)
check("엑셀 방식이면 매크로 기어 · AI 를 끈다 (목표 · 1회분할금 고정)", not p.useMacroGear and not p.raoerUseAi, "")
check("다른 방식은 기어 설정을 그대로 둔다", params(locMode="half_half").useMacroGear, "")

print("── 오늘 낼 LOC ──")
b = bot(FakeAcct())
t = b._excel_targets(80.0)
check("보유 없음: 1회분할금 $100 으로 1주 · 현재가×1.05 LOC", [(x["leg"], x["units"], x["limit"]) for x in t["legs"]]
      == [("첫 매수", 1, 84.0)], f"{t['legs']}")
b.pos.units, b.pos.entryPrice, b.pos.turn, b.cash = 1.0, 82.26, 1, 3917.74
t = b._excel_targets(81.0)
plan = muma_sheet.order_plan(4000.0, 1.0, 82.26, 81.0, 40, 10.0)
check("보유 있음: 1.15 LOC · 평단 LOC 가 엑셀 계산과 같다",
      [(x["units"], x["limit"]) for x in t["legs"][:2]] == [(1, 93.15), (1, 82.26)], f"{t['legs'][:2]}")
check("사다리 단도 엑셀 계산 그대로 1주씩",
      [x["limit"] for x in t["legs"][2:]] == [round(l["price"], 2) for l in plan["ladder"]]
      and all(x["units"] == 1 for x in t["legs"][2:]), f"{[x['limit'] for x in t['legs'][2:]]}")
b.cash = 150.0
t = b._excel_targets(81.0)
check("현금이 모자라면 사다리부터 덜어낸다 (1.15 LOC 는 남긴다)",
      t["legs"] and t["legs"][0]["leg"].startswith("1.15") and t["dropped"]
      and sum(x["units"] * x["limit"] for x in t["legs"]) <= 150.0, f"남김 {[x['leg'] for x in t['legs']]}")

print("── 실주문 접수 ──")
fa = FakeAcct(qty=1.0, avg=82.26)
b = bot(fa)
b.pos.units, b.pos.entryPrice, b.pos.turn, b.cash = 1.0, 82.26, 1, 3917.74
b._place_excel_orders(81.0, "2026-10-09")
n = len(b._excel_targets(81.0)["legs"])
check("다리마다 LOC 한 건씩 · 체결을 기다리지 않는다", len(fa.calls) == n and all(
      c["type"] == namuh.ORD_LOC and c["await"] is False for c in fa.calls), f"{len(fa.calls)}건")
check("접수한 주문은 정산 목록에 '엑셀' 로 남는다", len(b.pending_orders) == n and all(
      o["mode"] == "excel" and o["qtyBefore"] == 1.0 for o in b.pending_orders), "")
check("오늘 세션을 기록해 하루 두 번 걸지 않는다", b.loc_session == "2026-10-09", "")
check("장부는 접수만으로 바뀌지 않는다 (마감 뒤 정산)", b.pos.units == 1.0 and b.pos.turn == 1, "")

fb = FakeAcct(qty=1.0, avg=82.26, fail_at=2)
b2 = bot(fb)
b2.pos.units, b2.pos.entryPrice, b2.pos.turn, b2.cash = 1.0, 82.26, 1, 3917.74
b2._place_excel_orders(81.0, "2026-10-09")
check("앞 주문의 결과를 모르면 다음 다리를 내지 않는다", len(fb.calls) == 2 and bool(b2.order_hold)
      and len(b2.pending_orders) == 1, f"{len(fb.calls)}건")

fc = FakeAcct()
b3 = bot(fc)
b3._place_excel_orders(150.0, "2026-10-09")
check("1회분할금으로 1주를 못 사면 주문 없이 필요한 운용자본을 알린다",
      fc.calls == [] and "6,000" in b3.last_decision and b3.loc_session == "2026-10-09", b3.last_decision)

print("── 마감 뒤 정산 ──")
fa.fills = {o["orderId"]: {"qty": 0.0, "price": 0.0} for o in b.pending_orders}
fa.fills["1001"] = {"qty": 1.0, "price": 80.5}
fa.fills["1002"] = {"qty": 1.0, "price": 80.5}
fa.qty, fa.avg = 3.0, 81.09
for o in b.pending_orders:
    o["session"] = "2000-01-01"
b.budget_carryover = 37.71
b._settle_pending_loc()
check("주문별 체결내역으로 수량 · 체결가를 적는다", b.pos.units == 3.0 and abs(b.pos.entryPrice - (82.26 + 161.0) / 3) < 1e-9,
      f"{b.pos.units:g}주 · 평단 {b.pos.entryPrice:.4f}")
check("하루 매수 = 1회차 · 이월 없음 · 현금 차감", b.pos.turn == 2 and b.budget_carryover == 0.0
      and abs(b.cash - (3917.74 - 161.0)) < 1e-9 and not b.pending_orders, f"T={b.pos.turn} 현금 {b.cash:.2f}")

fd = FakeAcct(qty=3.0, avg=81.09)
b4 = bot(fd)
b4.pos.units, b4.pos.entryPrice, b4.pos.turn, b4.cash = 3.0, 81.09, 2, 3756.74
b4._place_excel_orders(70.0, "2000-01-01")
fd.fills = {o["orderId"]: {"qty": 0.0, "price": 0.0} for o in b4.pending_orders}
b4._settle_pending_loc()
check("하나도 안 붙으면 회차 · 현금 그대로 · 이월 없음",
      b4.pos.turn == 2 and b4.cash == 3756.74 and b4.budget_carryover == 0.0 and not b4.pending_orders, b4.last_decision)

fe = FakeAcct(qty=1.0, avg=82.26)
b5 = bot(fe)
b5.pos.units, b5.pos.entryPrice, b5.pos.turn, b5.cash = 1.0, 82.26, 1, 3917.74
b5._place_excel_orders(81.0, "2000-01-01")
fe.qty, fe.avg = 2.0, (82.26 + 80.0) / 2                  # 체결내역은 못 읽음 → 잔고로
b5._settle_pending_loc()
check("체결내역을 못 읽으면 잔고 변화로 역산한다", b5.pos.units == 2.0 and abs(b5.pos.entryPrice - 81.13) < 1e-6,
      f"{b5.pos.units:g}주 · 평단 {b5.pos.entryPrice:.4f}")

print("── 재시작 · 모의 ──")
fg = FakeAcct(qty=1.0, avg=82.26)
b6 = bot(fg)
b6.pos.units, b6.pos.entryPrice, b6.pos.turn, b6.cash = 1.0, 82.26, 1, 3917.74
b6._place_excel_orders(81.0, "2026-10-09")
r = trader.TradingBot.restore(b6.snapshot(), None, FakeAcct(qty=1.0, avg=82.26))
check("재시작해도 접수한 LOC 와 방식이 이어진다", len(r.pending_orders) == len(b6.pending_orders)
      and r.params.locMode == "excel" and r.pending_orders[0]["mode"] == "excel", "")
pb = bot(None, mode="PAPER")
pb.pos.units, pb.pos.entryPrice, pb.pos.turn, pb.cash = 1.0, 82.26, 1, 3917.74
pb._place_excel_orders(81.0, "2026-10-09")
check("모의: 상한이 현재가 이상인 다리만 현재가로 체결 (1.15 · 평단 LOC 2주)",
      pb.pos.units == 3.0 and pb.pos.turn == 2 and abs(pb.cash - (3917.74 - 162.0)) < 1e-9, f"{pb.pos.units:g}주")

print("── 돌고 있는 봇 옮기기 (scripts/namuh_to_excel.py) ──")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import io, contextlib, namuh_to_excel                # noqa: E402
from services.jsonfile import read_records, write_records   # noqa: E402
old = bot(FakeAcct(qty=1.0, avg=82.26), locMode="half_half_now")
old.pos.units, old.pos.entryPrice, old.pos.turn, old.cash, old.budget_carryover = 1.0, 82.26, 1, 3917.71, 37.71
old.loc_session = "2026-10-08"
path = botstore.namuh_store_file()
write_records(path, "bots", [old.snapshot()])
namuh_to_excel.service_active = lambda: False
with contextlib.redirect_stdout(io.StringIO()):
    rc = namuh_to_excel.main(["TQQQ"])
check("미리 보기는 파일을 바꾸지 않는다", rc == 0 and read_records(path, "bots", "")[0]["params"]["locMode"] == "half_half_now", "")
with contextlib.redirect_stdout(io.StringIO()):
    rc = namuh_to_excel.main(["TQQQ", "--apply"])
rec = read_records(path, "bots", "")[0]
m = trader.TradingBot.restore(rec, None, FakeAcct(qty=1.0, avg=82.26))
check("--apply: 엑셀 방식 · 기어 · AI 끔 · 이월 0 · 오늘 LOC 다시 걸 수 있게",
      rc == 0 and m._excel() and not m.params.useMacroGear and not m.params.raoerUseAi
      and m.budget_carryover == 0.0 and m.loc_session is None, "")
check("장부(수량 · 평단 · 회차 · 현금)는 그대로", (m.pos.units, m.pos.entryPrice, m.pos.turn, m.cash)
      == (1.0, 82.26, 1, 3917.71), "")
check("원본을 남긴다", any(f.startswith("bots_namuh.json.before-excel-") for f in os.listdir(_SANDBOX)), "")
namuh_to_excel.service_active = lambda: True
with contextlib.redirect_stdout(io.StringIO()):
    rc = namuh_to_excel.main(["TQQQ", "--apply"])
check("나무증권 서비스가 돌고 있으면 쓰지 않는다", rc == 1, "")
rec["orderHold"] = {"side": "buy"}
write_records(path, "bots", [rec])
namuh_to_excel.service_active = lambda: False
with contextlib.redirect_stdout(io.StringIO()):
    rc = namuh_to_excel.main(["TQQQ", "--apply"])
check("결과를 모르는 주문이 있으면 옮기지 않는다", rc == 1, "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
