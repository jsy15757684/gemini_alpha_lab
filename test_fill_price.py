#!/usr/bin/env python3
"""실제 체결가 기록 — 주문체결내역(/gbstock/inquiry/v1/unexecuted)의 체결가로 장부를 적는가.

지정가는 한도보다 싸게(매도는 비싸게) 붙는다(2026-10-01 TQQQ 한도 $82.22 → 체결 $77.68).
장부 · 손익 · 양도세 기록은 실제 체결가여야 한다. 가짜 통신 · 가짜 계좌로 본다.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="fill-test-")
from services import botstore, tradelog              # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

import requests                                      # noqa: E402
from services import namuh, trader, strategy          # noqa: E402
from services.namuh import NamuhAccount               # noqa: E402

namuh._NH_MIN_INTERVAL = 0.0
namuh.FILL_LOOKUP_WAIT = 0.0
namuh.market_session = lambda now_utc=None: {"open": True, "etTime": "10:00", "reason": ""}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


class _Resp:
    def __init__(self, code=200, payload=None):
        self.status_code, self._p, self.text = code, payload or {}, ""

    def json(self):
        return self._p


# 2026-10-02 모의계좌 실측 응답 모양 (iem_cd 는 ISIN)
EXEC = {"20261001": [{"orr_no": 3752, "orr_dt": "20261001", "iem_cd": "US74347X8314", "orr_qty": 1,
                      "fc_orr_uit_pr": 78.3, "cns_qty": 1, "cns_pr": 77.67, "ny_cns_orr_qty": 0, "can_qty": 0},
                     {"orr_no": 3753, "orr_dt": "20261001", "iem_cd": "US74347X8314", "orr_qty": 1,
                      "fc_orr_uit_pr": 82.22, "cns_qty": 1, "cns_pr": 77.68, "ny_cns_orr_qty": 0, "can_qty": 0}]}
SEEN = []


def post(url, **kw):
    inp = (kw.get("json") or {}).get("Input_0", {})
    if url.endswith("/inquiry/v1/unexecuted"):
        SEEN.append(inp.get("orr_dt"))
        return _Resp(200, {"rsp_cd": "00000", "Output_0": EXEC.get(inp.get("orr_dt"), [])})
    if url.endswith("/order/v1/buy") or url.endswith("/order/v1/sell"):
        return _Resp(200, {"rsp_cd": "00171", "Output_0": {"orr_no": 3753}})
    raise AssertionError(url)


requests.post = post


def acct(dates=("20261001", "20261002")):
    a = NamuhAccount("APPKEY", "SECRET", "12345678901")
    a._token, a._token_expires_at = "FAKE", time.time() + 9999
    a._order_dates = lambda: list(dates)
    return a


print("── 주문체결내역 ──")
a = acct()
f = a.order_fill(3753)
check("주문번호로 찾는다 (종목 칸은 ISIN 이라 쓰지 않는다)", f and f["price"] == 77.68 and f["qty"] == 1 and f["limit"] == 82.22, f"{f}")
SEEN.clear()
f = acct(("20261002", "20261001")).order_fill(3752)
check("미국 날짜 · 한국 날짜 둘 다 본다 (한국 0~5시 주문은 날짜가 갈릴 수 있다)", f and f["price"] == 77.67
      and SEEN == ["20261002", "20261001"], f"{SEEN}")
check("없는 주문번호면 None", acct().order_fill(9999) is None, "")
check("주문번호가 숫자가 아니면 조회하지 않는다 (UNKNOWN-…)", acct()._fill_price("UNKNOWN-1", 1) is None, "")

print("── 주문 결과에 실제 체결가 ──")
b = acct()
b.get_balance = lambda fresh=False: {"qtyByTicker": {"TQQQ": 2.0}, "usdAvailable": 9_999.0}
b._await_fill = lambda *x, **k: 1.0
namuh._price_cache["TQQQ"] = (time.time(), 80.0)
r = b.market_buy("TQQQ", units=1, order_type=namuh.ORD_LIMIT, limit_price=82.22)
check("매수 결과: 한도는 price · 실제 체결가는 fillPrice · 금액은 체결가로", r["price"] == 82.22
      and r["fillPrice"] == 77.68 and abs(r["amountUsd"] - 77.68) < 1e-9, f"{r}")
c = acct()
c.get_balance = lambda fresh=False: {"qtyByTicker": {"TQQQ": 2.0}, "usdAvailable": 9_999.0}
c.held_qty = lambda sym: 2.0
c._await_fill = lambda *x, **k: 1.0
EXEC["20261001"][1]["cns_pr"] = 0                     # 아직 체결가가 안 올라왔다
r = c.market_sell("TQQQ", 1)
check("체결가를 못 찾으면 fillPrice=None · 금액은 주문 가격으로 (주문 결과는 그대로)", r["fillPrice"] is None
      and r["units"] == 1 and r["proceedsUsd"] == r["price"], f"{r}")
EXEC["20261001"][1]["cns_pr"] = 77.68

print("── 봇 장부 ──")


class FakeAcct:
    configured = True

    def __init__(self, buy_px, sell_px=None, sell_fill=None):
        self.buy_px, self.sell_px, self.sell_fill = list(buy_px), sell_px, sell_fill
        self.n = 0

    def market_buy(self, sym, amount_usd=0.0, units=0.0, order_type="", limit_price=0.0, **k):
        self.n += 1
        px = self.buy_px.pop(0) if self.buy_px else None
        return {"orderId": str(self.n), "units": float(int(units)), "price": limit_price or 0, "fillPrice": px}

    def market_sell(self, sym, units, **k):
        got = float(self.sell_fill if self.sell_fill is not None else int(units))
        return {"orderId": "s", "units": got, "fillPrice": self.sell_px}


def bot(fa, locMode="half_half_now", fee=0.25):
    p = strategy.StrategyParams(strategyType="raoer_infinite", raoerVersion="v4", splitCount=40,
                                targetProfitPct=10.0, locMode=locMode, feePct=fee)
    return trader.TradingBot(bot_id=f"fill-{time.time()}", coin="TQQQ", interval="1h", mode="LIVE",
                             capital_krw=4000.0, params=p, namuh_account=fa, broker="namuh")


fa = FakeAcct([77.40])
bt = bot(fa)
bt._enter_chunk(price=78.00, invest_krw=200.0, reason="1회차")
q = bt.pos.units
check("첫 매수: 장부 평단 = 실제 체결가 (판단 시세 $78.00 이 아니라 $77.40)", q >= 1 and abs(bt.pos.entryPrice - 77.40) < 1e-9,
      f"{q:g}주 @ ${bt.pos.entryPrice}")
check("현금은 체결가 × (1 + 수수료) 만큼 줄었다", abs((4000.0 - bt.cash) - q * 77.40 * 1.0025) < 1e-6,
      f"차감 ${4000.0 - bt.cash:,.4f}")
check("매매 일지에도 실제 체결가 (일지는 최신이 앞)", abs(bt.trade_history[0]["price"] - 77.40) < 1e-9 if bt.trade_history else False, "")

fa.buy_px = [76.10, 77.90]                             # 반반 두 다리 (평단 · 평단+5%) 체결가가 다르다
u0, p0 = bt.pos.units, bt.pos.entryPrice
bt._enter_chunk(price=77.00, invest_krw=200.0, reason="2회차")
got = bt.pos.units - u0
legs = fa.n - 1
if got >= 2:
    exp_px = (76.10 + 77.90) / 2 if got == 2 else None
else:
    exp_px = 76.10
new_avg = (u0 * p0 + got * (exp_px or 0)) / bt.pos.units if exp_px else None
check("반반 두 다리: 두 체결가의 가중 평균으로 평단을 다시 낸다", got >= 1 and exp_px is not None
      and abs(bt.pos.entryPrice - new_avg) < 1e-6, f"{got:g}주 · 평단 ${bt.pos.entryPrice:,.4f}")

fb = FakeAcct([None])
bb = bot(fb)
bb._enter_chunk(price=78.00, invest_krw=200.0, reason="1회차")
check("체결가를 모르면 판단 시점 시세로 적는다 (지금과 같다)", abs(bb.pos.entryPrice - 78.00) < 1e-9, f"${bb.pos.entryPrice}")

fs = FakeAcct([77.40], sell_px=85.30)
bs = bot(fs)
bs._enter_chunk(price=77.40, invest_krw=400.0, reason="1회차")
units, cost = bs.pos.units, bs.pos.totalInvested
bs._exit(price=85.00, reason="익절")
want = units * 85.30 * (1 - 0.0025) - cost
check("전량 매도: 손익은 실제 체결가 $85.30 으로", not bs.pos.open and abs(bs.realized_pnl - want) < 1e-6,
      f"손익 ${bs.realized_pnl:,.4f} (기대 ${want:,.4f})")

fp = FakeAcct([77.40], sell_px=85.30, sell_fill=1)
bp = bot(fp)
bp._enter_chunk(price=77.40, invest_krw=400.0, reason="1회차")
have = bp.pos.units
bp._exit(price=85.00, reason="익절")
check("전량 매도가 일부만 팔리면 판 만큼만 덜고 포지션을 남긴다 (예전에는 전부 판 것으로 적었다)",
      have > 1 and bp.pos.open and abs(bp.pos.units - (have - 1)) < 1e-9
      and bp.trade_history[0]["action"] == "SELL_PARTIAL", f"{have:g} → {bp.pos.units:g}주")

fq = FakeAcct([77.40] * 3, sell_px=70.10)
bq = bot(fq)
bq.params.quarterCutPct = 25.0
bq._enter_chunk(price=77.40, invest_krw=800.0, reason="1회차")
before = bq.pos.units
bq._exit_quarter(price=70.00, reason="쿼터")
check("쿼터 매도: 실제 체결가로 적는다", bq.trade_history[0]["action"] == "SELL_QUARTER"
      and abs(bq.trade_history[0]["price"] - 70.10) < 1e-9 and bq.pos.units < before, f"{before:g} → {bq.pos.units:g}주")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
