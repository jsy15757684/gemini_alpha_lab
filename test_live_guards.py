#!/usr/bin/env python3
"""실계좌 첫날(2026-10-08) 점검에서 나온 실주문 안전장치 — 가짜 계좌 · 가짜 통신으로 본다.

  · 나무증권 일봉 봇은 미국 거래일마다 한 번만 산다 (모의 TQQQ 9/25: 같은 날 두 번 샀다)
  · 주문 중 정지 · 삭제는 그 주문이 장부에 적힐 때까지 기다린다
  · 주문을 보내던 중 재시작되면 그 주문의 결과를 모르는 것으로 이어 받는다
  · 결과를 모르는 주문이 있는 봇은 지우지 않는다 · 반반 매수의 다음 다리를 내지 않는다
  · 서버 오류(5xx) 주문 응답은 '실패' 가 아니라 '결과 모름'
  · 체결 확인 표시는 봇(스레드)마다 따로
"""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="guard-test-")
from services import botstore, tradelog              # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

import requests                                      # noqa: E402
from services import namuh, trader, strategy          # noqa: E402
from services.namuh import NamuhAccount, OrderUnknown  # noqa: E402

namuh._NH_MIN_INTERVAL = 0.0
namuh.UNKNOWN_RESOLVE_SEC = 0.2
namuh.UNKNOWN_POLL_SEC = 0.05
namuh.FILL_LOOKUP_WAIT = 0.0
namuh.market_session = lambda now_utc=None: {"open": True, "etTime": "10:00", "reason": ""}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


P = strategy.StrategyParams(strategyType="raoer_infinite", raoerVersion="v4", splitCount=40,
                            targetProfitPct=10.0, locMode="half_half_now", feePct=0.0)


class FakeAcct:
    configured = True

    def __init__(self, buy_delay=0.0, buy_err=None):
        self.buy_delay, self.buy_err, self.calls = buy_delay, buy_err, []

    def market_buy(self, sym, amount_usd=0.0, units=0.0, order_type="", limit_price=0.0, **k):
        self.calls.append(("buy", units, limit_price))
        if self.buy_delay:
            time.sleep(self.buy_delay)
        if self.buy_err:
            raise self.buy_err
        return {"orderId": str(len(self.calls)), "units": float(int(units)), "fillPrice": None}

    def market_sell(self, sym, units, **k):
        self.calls.append(("sell", units))
        return {"orderId": "s", "units": float(int(units)), "fillPrice": None}

    def held_qty(self, sym):
        return 0.0


def bot(fa, interval="24h"):
    b = trader.TradingBot(bot_id=f"g-{time.time()}", coin="TQQQ", interval=interval, mode="LIVE",
                          capital_krw=4000.0, params=P, namuh_account=fa, broker="namuh")
    b._fetch_price = lambda: 80.0
    return b


print("── 미국 거래일마다 한 번 ──")
b = bot(FakeAcct())
day1_prev_bar, day1_bar = 1_791_000_000_000, 1_791_086_400_000
check("새 봇은 첫 판단에서 산다 (받아 둔 봉이 전날 것이어도)", b._bar_gate(day1_prev_bar), "")
b.buy_session = b._ny_date()
check("같은 거래일에 오늘 봉이 새로 생겨도 다시 사지 않는다 (9/25 두 번 산 원인)", not b._bar_gate(day1_bar), "")
b.buy_session = "2026-01-02"
check("다음 거래일에는 산다", b._bar_gate(day1_bar), "")
c = bot(FakeAcct(), interval="1h")
c._last_bar_time = day1_bar
check("나무증권 일봉이 아닌 봇은 예전처럼 새 봉마다", c._bar_gate(day1_bar + 3_600_000) and not c._bar_gate(day1_bar), "")
snap = bot(FakeAcct()).snapshot()
snap.update(lastBarTime=1791379800000, buySession=None)       # 2026-10-07 09:30 ET 봉
r = trader.TradingBot.restore(snap, None, FakeAcct())
check("이 칸이 없던 봇은 마지막 일봉의 뉴욕 날짜로 이어 받는다", r.buy_session == "2026-10-07", f"{r.buy_session}")

print("── 주문 중 정지 · 삭제 ──")
fa = FakeAcct(buy_delay=0.6)
b = bot(fa)
t = threading.Thread(target=b._enter_chunk, args=(80.0, 200.0, "1회차"))
t.start()
time.sleep(0.1)
t0 = time.time()
ok = b.stop(liquidate=True)
waited = time.time() - t0
t.join()
check("정지는 진행 중인 매수가 장부에 적힐 때까지 기다린다", waited >= 0.4 and fa.calls[0][0] == "buy", f"{waited:.2f}초")
check("기다린 뒤 그 매수 물량까지 청산한다 (장부에 주인 없는 주식이 남지 않는다)",
      ok and not b.pos.open and [c_[0] for c_ in fa.calls] == ["buy", "sell"], f"{fa.calls}")
n_before = len(fa.calls)
b._enter_chunk(80.0, 200.0, "정지 뒤")
check("정지된 봇은 새 매수를 시작하지 않는다", len(fa.calls) == n_before, "")

trader.ORDER_LOCK_WAIT_SEC = 0.2
fb = FakeAcct(buy_delay=1.0)
b2 = bot(fb)
b2.is_running = True
t = threading.Thread(target=b2._enter_chunk, args=(80.0, 200.0, "1회차"))
t.start()
time.sleep(0.1)
try:
    b2.stop(liquidate=True)
    ok = False
except trader.LiquidationFailed as e:
    ok = "주문을 처리하는 중" in str(e) and b2.is_running
t.join()
check("주문이 오래 걸리면 정지하지 않고 그대로 둔다 (잠시 뒤 다시)", ok, "")
trader.ORDER_LOCK_WAIT_SEC = 150.0

mgr = trader.BotManager()
b3 = bot(FakeAcct())
b3.order_hold = {"side": "buy", "at": "2026-10-08 23:31:05"}
mgr.bots[b3.bot_id] = b3
try:
    mgr.delete(b3.bot_id)
    ok = False
except trader.LiquidationFailed as e:
    ok = "결과를 모르는 주문" in str(e) and b3.bot_id in mgr.bots
check("결과를 모르는 주문이 있는 봇은 지우지 않는다 (확인할 기록이 사라진다)", ok, "")

print("── 주문을 보내던 중 재시작 ──")


class Crash(Exception):
    pass


fc = FakeAcct()
seen = {}


def crash_buy(*a, **k):
    seen["snap"] = b4.snapshot()          # 주문 중 저장된 상태 = 이 순간 프로세스가 죽었을 때 디스크
    raise Crash()


fc.market_buy = crash_buy
b4 = bot(fc)
try:
    b4._nh_buy("TQQQ", amount_usd=80.0, units=1)
except Crash:
    pass
check("주문 직전에 '보내는 중' 을 저장한다", bool(seen["snap"].get("inflightOrder")), f"{seen['snap'].get('inflightOrder')}")
r = trader.TradingBot.restore(seen["snap"], None, FakeAcct())
check("그 상태로 재시작하면 결과를 모르는 주문으로 이어 받아 새 주문을 멈춘다",
      bool(r.order_hold) and "재시작" in r.order_hold.get("message", ""), f"{r.order_hold and r.order_hold.get('message', '')[:30]}")
check("주문이 끝나면 '보내는 중' 을 지운다", b4._inflight is None and not b4.snapshot().get("inflightOrder"), "")

print("── 반반 매수: 앞 다리 결과를 모르면 다음 다리를 내지 않는다 ──")
fd = FakeAcct(buy_err=OrderUnknown("응답 끊김 (시험)", side="buy", ticker="TQQQ", qty=1, price=80, qty_before=0))
b5 = bot(fd)
b5.pos.units, b5.pos.entryPrice, b5.pos.turn, b5.pos.totalInvested = 2.0, 80.0, 1, 160.0
b5.cash = 3840.0
b5._enter_chunk(79.0, 400.0, "2회차")
check("앞 다리가 결과 모름이면 멈추고 다음 다리는 보내지 않는다", len(fd.calls) == 1 and bool(b5.order_hold), f"{fd.calls}")

print("── 서버 오류(5xx) 주문 응답 ──")


class _Resp:
    def __init__(self, code, payload):
        self.status_code, self._p, self.text = code, payload, ""

    def json(self):
        return self._p


def post(url, **kw):
    if url.endswith("/order/v1/buy") or url.endswith("/order/v1/sell"):
        return _Resp(504, {"rsp_cd": "IGW50400", "rsp_msg": "Gateway Timeout"})
    if url.endswith("/inquiry/v1/buyableAmount"):
        return _Resp(500, {})
    raise AssertionError(url)


requests.post = post
a = NamuhAccount("APPKEY", "SECRET", "12345678901")
a._token, a._token_expires_at = "FAKE", time.time() + 9999
a.get_balance = lambda fresh=False: {"qtyByTicker": {"TQQQ": 2.0}, "usdAvailable": 9_999.0}
a.held_qty = lambda sym: 2.0
namuh._price_cache["TQQQ"] = (time.time(), 80.0)
for side in ("buy", "sell"):
    try:
        a.market_buy("TQQQ", units=1) if side == "buy" else a.market_sell("TQQQ", 1)
        res = "no error"
    except OrderUnknown:
        res = "unknown"
    except namuh.NamuhError as e:
        res = "plain: " + e.message[:30]
    check(f"{'매수' if side == 'buy' else '매도'} 주문에 504 + JSON 오류 본문이 오면 '결과 모름' (예전에는 확실한 실패)",
          res == "unknown", res)

print("── 체결 확인 표시는 스레드마다 ──")
acc = NamuhAccount("APPKEY", "SECRET", "12345678901")
seen2 = {}


def worker(v, name):
    acc._fill_unreadable = v
    time.sleep(0.05)
    seen2[name] = acc._fill_unreadable


ts = [threading.Thread(target=worker, args=(True, "A")), threading.Thread(target=worker, args=(False, "B"))]
[x.start() for x in ts]
[x.join() for x in ts]
check("한 봇이 다른 봇의 '잔고를 못 읽음' 표시를 덮지 않는다", seen2 == {"A": True, "B": False}, f"{seen2}")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
