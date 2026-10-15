#!/usr/bin/env python3
"""무매법 엑셀 방식의 '걸어 두는' 지정가 매도 — 평단×(1+목표%) 를 장 시작 때 걸고 체결을 주문번호로 적는가.

예전에는 봇이 시세를 보다가 목표가에 닿으면 그때 매도했다. 장중에 잠깐 찍고 내려오면 놓치고,
즉시 체결용 지정가(현재가 −0.5%)라 목표가보다 낮게 팔릴 수 있었다. 가짜 계좌로 본다.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="excel-sell-test-")
from services import botstore, tradelog              # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

from services import namuh, trader, strategy, market_schedule   # noqa: E402
from services.namuh import OrderUnknown, NamuhError              # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


WIN = {"in": True, "sessionDate": "2026-10-13", "pastClose": False}
market_schedule.loc_window = lambda *a, **k: dict(WIN)


class FakeAcct:
    configured = True

    def __init__(self, qty=5.0):
        self.qty, self.calls, self.fills, self.sell_err, self.cancel_err = qty, [], {}, None, None

    def market_sell(self, sym, units, order_type="", limit_price=0.0, await_fill=True, **k):
        self.calls.append(("sell", int(units), order_type, limit_price, await_fill))
        if self.sell_err:
            raise self.sell_err
        if await_fill:
            self.qty -= int(units)
            return {"orderId": "M1", "units": float(int(units)), "fillPrice": None}
        return {"orderId": f"S{len(self.calls)}", "units": 0.0, "requestedUnits": float(int(units))}

    def market_buy(self, *a, **k):
        self.calls.append(("buy",))
        return {"orderId": "B", "units": 0.0}

    def order_fill(self, oid):
        return self.fills.get(str(oid))

    def cancel_order(self, oid, sym, qty=0.0):
        self.calls.append(("cancel", str(oid)))
        if self.cancel_err:
            raise self.cancel_err
        return {"cancelOrderId": "C"}

    def held_qty(self, sym):
        return self.qty

    def get_balance(self, fresh=False):
        return {"qtyByTicker": {"TQQQ": self.qty}, "holdings": {"TQQQ": {"avgPrice": 80.81}}}


def bot(fa):
    p = strategy.StrategyParams.from_dict({"strategyType": "raoer_infinite", "locMode": "excel",
                                           "targetProfitPct": 10.0, "feePct": 0.0})
    b = trader.TradingBot(bot_id=f"s-{time.time()}", coin="TQQQ", interval="24h", mode="LIVE",
                          capital_krw=4000.0, params=p, namuh_account=fa, broker="namuh")
    b._persist = lambda: None
    b.pos.units, b.pos.entryPrice, b.pos.turn, b.pos.totalInvested, b.cash = 5.0, 80.81, 3, 404.05, 3595.95
    b.last_price = 80.0
    return b


def tick(b):
    b._sell_check_at = 0.0
    b._excel_sell_tick()


print("── 장 시작 때 건다 ──")
fa = FakeAcct()
b = bot(fa)
tick(b)
c = fa.calls[0]
check("평단×1.10 지정가 · 보유 전부 · 체결을 기다리지 않는다",
      c == ("sell", 5, namuh.ORD_LIMIT, 88.89, False), f"{c}")
check("주문을 장부에 남긴다 (재시작해도 이어지게)", b.sell_order and b.sell_order["orderId"] == "S1"
      and trader.TradingBot.restore(b.snapshot(), None, FakeAcct()).sell_order["orderId"] == "S1", "")
tick(b)
check("같은 세션에 두 번 걸지 않는다", [x[0] for x in fa.calls].count("sell") == 1, f"{fa.calls}")
check("걸려 있으면 장부는 그대로 (체결 전)", b.pos.units == 5.0, "")

print("── 체결 ──")
fa.fills["S1"] = {"qty": 2.0, "price": 88.9, "canceled": 0.0}
tick(b)
check("일부 체결은 판 만큼만 적고 주문은 둔다", b.pos.units == 3.0 and b.sell_order and b.sell_order["booked"] == 2.0,
      f"{b.pos.units:g}주")
b.pending_orders = [{"orderId": "L1", "leg": "평단 LOC", "units": 1, "limit": 80.81, "session": "2026-10-13"}]
fa.fills["S1"] = {"qty": 5.0, "price": 88.92, "canceled": 0.0}
tick(b)
check("다 팔리면 포지션을 비우고 손익을 적는다", not b.pos.open and b.sell_order is None
      and abs(b.realized_pnl - (2 * 88.9 + 3 * 88.92 - 404.05)) < 1e-6, f"실현 {b.realized_pnl:+.2f}")
check("살아 있는 매수 LOC 를 거둬들이고, 익절한 날은 새 첫 매수를 걸지 않는다",
      ("cancel", "L1") in fa.calls and not b.pending_orders and b.loc_session == "2026-10-13", "")

print("── 마감 뒤 · 다음 날 ──")
fb = FakeAcct()
b2 = bot(fb)
tick(b2)
fb.fills["S1"] = {"qty": 0.0, "price": 0.0, "canceled": 0.0}
WIN.update(pastClose=True, **{"in": False})
tick(b2)
check("안 팔렸으면 마감 뒤 주문을 정리한다 (그날만 유효)", b2.sell_order is None and b2.pos.units == 5.0, "")
b2.pos.units, b2.pos.entryPrice = 7.0, 80.0                # 마감 LOC 정산으로 평단이 바뀌었다
WIN.update(sessionDate="2026-10-14", pastClose=False, **{"in": True})
tick(b2)
check("다음 세션에는 바뀐 평단 · 수량으로 다시 건다", fb.calls[-1][:4] == ("sell", 7, namuh.ORD_LIMIT, 88.0), f"{fb.calls[-1]}")
fb.fills.clear()
fb.qty = 4.0                                               # 꺼져 있는 동안 3주 팔렸다 · 체결내역에서 못 찾음
WIN.update(sessionDate="2026-10-15")
tick(b2)
check("체결내역에서 못 찾으면 계좌 수량으로 정산한다 (지정가로 적음)", b2.pos.units == 4.0 and b2.sell_order is None, f"{b2.pos.units:g}주")
WIN.update(sessionDate="2026-10-13")

print("── 다른 매도 · 정지와 겹칠 때 ──")
fc = FakeAcct()
b3 = bot(fc)
tick(b3)
b3._exit(81.0, "사용자 정지")
kinds = [x[0] for x in fc.calls]
check("청산은 걸어 둔 지정가를 먼저 취소한 뒤 판다 (두 번 팔지 않는다)",
      kinds == ["sell", "cancel", "sell"] and not b3.pos.open, f"{kinds}")
fd = FakeAcct()
b4 = bot(fd)
tick(b4)
fd.cancel_err = NamuhError("취소 거부 (시험)")
fd.fills["S1"] = {"qty": 0.0, "price": 0.0, "canceled": 0.0}
b4._exit(81.0, "사용자 정지")
check("취소가 안 되고 주문이 살아 있으면 새로 팔지 않는다", [x[0] for x in fd.calls].count("sell") == 1
      and b4.pos.units == 5.0 and b4.sell_order, "")
fe = FakeAcct()
b5 = bot(fe)
tick(b5)
b5.stop(liquidate=False)
check("정지(청산 없이)도 걸어 둔 매도를 거둬들인다", ("cancel", "S1") in fe.calls and b5.sell_order is None
      and b5.pos.units == 5.0, "")

print("── 접수 실패 ──")
ff = FakeAcct()
ff.sell_err = NamuhError("주문 거부 (시험)")
b6 = bot(ff)
tick(b6)
check("거부되면 잠시 뒤 다시 건다 (그동안은 시세 감시)", b6.sell_order is None and b6.sell_session is None
      and b6._sell_retry_after > time.time(), "")
fg = FakeAcct()
fg.sell_err = OrderUnknown("응답 끊김 (시험)", side="sell", ticker="TQQQ", qty=5, price=88.89, qty_before=5)
b7 = bot(fg)
tick(b7)
tick(b7)
check("결과를 모르면 멈추고 다시 내지 않는다", bool(b7.order_hold) and [x[0] for x in fg.calls].count("sell") == 1, "")

print("── 쿼터손절 (라오어: 40회 소진 · 현금 소진) ──")
WIN.update(sessionDate="2026-10-13", pastClose=False, **{"in": True})
fq = FakeAcct(qty=6.0)
bq = bot(fq)
bq.pos.units, bq.pos.turn, bq.pos.totalInvested, bq.cash = 6.0, 40, 484.86, 3515.14
bq.last_price = 70.0
tick(bq)
kinds = [(x[0], x[1], x[4]) for x in fq.calls]
check("40회 소진: 25%(6주 중 2주)를 먼저 팔고, 남은 4주에 지정가 매도를 건다",
      kinds == [("sell", 2, True), ("sell", 4, False)], f"{kinds}")
check("회차를 10 되돌리고 판 돈이 현금에 들어간다", bq.pos.turn == 30 and bq.pos.units == 4.0
      and abs(bq.cash - (3515.14 + 2 * 70.0)) < 1e-6 and bq.quarter_session == "2026-10-13",
      f"T={bq.pos.turn} 현금 ${bq.cash:,.2f}")
bq.sell_order, bq.sell_session, bq.pos.turn = None, None, 40
tick(bq)
check("같은 세션에 두 번 손절하지 않는다", [x[1] for x in fq.calls if x[0] == "sell" and x[4]] == [2], f"{fq.calls}")

fr = FakeAcct(qty=6.0)
br = bot(fr)
br.pos.units, br.pos.turn, br.cash = 6.0, 25, 5.0
br.last_price = 70.0
tick(br)
check("현금 소진(오늘 낼 매수가 없음)도 쿼터손절", fr.calls and fr.calls[0][:2] == ("sell", 2) and fr.calls[0][4]
      and br.pos.turn == 15, f"{fr.calls[:1]} T={br.pos.turn}")

fs = FakeAcct(qty=6.0)
bs = bot(fs)
bs.pos.units, bs.pos.turn = 6.0, 4
tick(bs)
check("회차 · 현금이 남아 있으면 손절하지 않는다", [x[4] for x in fs.calls if x[0] == "sell"] == [False]
      and bs._quarter_checked == "2026-10-13", f"{fs.calls}")

ft = FakeAcct(qty=6.0)
bt = bot(ft)
bt.pos.units, bt.pos.turn = 6.0, 40
bt.pending_orders = [{"orderId": "L9", "leg": "평단 LOC", "units": 1, "limit": 80.0, "session": "2026-10-12"}]
tick(bt)
check("지난 세션 LOC 정산 전이면 손절 · 매도를 미룬다 (장부가 옛것)", ft.calls == [] and bt._quarter_checked is None, "")

fu = FakeAcct(qty=1.0)
bu = bot(fu)
bu.pos.units, bu.pos.turn, bu.pos.totalInvested = 1.0, 40, 80.81
bu.last_price = 70.0
tick(bu)
check("1주만 들고 손절하면 포지션을 비운다", not bu.pos.open and bu.pos.turn == 0, f"T={bu.pos.turn}")

pp = trader.TradingBot(bot_id="pq", coin="TQQQ", interval="24h", mode="PAPER", capital_krw=4000.0,
                       params=strategy.StrategyParams.from_dict({"strategyType": "raoer_infinite", "locMode": "excel",
                                                                "feePct": 0.0}), broker="namuh")
pp._persist = lambda: None
pp.pos.units, pp.pos.entryPrice, pp.pos.turn, pp.pos.totalInvested, pp.cash = 8.0, 80.0, 40, 640.0, 3360.0
pp.last_price = 72.0
pp._excel_quarter("2026-10-13")
check("모의 봇도 같은 규칙 (8주 중 2주)", pp.pos.units == 6.0 and pp.pos.turn == 30
      and any(t["action"] == "SELL_QUARTER" for t in pp.trade_history), f"{pp.pos.units:g}주")

print("── 나무증권 주문 (가짜 통신) ──")
import requests                                       # noqa: E402
namuh._NH_MIN_INTERVAL = 0.0
namuh.market_session = lambda now_utc=None: {"open": True, "etTime": "10:00", "reason": ""}


class _Resp:
    def __init__(self, code, payload):
        self.status_code, self._p, self.text = code, payload, ""

    def json(self):
        return self._p


MODE = {"code": 200}
SENT = []


def post(url, **kw):
    if url.endswith("/order/v1/sell"):
        SENT.append(kw["json"]["Input_0"])
        if MODE["code"] == 200:
            return _Resp(200, {"rsp_cd": "00171", "Output_0": {"orr_no": 777}})
        return _Resp(504, {"rsp_cd": "IGW50400", "rsp_msg": "Gateway Timeout"})
    raise AssertionError(url)


requests.post = post
a = namuh.NamuhAccount("APPKEY", "SECRET", "12345678901")
a._token, a._token_expires_at = "FAKE", time.time() + 9999
a.held_qty = lambda sym: 5.0
a._await_fill = lambda *x, **k: (_ for _ in ()).throw(AssertionError("체결을 기다리면 안 된다"))
r = a.market_sell("TQQQ", 5, order_type=namuh.ORD_LIMIT, limit_price=88.89, await_fill=False)
check("걸어 두는 지정가는 접수만 확인하고 돌아온다 (체결 대기 · 미체결 취소 없음)",
      r["orderId"] == "777" and r["units"] == 0.0 and SENT[-1]["fc_orr_uit_pr"] == 88.89
      and SENT[-1]["ahi_nmn_pr_tp_cd"] == "00", f"{r}")
MODE["code"] = 504
try:
    a.market_sell("TQQQ", 5, order_type=namuh.ORD_LIMIT, limit_price=88.89, await_fill=False)
    res = "no error"
except OrderUnknown:
    res = "unknown"
except NamuhError as e:
    res = "plain: " + e.message[:30]
check("응답이 서버 오류면 '결과 모름' (다시 내지 않는다)", res == "unknown", res)

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
