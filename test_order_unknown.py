#!/usr/bin/env python3
"""결과를 모르는 해외 주문 (namuh.OrderUnknown) — 두 번 사지 않는가.

2026-09-30 22:19~22:50 서버 → 나무증권 경로가 끊겼다 끊겼다 했다. 그때 주문을 보냈다면
응답만 끊기고 주문은 접수됐을 수 있다. 예전 코드는 그것을 '실패' 로 처리해, 봇이 다음
판단에서 같은 회차를 다시 주문할 수 있었다. 가짜 통신으로 그 상황들을 만든다.
나무증권에 아무것도 보내지 않는다.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="unknown-test-")
from services import botstore, tradelog              # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

import requests                                      # noqa: E402
from services import namuh, trader, strategy          # noqa: E402
from services.namuh import NamuhAccount, NamuhError, OrderUnknown  # noqa: E402

namuh._NH_MIN_INTERVAL = 0.0
namuh.UNKNOWN_RESOLVE_SEC = 0.3
namuh.UNKNOWN_POLL_SEC = 0.05
namuh.market_session = lambda now_utc=None: {"open": True, "etTime": "10:00", "reason": ""}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


class _Resp:
    def __init__(self, code=200, payload=None, text="", bad_json=False):
        self.status_code, self._p, self.text, self._bad = code, payload or {}, text, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._p


ORDERS, CANCELS = [], []


def route(order=None, cancel_ok=True):
    """주문 POST 의 반응을 정한다. order: 예외 인스턴스 · _Resp · None(접수 성공)."""
    def post(url, **kw):
        if url.endswith("/order/v1/cancel"):
            CANCELS.append(kw["json"]["Input_0"])
            if not cancel_ok:
                raise requests.ReadTimeout("취소 응답 끊김 (시험)")
            return _Resp(200, {"rsp_cd": "00192", "Output_0": {"orr_no": 9}})
        if "/order/v1/" in url:
            ORDERS.append(url.rsplit("/", 1)[-1])
            if isinstance(order, Exception):
                raise order
            if isinstance(order, _Resp):
                return order
            return _Resp(200, {"rsp_cd": "00171", "Output_0": {"orr_no": 548597}})
        raise AssertionError(f"예상하지 못한 호출 {url}")
    requests.post = post


def acct(seq):
    """seq: held_qty 가 차례로 돌려줄 값 (예외 인스턴스면 그 조회는 실패)."""
    a = NamuhAccount("APPKEY", "SECRET", "12345678901")
    a._token, a._token_expires_at = "FAKE", time.time() + 9999
    it = iter(seq)
    last = [None]

    def held(sym):
        try:
            v = next(it)
        except StopIteration:
            v = last[0]
        last[0] = v
        if isinstance(v, Exception):
            raise v
        return float(v)
    a.held_qty = held
    a.get_balance = lambda fresh=False: {"qtyByTicker": {"TQQQ": 2.0}, "usdAvailable": 99_999.0}
    a._await_fill = lambda *x, **k: NamuhAccount._await_fill(a, *x, tries=2, wait=0.0)
    namuh._price_cache["TQQQ"] = (time.time(), 75.00)
    return a


def reset():
    ORDERS.clear()
    CANCELS.clear()


DOWN = NamuhError("잔고 조회 통신 오류 (시험)")

print("── 주문 응답이 끊겼을 때 ──")
reset(); route(requests.ReadTimeout("읽기 시간 초과 (시험)"))
r = acct([2, 2, 7]).market_buy("TQQQ", units=5)
check("응답이 끊겨도 잔고로 전량 체결이 보이면 정상 체결로 돌려준다 (장부에 평소대로 적힌다)",
      r["status"] == "FILLED" and r["units"] == 5 and r.get("resolvedFromUnknown") and ORDERS == ["buy"], f"{r}")

reset(); route(requests.ReadTimeout("읽기 시간 초과 (시험)"))
try:
    acct([2]).market_buy("TQQQ", units=5)
    check("응답이 끊기고 잔고도 그대로면 '결과 모름' (다시 주문하지 않는다)", False, "예외가 없었다")
except OrderUnknown as e:
    check("응답이 끊기고 잔고도 그대로면 '결과 모름' (다시 주문하지 않는다)",
          e.side == "buy" and e.qty == 5 and e.filled == 0 and ORDERS == ["buy"], e.message[:70])
    check("주문번호를 모르면 거둘 수 없다고 알린다 (앱에서 확인)", "주문번호를 몰라" in e.message and CANCELS == [], "")
check("'결과 모름' 도 NamuhError 다 — 기존 호출부가 '이번엔 실패' 로 끝낸다", issubclass(OrderUnknown, NamuhError), "")

reset(); route(requests.ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed'))"))
try:
    acct([2]).market_buy("TQQQ", units=5)
    ok = False
except OrderUnknown:
    ok = True
except NamuhError:
    ok = False
check("연결 끊김(RemoteDisconnected — 9/30 실제 오류)은 보낸 뒤일 수 있어 '결과 모름'", ok, "")

for name, exc in (("연결 시간 초과(ConnectTimeout)", requests.ConnectTimeout("connect timeout (시험)")),
                  ("연결 자체 실패(NewConnectionError)",
                   requests.ConnectionError("HTTPSConnectionPool: Max retries exceeded (Caused by NewConnectionError('Failed to establish'))"))):
    reset(); route(exc)
    try:
        acct([2]).market_buy("TQQQ", units=5)
        res = "no error"
    except OrderUnknown:
        res = "unknown"
    except NamuhError as e:
        res = "plain" if "나가기 전" in e.message else "plain?"
    check(f"{name}은 주문이 나가지 않은 것이 확실 — 평범한 실패", res == "plain", res)

reset(); route(_Resp(502, text="<html>Bad Gateway</html>", bad_json=True))
try:
    acct([2]).market_buy("TQQQ", units=5)
    res = "no error"
except OrderUnknown:
    res = "unknown"
except NamuhError:
    res = "plain"
check("응답을 못 읽은 5xx 는 '결과 모름'", res == "unknown", res)
reset(); route(_Resp(403, text="Forbidden", bad_json=True))
try:
    acct([2]).market_buy("TQQQ", units=5)
    res = "no error"
except OrderUnknown:
    res = "unknown"
except NamuhError:
    res = "plain"
check("응답을 못 읽은 4xx 는 거부 — 평범한 실패", res == "plain", res)

print("── 접수는 됐는데 ──")
reset(); route(None)
try:
    acct([DOWN, DOWN, DOWN]).market_buy("TQQQ", units=5)
    ok, msg = False, "예외가 없었다"
except OrderUnknown as e:
    ok, msg = e.order_id == "548597" and len(CANCELS) == 1, e.message[:70]
check("체결을 확인할 잔고를 한 번도 못 읽으면 '안 붙었다' 가 아니라 '모른다' · 주문번호로 거둔다", ok, msg)

reset(); route(None)
r = acct([DOWN, DOWN, 7]).market_buy("TQQQ", units=5)
check("잔고를 처음엔 못 읽어도 다시 확인해 체결이 보이면 정상 체결", r["units"] == 5 and r["status"] == "FILLED", f"{r['units']}")

reset(); route(None, cancel_ok=False)
try:
    acct([2, 2]).market_buy("TQQQ", units=5)
    ok, msg = False, "예외가 없었다"
except OrderUnknown as e:
    ok, msg = "취소도 되지 않았습니다" in e.message, e.message[:60]
except NamuhError as e:
    ok, msg = False, "평범한 실패로 끝났다: " + e.message[:40]
check("안 붙었는데 취소도 실패하면 주문이 살아 있을 수 있다 — '결과 모름'", ok, msg)

reset(); route(None)
try:
    acct([2, 2]).market_buy("TQQQ", units=5)
    ok = False
except OrderUnknown:
    ok = False
except NamuhError as e:
    ok = "체결되지 않았습니다" in e.message and len(CANCELS) == 1
check("안 붙었고 취소가 확인되면 평범한 미체결 (다음 판단에서 다시 사도 된다)", ok, "")

reset(); route(None)
r = acct([2, 4, 4]).market_buy("TQQQ", units=5)
check("일부 체결: 체결분만 돌려주고 남은 주문을 거둔다", r["units"] == 2 and r["status"] == "PARTIAL"
      and len(CANCELS) == 1 and r["remainderUnknown"] is False, f"{r['units']} · 취소 {len(CANCELS)}")
reset(); route(None, cancel_ok=False)
r = acct([2, 4, 4]).market_buy("TQQQ", units=5)
check("일부 체결인데 남은 주문을 못 거두면 표시한다 (봇이 멈춘다)", r["remainderUnknown"] is True, "")

print("── LOC · 매도 ──")
reset(); route(requests.ReadTimeout("t"))
t0 = time.time()
try:
    acct([2]).market_buy("TQQQ", units=5, order_type=namuh.ORD_LOC, limit_price=70.0, await_fill=False)
    ok = False
except OrderUnknown as e:
    ok = "LOC" in e.message and time.time() - t0 < 0.2
check("LOC 접수 응답이 끊기면 바로 '결과 모름' (마감에 붙으니 지금 잔고로는 알 수 없다)", ok, "")

reset(); route(requests.ReadTimeout("t"))
a = acct([5, 5, 2])
r = a.market_sell("TQQQ", 3)
check("매도 응답이 끊겨도 잔고가 줄었으면 정상 체결", r["units"] == 3 and r.get("resolvedFromUnknown")
      and r["proceedsUsd"] > 0, f"{r}")
reset(); route(requests.ReadTimeout("t"))
try:
    acct([5]).market_sell("TQQQ", 3)
    ok = False
except OrderUnknown as e:
    ok = e.side == "sell"
check("매도 응답이 끊기고 잔고가 그대로면 '결과 모름'", ok, "")

print("── 봇: 멈추고, 맞으면 다시 움직인다 ──")
params = strategy.StrategyParams(strategyType="raoer_infinite", raoerVersion="v4", splitCount=40,
                                 targetProfitPct=10.0, locMode="half_half_now", feePct=0.0)


class _FakeAcct:
    configured = True

    def __init__(self):
        self.qty = 0.0
        self.calls = 0

    def market_buy(self, *a, **k):
        self.calls += 1
        raise OrderUnknown("TQQQ 매수 주문 결과를 확인하지 못했습니다 (시험)", side="buy", ticker="TQQQ",
                           qty=1, price=75.0, qty_before=0)

    def held_qty(self, sym):
        return self.qty


fa = _FakeAcct()
bot = trader.TradingBot(bot_id="test-unknown", coin="TQQQ", interval="1h", mode="LIVE", capital_krw=4000.0,
                        params=params, namuh_account=fa, broker="namuh")
before = (bot.pos.units, bot.pos.turn, bot.cash)
bot._enter_chunk(price=75.0, invest_krw=100.0, reason="1회차")
check("결과를 모르는 주문이면 장부를 바꾸지 않는다", (bot.pos.units, bot.pos.turn, bot.cash) == before,
      f"{before} → {(bot.pos.units, bot.pos.turn, bot.cash)}")
check("봇이 멈춤 상태가 된다 (주문 정보를 남긴다)", bool(bot.order_hold) and bot.order_hold["side"] == "buy"
      and bot.order_hold.get("etDate"), f"{bot.order_hold and bot.order_hold.get('message', '')[:40]}")
check("멈춤은 저장된다 — 재시작해도 이어진다",
      trader.TradingBot.restore(bot.snapshot(), None, fa).order_hold == bot.order_hold
      if "namuh_account" in trader.TradingBot.restore.__code__.co_varnames else bot.snapshot().get("orderHold") == bot.order_hold, "")
check("화면 상태에도 나온다", bot.status().get("orderHold") == bot.order_hold, "")

import services.market_schedule as ms                 # noqa: E402
orig_status = ms.get_us_market_status
try:
    ms.get_us_market_status = lambda now=None: {"isOpen": True}
    fa.qty = 0.0
    bot._hold_checked_at = 0
    check("장중이면 계좌가 장부와 맞아도 기다린다 (그 주문이 아직 살아 있을 수 있다)", bot._order_hold_active()
          and "정규장이 끝날 때까지" in bot.last_decision, bot.last_decision[:60])
    check("1분 안에는 계좌를 다시 묻지 않는다 (호출 한도)", bot._order_hold_active(), "")
    ms.get_us_market_status = lambda now=None: {"isOpen": False}
    fa.qty = 1.0
    bot._hold_checked_at = 0
    check("마감 뒤 계좌 ≠ 장부(주문이 실제로 붙었다)면 계속 멈추고 확인하라고 알린다",
          bot._order_hold_active() and "계좌 1주 ≠ 장부 0주" in bot.last_decision, bot.last_decision[:70])
    fa.qty = 0.0
    bot._hold_checked_at = 0
    check("마감 뒤 계좌 = 장부면 풀린다", bot._order_hold_active() is False and bot.order_hold is None, "")
finally:
    ms.get_us_market_status = orig_status

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
