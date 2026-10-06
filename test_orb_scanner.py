#!/usr/bin/env python3
"""국내 ORB 스캐너 — 여러 종목을 실시간으로 보다가 신호 뜬 종목을 산다.

실시간 스트림과 나무증권 REST 를 가짜로 바꿔 끼운다. 아무것도 보내지 않는다.

  - 웹소켓 프레임 인코딩/디코딩 (표준 라이브러리 클라이언트)
  - 실시간 체결 메시지(oc) 해석 — 서버에서 받아 본 실제 메시지로
  - 신호 먼저 뜬 순서대로 최대 3종목 · 종목당 자본 ⅓ · 4번째는 기다림
  - 한 종목은 하루 한 번 · 슬롯이 비면 기다리던 신호를 받는다
  - 종목마다 09:05 거래량을 기록해 다음 날 RVOL 기준으로 쓴다
  - 1주 값이 종목당 자본보다 비싸면 건너뛴다
  - 산 종목은 스트림이 멈추면 REST 로 보고 청산한다
  - 저장 → 복원 (포지션 · 오늘 진입 · RVOL 기록)
  - 실전 경로: 시장성 지정가 · 잔고 평단 역산 · 미체결이면 두 번 사지 않음
"""

import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="orbscan-test-")
from services import botstore, tradelog                 # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

from services import krx, orb, ws_lite, krx_stream      # noqa: E402
from services import orb_scanner as osc                  # noqa: E402
from services.namuh import NamuhError                   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def at(hms, date="2026-09-29"):
    h, m, s = (int(x) for x in hms.split(":"))
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, h, m, s, tzinfo=orb.KST)


print("── 웹소켓 프레임 (ws_lite) ──")
fr = ws_lite.encode_frame(b'{"a":1}', mask=b"\x01\x02\x03\x04")
check("클라이언트 프레임은 마스킹한다", fr[1] & 0x80 and fr[0] == 0x81, fr[:2].hex())
rd = ws_lite.FrameReader()
server_frame = bytes([0x81, 5]) + b"hello"
rd.feed(server_frame[:3]); none_yet = rd.next()
rd.feed(server_frame[3:]); got = rd.next()
check("모자라면 기다렸다가 온전한 메시지를 준다", none_yet is None and got == (1, b"hello"), f"{got}")
big = b"x" * 300
rd.feed(bytes([0x81, 126]) + (300).to_bytes(2, "big") + big)
check("126 길이 확장 (300바이트)", rd.next() == (1, big), "")
rd.feed(bytes([0x01, 3]) + b"abc" + bytes([0x89, 0]) + bytes([0x80, 3]) + b"def")
first = rd.next(); second = rd.next()
check("조각난 메시지 사이 ping 을 먼저 주고 조각을 합친다", first == (9, b"") and second == (1, b"abcdef"), f"{first} {second}")

print("── 실시간 체결 메시지 해석 (2026-09-28 서버 수신) ──")
raw = json.loads('{"header": {"tr_cd": "oc", "tr_key": "247540"}, "body": {"code": "247540", "time": "12:24:54", '
                 '"price": "107800", "high": "109900", "low": "102900", "open": "104500", "volume": "209347", '
                 '"avgprice": "107823", "offer": "107900", "bid": "107800", "kospigb": "2", "chrate": "3.1"}}')
t = krx_stream.parse_tick(raw["body"])
check("가격 · 고저 · 시가 · 누적거래량 · VWAP · 호가", (t["price"], t["high"], t["low"], t["open"], t["volume"], t["vwap"], t["ask"], t["bid"])
      == (107800, 109900, 102900, 104500, 209347, 107823.0, 107900, 107800), "")
check("kospigb 2 → 코스닥", t["market"] == "kosdaq" and t["hogaTime"] == "12:24:54", "")
# 2026-09-29 서버 수신 원본 — chrate · change 는 부호 없이 오고 방향은 sign 에만 있다
down = krx_stream.parse_tick({"code": "052690", "sign": "5", "change": "3300", "price": "122900", "chrate": "2.61",
                              "open": "122700", "high": "123800", "low": "121200", "volume": "35173"})
up = krx_stream.parse_tick({"code": "005930", "sign": "2", "change": "2250", "price": "272250", "chrate": "0.83",
                            "open": "266000", "high": "272500", "low": "266000", "volume": "2264594"})
flat = krx_stream.parse_tick({"code": "000001", "sign": "3", "change": "0", "price": "10000", "chrate": "0.00"})
check("하락(sign 5): 전일 종가 = 가격 + 전일 대비 · 등락률은 음수 (한전기술 126,200 · −2.61%)",
      down["prevClose"] == 126200 and down["changePct"] == -2.61, f"{down['prevClose']} · {down['changePct']}")
check("상승(sign 2): 전일 종가 = 가격 − 전일 대비 · 등락률은 양수 (삼성전자 270,000 · +0.83%)",
      up["prevClose"] == 270000 and up["changePct"] == 0.83, f"{up['prevClose']} · {up['changePct']}")
check("보합(sign 3): 전일 종가 = 가격", flat["prevClose"] == 10000 and flat["changePct"] == 0.0, "")
from services import orb as _orb
check("갭하락 출발을 갭상승으로 읽지 않는다 (9/29 한전기술: 실제 −2.77% · 고치기 전 +2.77%)",
      round(_orb.open_gap_pct(down), 2) == -2.77, f"{_orb.open_gap_pct(down):+.2f}%")
check("부호 칸이 없는 메시지는 전일 종가를 모른다고 둔다 (갭을 지어내지 않는다)",
      t["prevClose"] == 0, f"{t['prevClose']}")
st = krx_stream.KrxStream()
st._on_message(json.dumps(raw))
st._on_message(json.dumps({"header": {"tr_type": "1", "tr_cd": "oc", "rsp_cd": "10001", "rsp_msg": "없는 종목"},
                           "body": {"tr_key": ["999999"]}}))
check("체결은 스냅샷에 담고 거부된 구독은 따로 적는다", st.get("247540")["price"] == 107800 and "999999" in st.rejected, "")
try:
    st.ensure(None, [f"{i:06d}" for i in range(31)])
    over = False
except ValueError:
    over = True
check("연결당 30종목 한도를 넘기면 거부한다", over, "")


# ── 가짜 스트림 · 가짜 REST ──
class FakeStream:
    connected = True
    last_msg_at = 0.0

    def __init__(self):
        self.snap, self.ages, self.ensured = {}, {}, []

    def ensure(self, acc, codes):
        self.ensured = list(codes)

    def release(self):
        pass

    def kick(self, why):
        pass

    def get(self, code):
        return dict(self.snap[code]) if code in self.snap else None

    def age(self, code):
        return self.ages.get(code, 0.5) if code in self.snap else None

    def status(self):
        return {"connected": True}


fs = FakeStream()
osc.stream = fs
rest_quotes = {}
orig_quote = krx.quote
krx.quote = lambda acc, code: dict(rest_quotes.get(code) or {**fs.snap[code], "etf": code in ("122630", "233740"),
                                                              "upperLimit": 0, "lowerLimit": 0})


def tick_snap(code, now, price, high, low, vol, vwap=None):
    fs.snap[code] = {"code": code, "price": price, "high": high, "low": low, "open": low, "volume": vol,
                     "vwap": vwap if vwap is not None else price - 50, "ask": price + 10, "bid": price - 10,
                     "hogaTime": now.strftime("%H:%M:%S"), "market": "kospi"}


# 갭 앤 고 조건(시초가 갭 · 시초가 지지)은 test_orb.py 가 본다 — 여기서는 끈다
OFF = {"gapMinPct": 0, "gapMaxPct": 0, "openHold": False}
P = {"rvolMin": 0, "marketFilter": False, "watchMode": "manual", **OFF}
CODES = ["005930", "000660", "035720", "105560"]
bot = osc.OrbScanner("ORB-t", "PAPER", 900_000, {**P, "watchlist": CODES, "maxPositions": 3}, None)
bot.day_date = "2026-09-29"
check("종목당 자본 = 운용자본 ÷ 최대 보유", bot.slot_budget() == 300_000, f"{bot.slot_budget():,.0f}")
check("실시간 구독에 감시 목록이 들어간다", set(CODES) <= set(bot._codes_to_stream()), "")
bot2 = osc.OrbScanner("x", "PAPER", 900_000, {"watchlist": CODES, "watchMode": "manual"}, None)
check("지수 필터를 켜면 지수 ETF 2종도 구독한다", {"069500", "229200"} <= set(bot2._codes_to_stream()), "")


def run(bot_, hms, prices):
    """prices: {code: (price, high, low, vol)} — 그 시각의 스냅샷을 넣고 한 번 판단."""
    now = at(hms)
    for c, (p, h, l, v) in prices.items():
        tick_snap(c, now, p, h, l, v)
    bot_._tick(now, "2026-09-29", orb.minutes_since_open(now))


BASE = {"005930": 270_000, "000660": 50_000, "035720": 34_000, "105560": 170_000}
TICK = {"005930": 500, "000660": 100, "035720": 50, "105560": 100}
for sec in range(5, 300, 10):          # OR 09:00~09:05 · 거래 속도 100주/초
    run(bot, f"09:0{sec // 60}:{sec % 60:02d}",
        {c: (b, b + 2 * TICK[c], b - 2 * TICK[c], 10_000 + 100 * sec) for c, b in BASE.items()})
check("OR 을 종목마다 잰다", all(bot.days[c].orHigh == BASE[c] + 2 * TICK[c] for c in CODES), "")


def breakout(codes, hms_list, vol_rate=400):
    for i, hms in enumerate(hms_list):
        prices = {}
        for c, b in BASE.items():
            v = 10_000 + 100 * 295 + (vol_rate if c in codes else 50) * (20 * i + 20)
            p = b + 4 * TICK[c] if c in codes else b
            prices[c] = (p, max(p, b + 2 * TICK[c]), b - 2 * TICK[c], v)
        run(bot, hms, prices)


breakout(["035720", "000660", "105560", "005930"], ["09:06:00", "09:06:20", "09:06:40"])
held = sorted(bot.positions)
check("신호가 넷 떠도 최대 3종목만 산다", len(bot.positions) == 3, f"{held}")
check("종목당 자본 안에서 정수로 산다",
      all(p.units * p.entryPrice <= 300_000 for p in bot.positions.values()), "")
waiting = [c for c in CODES if c not in bot.positions]
check("못 산 종목은 오늘 끝내지 않고 기다린다", waiting and not bot.days[waiting[0]].done
      and not bot.days[waiting[0]].entered, f"{waiting}")
check("09:05 거래량을 종목마다 기록한다 (내일 RVOL 기준)",
      all(bot.or_vol_history.get(c, {}).get("2026-09-29") == 10_000 + 100 * 295 for c in CODES), "")

# 보유 하나를 손절 → 슬롯이 빈다 → 기다리던 종목이 들어온다
first = held[0]
b0 = bot.positions[first].entryPrice
run(bot, "09:07:00", {first: (int(b0 * 0.985), BASE[first] + 4 * TICK[first], BASE[first] - 2 * TICK[first], 99_999)})
check("손절하면 판다", first not in bot.positions and bot.total_trades == 1, f"{first}")
breakout(["035720", "000660", "105560", "005930"], ["09:07:20", "09:07:40", "09:08:00"])
check("슬롯이 비면 기다리던 신호를 산다", waiting[0] in bot.positions, f"{sorted(bot.positions)}")
check("판 종목은 같은 날 다시 사지 않는다", first not in bot.positions and bot.days[first].done, "")

# 산 종목의 스트림이 멈추면 REST 로 본다
some = sorted(bot.positions)[0]
fs.ages[some] = 30.0
rest_quotes[some] = {**fs.snap[some], "price": int(bot.positions[some].entryPrice * 1.03), "hogaTime": "09:09:00",
                     "etf": False, "upperLimit": 0, "lowerLimit": 0, "bid": int(bot.positions[some].entryPrice * 1.03) - 10}
now = at("09:09:00")
bot._tick(now, "2026-09-29", orb.minutes_since_open(now))
check("산 종목 스트림이 멈추면 REST 시세로 익절한다", some not in bot.positions, "")
fs.ages.clear(); rest_quotes.clear()

now = at("09:30:00")
for c in list(bot.positions):
    tick_snap(c, now, int(bot.positions[c].entryPrice), BASE[c] + 4 * TICK[c], BASE[c] - 2 * TICK[c], 999_999)
bot._tick(now, "2026-09-29", orb.minutes_since_open(now))
check("09:30 타임컷으로 남은 종목을 다 판다", not bot.positions, f"{bot.total_trades}건")
acts = [t["action"] for t in bot.trade_history]
check("일지에 종목별로 원화 · 국내로 남는다", all(t["currency"] == "KRW" and t["market"] == "KRX" for t in bot.trade_history)
      and acts.count("BUY") == acts.count("SELL"), f"매수 {acts.count('BUY')} · 매도 {acts.count('SELL')}")

print("── 비싼 종목 · RVOL ──")
pricey = osc.OrbScanner("ORB-p", "PAPER", 900_000, {**P, "watchlist": ["000660"], "maxPositions": 3}, None)
pricey.day_date = "2026-09-29"
BASE_SAVE = dict(BASE); BASE["000660"] = 1_779_000; TICK["000660"] = 1000
bot_save, bot = bot, pricey
for sec in range(5, 300, 10):
    run(bot, f"09:0{sec // 60}:{sec % 60:02d}", {"000660": (BASE["000660"], BASE["000660"] + 2000, BASE["000660"] - 2000, 10_000 + 100 * sec)})
breakout(["000660"], ["09:06:00", "09:06:20", "09:06:40"])
check("1주가 종목당 자본(30만원)보다 비싸면 건너뛴다 (SK하이닉스 177.9만원)",
      not bot.positions and bot.days["000660"].done and "살 수 없어" in bot.days["000660"].note, bot.days["000660"].note[:30])
BASE.update(BASE_SAVE); bot = bot_save

rv = osc.OrbScanner("ORB-r", "PAPER", 900_000, {"marketFilter": False, "watchlist": ["035720"], "watchMode": "manual", **OFF}, None)
rv.or_vol_history = {"035720": {"2026-09-28": 5_000, "2026-09-25": 100}}
check("RVOL 기준은 종목별 · 오늘 이전 가장 최근 거래일", rv._prev_or_vol("035720", "2026-09-29") == 5_000
      and rv._prev_or_vol("005930", "2026-09-29") is None, "")

print("── 저장 · 복원 ──")
b3 = osc.OrbScanner("ORB-s", "PAPER", 900_000, {**P, "watchlist": CODES, "maxPositions": 2, "exitMode": "trailing"}, None,
                    names={"035720": "카카오"})
b3.day_date = "2026-09-29"
b3.positions["035720"] = osc._Pos(8, 34_200, 273_641, "2026-09-29", peak=34_800)
b3.days["035720"] = orb.OrbDay("2026-09-29"); b3.days["035720"].entered = True
b3.or_vol_history = {"035720": {"2026-09-29": 12_345}}
r3 = osc.OrbScanner.restore(json.loads(json.dumps(b3.snapshot())), None)
check("포지션 · 고점(트레일링) 이 그대로", r3.positions["035720"].units == 8 and r3.positions["035720"].peak == 34_800, "")
check("오늘 진입 여부 · 설정 · 목록이 그대로",
      r3.days["035720"].entered and r3.max_positions == 2 and r3.orb.exitMode == "trailing" and r3.watch == CODES, "")
check("RVOL 기록도 그대로", r3.or_vol_history == {"035720": {"2026-09-29": 12_345}}, "")
check("BotManager 가 보는 합계 (pos.units · 상태)", r3.pos.units == 8 and r3.status()["strategyType"] == "orb"
      and r3.status()["orbScan"]["maxPositions"] == 2, "")

print("── 실전 경로 (krx 주문 함수를 바꿔 끼움) ──")
calls, state = [], {"qty": {}, "avg": {}, "fill": True}


class FakeAcc:
    configured = True
    account_no = "5000000000"


def f_balance(acc, fresh=False):
    return {"cash": 5_000_000, "holdings": {c: {"qty": q, "avg": state["avg"][c]} for c, q in state["qty"].items() if q}}


def f_order(acc, side, code, qty, limit):
    calls.append((side, code, qty, limit))
    if state["fill"]:
        q0 = state["qty"].get(code, 0)
        if side == "buy":
            state["avg"][code] = (q0 * state["avg"].get(code, 0) + qty * (limit - 10)) / (q0 + qty)
            state["qty"][code] = q0 + qty
        else:
            state["qty"][code] = q0 - qty
    return f"NO-{len(calls)}"


def f_await(acc, code, before, want, side, wait_sec=8.0):
    now_q = state["qty"].get(code, 0)
    filled = now_q - before if side == "buy" else before - now_q
    return {"filled": max(0, min(want, filled)), "qtyAfter": now_q, "avgAfter": state["avg"].get(code, 0), "known": True}


cancels = []
state["cancel_ok"] = True
def f_cancel(acc, order_no, code, qty=None):
    cancels.append((order_no, code, qty))
    if not state["cancel_ok"]:
        raise NamuhError("취소 거부 (테스트)")
    return "C-1"


orig = (krx.balance, krx.order, krx.await_fill, krx.cancel)
krx.balance, krx.order, krx.await_fill, krx.cancel = f_balance, f_order, f_await, f_cancel
try:
    live = osc.OrbScanner("ORB-L", "LIVE", 900_000, {**P, "watchlist": ["122630"], "maxPositions": 3}, FakeAcc())
    live.day_date = "2026-09-29"
    live.days["122630"] = orb.OrbDay("2026-09-29")
    fs.snap["122630"] = {"code": "122630", "price": 110_900, "ask": 110_905, "bid": 110_895, "high": 111_000,
                         "low": 110_000, "open": 110_000, "volume": 1, "vwap": 110_500, "hogaTime": "09:06:00"}
    live._buy("122630", "돌파")
    check("시장성 지정가 = 매도호가 +2호가 (ETF 5원)", calls[-1] == ("buy", "122630", 2, 110_915), f"{calls[-1]}")
    check("체결가는 잔고 평단으로 역산", live.positions["122630"].entryPrice == 110_905, "")
    live._sell("122630", "익절")
    check("매도 = 매수호가 -2호가 · 전량", calls[-1] == ("sell", "122630", 2, 110_885) and not live.positions, f"{calls[-1]}")

    state["fill"] = False
    stuck = osc.OrbScanner("ORB-S", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    stuck.day_date = "2026-09-29"
    stuck.days["122630"] = orb.OrbDay("2026-09-29")
    try:
        stuck._buy("122630", "돌파")
        raised = False
    except NamuhError as e:
        raised = "체결되지 않았습니다" in e.message
    check("미체결이면 잔량을 취소하고 장부를 바꾸지 않는다 (취소 성공 → 미체결로 남기지 않음)",
          raised and not stuck.positions and not stuck.pending_orders and cancels[-1] == (calls[-1] and "NO-%d" % len(calls), "122630", None), f"{cancels[-1:]}")
    state["cancel_ok"] = False
    stuck2 = osc.OrbScanner("ORB-S2", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    stuck2.day_date = "2026-09-29"; stuck2.days["122630"] = orb.OrbDay("2026-09-29")
    try:
        stuck2._buy("122630", "돌파")
    except NamuhError:
        pass
    check("취소도 실패하면 미체결 주문으로 남겨 지켜본다", len(stuck2.pending_orders) == 1 and not stuck2.positions, "")
    # 나중에 체결됐다 → 장부에 넣고 감시를 붙인다
    state["qty"]["122630"] = state["qty"].get("122630", 0) + 2
    state["avg"]["122630"] = 110_900.0
    stuck2._check_pending(10)
    check("미체결 매수가 뒤늦게 붙으면 장부에 넣는다 (주인 없는 주식이 생기지 않게)",
          stuck2.positions.get("122630") and stuck2.positions["122630"].units == 2, f"{stuck2.positions}")
    state["cancel_ok"] = True
    state.update(qty={}, avg={})

    # 체결 확인 중 마지막 잔고 조회 하나가 실패해도 앞서 본 체결을 버리지 않는다
    seq = iter([{"cash": 1, "holdings": {"069500": {"qty": 2, "avg": 100}}}, NamuhError("429")])
    def flaky_balance(acc, fresh=False):
        v = next(seq, NamuhError("429"))
        if isinstance(v, Exception):
            raise v
        return v
    krx.balance = flaky_balance
    import services.krx as _k
    _sleep = _k.time.sleep
    _k.time.sleep = lambda s_: None
    try:
        rr = orig[2](FakeAcc(), "069500", 0, 2, "buy", wait_sec=0.05)
    finally:
        _k.time.sleep = _sleep
        krx.balance = f_balance
    check("체결 확인: 마지막 조회가 실패해도 앞서 본 2주 체결을 돌려준다", rr["filled"] == 2 and rr["known"], f"{rr}")

    # 부분 체결 → 나머지만 취소
    state["partial"] = True
    def f_order_partial(acc, side, code, qty, limit):
        calls.append((side, code, qty, limit))
        if side == "buy":
            state["qty"][code] = state["qty"].get(code, 0) + 1
            state["avg"][code] = limit - 10
        return "NO-P"
    krx.order = f_order_partial
    pp = osc.OrbScanner("ORB-P", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    pp.day_date = "2026-09-29"; pp.days["122630"] = orb.OrbDay("2026-09-29")
    pp._buy("122630", "돌파")
    check("부분 체결이면 남은 수량만 취소하고 체결분만 장부에 넣는다",
          pp.positions["122630"].units == 1 and cancels[-1] == ("NO-P", "122630", 1), f"{cancels[-1]}")
    krx.order = f_order

    # 주문 응답을 못 받았다 → 들어갔는지 모른다 → 다시 사지 않는다
    def f_order_timeout(acc, side, code, qty, limit):
        raise krx.OrderUnknown("응답 없음 (테스트)")
    krx.order = f_order_timeout
    ou = osc.OrbScanner("ORB-U", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    ou.day_date = "2026-09-29"; ou.days["122630"] = orb.OrbDay("2026-09-29")
    try:
        ou._buy("122630", "돌파"); unk = False
    except krx.OrderUnknown:
        unk = True
    check("주문 응답을 못 받으면 미체결로 남기고 예외를 올린다 (호출부가 그날 다시 사지 않는다)",
          unk and ou.pending_orders and ou.pending_orders[0]["orderNo"] is None, "")
    krx.order = f_order

    # 계좌에 없는 보유 — 한 번 비어 온 것일 수 있다
    state.update(qty={}, avg={})
    gone = osc.OrbScanner("ORB-G", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    gone.day_date = "2026-09-29"
    gone.positions["122630"] = osc._Pos(3, 110_000, 330_000, "2026-09-29")
    try:
        gone._sell("122630", "타임컷"); first_raise = False
    except NamuhError:
        first_raise = True
    check("계좌에 안 보여도 한 번에 장부를 닫지 않는다 (잔고가 잠깐 비어 올 수 있다)",
          first_raise and "122630" in gone.positions, "")
    gone._missing["122630"] -= 30
    gone._sell("122630", "타임컷")
    check("25초 넘게 계속 없으면 장부를 닫아 슬롯을 푼다", "122630" not in gone.positions, "")
    state.update(fill=True, qty={"122630": 3}, avg={"122630": 110_000.0})
    rc = osc.OrbScanner("ORB-R", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    rc.positions["122630"] = osc._Pos(5, 110_000, 550_000, "2026-09-29")
    why = rc.reconcile()
    check("복원 대조: 장부가 계좌보다 많으면 재가동을 보류한다", why and "보류" in why, (why or "")[:40])
finally:
    krx.balance, krx.order, krx.await_fill, krx.cancel = orig
    krx.quote = orig_quote

print("── 실시간 스트림 수명 ──")
st2 = krx_stream.KrxStream()
check("연결 전에는 '조용함' 을 재지 않는다 (재접속을 걸지 않는다)", st2.quiet_for() is None, "")
st2.last_msg_at, st2.connected, st2.connected_at = 1.0, True, __import__("time").time()
check("연결 직후에는 어제 마지막 체결이 아니라 연결 시각부터 잰다", st2.quiet_for() < 5, f"{st2.quiet_for():.1f}")
st2._snap["005930"] = {"price": 1, "receivedAt": 1.0}
st2.release()
check("연결을 닫으면 어제 값(마지막 체결 시각 · 스냅샷)을 지운다",
      st2.last_msg_at == 0 and st2.connected_at == 0 and st2.get("005930") is None, "")

print("── 메인 루프 한 바퀴 (시계를 바꿔 끼움) ──")


class _OneShot:
    """_stop 대신 — 한 번 기다리면 루프를 끝낸다."""
    def __init__(self): self.waits, self._set = [], False
    def is_set(self): return self._set
    def set(self): self._set = True
    def clear(self): self._set = False
    def wait(self, t): self.waits.append(t); self._set = True


def one_loop(bot_, when):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    real_dt = osc.datetime
    osc.datetime = FakeDT
    bot_._stop = _OneShot()
    try:
        bot_._loop()
    finally:
        osc.datetime = real_dt
    return bot_._stop.waits


class LoopStream(FakeStream):
    def __init__(self):
        super().__init__(); self.released = 0; self.kicks = []; self.quiet = None; self.connected = True
    def release(self): self.released += 1; self.connected = False
    def kick(self, why): self.kicks.append(why)
    def quiet_for(self): return self.quiet


ls = LoopStream()
osc.stream = ls
rest = []
krx.quote = lambda acc, code: rest.append(code) or {"price": 1, "hogaTime": "00:00:00", "etf": False,
                                                     "bid": 1, "ask": 1, "upperLimit": 0, "lowerLimit": 0}
hb2 = osc.OrbScanner("ORB-N", "LIVE", 900_000, {**P, "watchlist": ["035720"]}, None)
hb2.positions["035720"] = osc._Pos(5, 34_000, 170_000, "2026-09-29")
sat = datetime(2026, 10, 3, 3, 0, 0, tzinfo=orb.KST)
w = one_loop(hb2, sat)
check("토요일 새벽에 들고 있어도 시세를 부르지 않고 연결을 닫고 길게 잔다",
      not rest and ls.released >= 1 and w and w[0] >= 60, f"REST {len(rest)} · 잠 {w}")
w = one_loop(hb2, at("20:00:00"))
check("평일 밤에도 마찬가지 (해외 봇과 같이 쓰는 호출 한도를 지킨다)", not rest and w and w[0] >= 60, f"{w}")
w = one_loop(hb2, at("08:30:00"))
check("개장 전(08:30)에도 들고 있는 종목 시세를 부르지 않는다", not rest, "")

late = osc.OrbScanner("ORB-L2", "PAPER", 900_000, {**P, "watchMode": "auto", "watchlist": ["035720", "005930"]}, None)
sel_calls = []
real_select = osc.orb_selector.select
osc.orb_selector.select = lambda *a, **k: sel_calls.append(1) or {}
try:
    one_loop(late, at("09:10:00"))
finally:
    osc.orb_selector.select = real_select
check("장중(09:10)에 늦게 떴으면 6분짜리 선정을 건너뛰고 직접 입력 목록으로 본다",
      not sel_calls and late.watch == ["035720", "005930"] and late.prepared_date == "2026-09-29", f"{late.watch}")

k = osc.OrbScanner("ORB-K", "PAPER", 900_000, {**P, "watchlist": ["035720"]}, None)
k.prepared_date = "2026-09-29"
ls.connected, ls.quiet, ls.kicks = True, 40.0, []
one_loop(k, at("09:00:10"))
check("09:00 직후(동시호가 체결 전)에는 조용해도 재접속하지 않는다", not ls.kicks, f"{ls.kicks}")
ls.connected = True
one_loop(k, at("09:02:00"))
check("장 초반에 연결된 뒤 20초 넘게 체결이 없으면 다시 붙는다", len(ls.kicks) == 1, f"{ls.kicks}")
ls.quiet = None; ls.kicks = []; ls.connected = True
one_loop(k, at("09:02:00"))
check("아직 연결 전이면(재접속 중) 끊지 않는다 — 끊으면 영영 못 붙는다", not ls.kicks, "")
krx.quote = orig_quote
osc.stream = fs

print("── 정지 규칙 (BotManager) ──")
from services import trader as _tr    # noqa: E402
class _B:
    coin = "ORB"; is_running = True
    def __init__(self, ok): self.ok = ok; self.stopped = False
    def can_liquidate(self): return (self.ok, "국내 정규장(09:00~15:30)이 아닙니다.")
    def stop(self, liquidate=True): self.stopped = True; return True
bm = _tr.BotManager()
bm.bots = {"a": _B(False), "b": _B(True)}
try:
    bm.stop("a"); refused = False
except _tr.LiquidationFailed:
    refused = True
check("팔 수 없을 때는 정지를 거부한다 (포지션이 감시 없이 남지 않게)", refused and not bm.bots["a"].stopped, "")
n, skipped = bm.stop_all()
check("전체 정지는 팔 수 있는 봇만 멈추고 나머지는 사유와 함께 알린다",
      n == 1 and bm.bots["b"].stopped and not bm.bots["a"].stopped and skipped, f"{skipped}")

print("── 자동 선정: 종목 마스터 (m_new_stock.mst 레이아웃) ──")
from services import krx_master, orb_selector   # noqa: E402


def mst_rec(code, name, market="1", k200="0", q150="N", prev=10000, cap=1000, under="N", stop="N",
            alert="0", short="0", sltr="N"):
    r = bytearray(b" " * 237)
    def put(off, n, val):
        v = str(val).encode("cp949")[:n]
        r[off:off + len(v)] = v
    put(0, 6, code); put(6, 1, market); put(7, 41, name); put(152, 7, f"{prev:07d}")
    put(160, 1, under); put(161, 1, stop); put(168, 1, k200); put(172, 1, q150)
    put(174, 12, f"{cap:012d}"); put(187, 1, short); put(188, 1, alert); put(189, 1, sltr)
    r[236] = 0x0A
    return bytes(r)


raw = b"".join([
    mst_rec("005930", "*삼성전자", k200="B", prev=272000, cap=16000000),
    mst_rec("035720", "*카카오", k200="5", prev=34000, cap=150000),
    mst_rec("0009K0", "#에임드바이오", market="4", q150="Y", prev=21400, cap=13778),
    mst_rec("036540", "#SFA반도체", market="4", q150="Y", alert="1"),          # 투자주의 — 남긴다
    mst_rec("031980", "#피에스케이홀딩스", market="4", q150="Y", alert="2"),   # 투자경고 — 뺀다
    mst_rec("111111", "*정지종목", k200="1", stop="Y"),
    mst_rec("222222", "일반종목"),                                              # 지수 편입 아님
])
rows = krx_master.parse(raw)
check("마스터 레코드 237바이트 · 이름 앞 표시(*·#)를 뗀다", rows[0]["name"] == "삼성전자" and rows[2]["name"] == "에임드바이오", "")
check("코스피200 · 코스닥150 · 시장 · 전일종가 · 시총", rows[0]["kospi200"] and rows[2]["kosdaq150"]
      and rows[2]["market"] == "kosdaq" and rows[1]["prevClose"] == 34000 and rows[0]["capEok"] == 16000000, "")
cands = krx_master.candidates(rows)
check("후보: 투자주의는 남기고 경고 · 정지 · 비편입은 뺀다",
      [c["code"] for c in cands] == ["005930", "035720", "0009K0", "036540"], f"{[c['code'] for c in cands]}")
try:
    krx_master.parse(raw[:-5]); bad_len = False
except ValueError:
    bad_len = True
check("크기가 레코드 배수가 아니면 형식이 바뀐 것으로 보고 멈춘다", bad_len, "")
check("영문이 섞인 새 코드도 국내 종목으로 본다 (0009K0)", krx.is_krx("0009K0") and not krx.is_krx("TQQQ"), "")

print("── 자동 선정: 거르기 · 순위 ──")
SP = orb_selector.SelectParams()
J = lambda **k: orb_selector.judge({"expPrice": 34_500, "expChangePct": 2.0, "expVolume": 300_000,
                                   "prevVolume": 600_000, **k}, 1_000_000, SP)
check("통과: 갭 +2% · 예상 거래대금 103억 · 장 전 RVOL 0.5배", J()["ok"] and J()["score"] == 0.5, f"{J()}")
check("예상체결이 없으면 뺀다", "예상체결 없음" in J(expVolume=0)["why"], "")
check("갭 +0.5% 는 하한(+2%) 밑", "갭" in J(expChangePct=0.5)["why"], "")
check("갭 +5.5% 는 상한(+5%) 위 — 09:05 실제 갭 조건과 같은 범위", "갭" in J(expChangePct=5.5)["why"], "")
check("갭이 아래로(-3%)면 뺀다 (매수만 한다)", not J(expChangePct=-3)["ok"], "")
check("예상 거래대금 0.3억은 하한(0.5억) 밑", "거래대금" in J(expVolume=1_000)["why"], J(expVolume=1_000)["why"])
check("08:5x 의 얇은 예상체결(2억)도 통과한다 — 9/29 첫 선정은 3억 하한에 189종목 중 1종목만 남았다",
      J(expVolume=6_000)["ok"], J(expVolume=6_000)["why"])
check("1주가 종목당 자본보다 비싸면 뺀다", "종목당" in J(expPrice=1_779_000)["why"], "")

fakeq = {"005930": {"expPrice": 280_000, "expChangePct": 2.9, "expVolume": 400_000, "prevVolume": 12_000_000},
         "035720": {"expPrice": 35_000, "expChangePct": 3.0, "expVolume": 900_000, "prevVolume": 1_000_000},
         "0009K0": {"expPrice": 22_500, "expChangePct": 4.6, "expVolume": 200_000, "prevVolume": 100_000},
         "036540": {"expPrice": 0, "expChangePct": 0, "expVolume": 0, "prevVolume": 50_000}}
calls_q = []
def fq(acc, code):
    calls_q.append(code)
    return fakeq[code]
import time as _t
res = orb_selector.select(None, cands, 1_000_000, SP, _t.time() + 60, lambda: False, quote=fq)
check("순위 = 예상체결량 ÷ 전일 거래량 (평소보다 몰린 종목이 위)",
      [c["code"] for c in res["chosen"]] == ["0009K0", "035720", "005930"], f"{[(c['code'], c['score']) for c in res['chosen']]}")
check("시가총액 큰 종목부터 훑는다 (시간이 모자라면 작은 종목을 남긴다)", calls_q[:2] == ["005930", "035720"], f"{calls_q}")
check("집계: 후보 · 조회 · 예상체결 받은 수 · 통과", (res["candidates"], res["scanned"], res["withExpected"], res["passed"]) == (4, 4, 3, 3), "")
res2 = orb_selector.select(None, cands, 1_000_000, orb_selector.SelectParams(topN=2), _t.time() + 60, lambda: False, quote=fq)
check("상위 topN 만", len(res2["chosen"]) == 2, "")
res3 = orb_selector.select(None, cands, 1_000_000, SP, _t.time() - 1, lambda: False, quote=fq)
check("마감 시각이 지나면 거기서 끝낸다 (08:55 실시간 연결을 지킨다)", res3["scanned"] == 0 and res3["truncated"], "")
res4 = orb_selector.select(None, cands, 30_000, SP, _t.time() + 60, lambda: False, quote=fq)
check("전일 종가로 살 수 없는 종목은 호출하지 않는다", res4["affordable"] == 2, f"{res4['affordable']}")
bad = 0
for b_ in ({"topN": 40}, {"gapMinPct": 5, "gapMaxPct": 1}, {"minExpTurnoverEok": -1}):
    try:
        orb_selector.SelectParams.from_dict(b_)
    except ValueError:
        bad += 1
check("말이 안 되는 선정 설정은 거부한다", bad == 3, f"{bad}/3")

print("── 자동 선정 → 스캐너 ──")
orig_load, orig_prev, orig_q2 = krx_master.load, krx.prev_or_volume, krx.quote
krx_master.load = lambda: rows
krx.quote = fq
prev_calls = []
def fprev(acc, code, today, rng):
    prev_calls.append(code)
    return {"date": "2026-09-23", "volume": 5_000}
krx.prev_or_volume = fprev
try:
    au = osc.OrbScanner("ORB-A", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    check("기본은 자동 선정 · 선정 전 목록은 비어 있다", au.watch_mode == "auto" and au.watch == [], "")
    au._prepare_day("2026-09-29", at("08:48:00"))
    check("선정한 종목을 오늘 감시한다", au.watch == ["0009K0", "035720", "005930"], f"{au.watch}")
    check("코스닥 종목은 코스닥 지수로 거른다", au.index_of.get("0009K0") == "kosdaq" and au._name("0009K0") == "에임드바이오", "")
    check("선정한 종목마다 전 거래일 09:05 거래량을 받는다 → 첫날부터 RVOL",
          sorted(prev_calls) == sorted(au.watch) and au._prev_or_vol("0009K0", "2026-09-29") == 5_000, "")
    au.prepared_date = "2026-09-29"
    ra = osc.OrbScanner.restore(json.loads(json.dumps(au.snapshot())), None)
    check("선정 뒤 재시작해도 오늘 목록을 그대로 쓴다 (6분 선정을 다시 하지 않음)",
          ra.watch == au.watch and ra.prepared_date == "2026-09-29" and ra.selection["passed"] == 3, "")
    for k in fakeq:
        fakeq[k] = {**fakeq[k], "expPrice": 0, "expVolume": 0}
    fb = osc.OrbScanner("ORB-F", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930", "035720"]}, None)
    fb._prepare_day("2026-09-29", at("08:48:00"))
    check("예상체결을 한 종목도 못 받으면 직접 입력 목록으로 본다", fb.watch == ["005930", "035720"], f"{fb.watch}")
finally:
    krx_master.load, krx.prev_or_volume, krx.quote = orig_load, orig_prev, orig_q2

print("── 08:40 넓게 훑기 → 08:58 다시 거르기 ──")
C2 = [{"code": c, "name": n, "market": m, "prevClose": 10_000, "capEok": cap}
      for c, n, m, cap in (("000001", "가", "kospi", 900), ("000002", "나", "kosdaq", 800), ("000003", "다", "kospi", 700),
                           ("000004", "라", "kospi", 600), ("000005", "마", "kospi", 500), ("000006", "바", "kosdaq", 400))]
def q2(gap, vol, prev=1_000_000, px=10_000):
    return {"expPrice": px if vol else 0, "expChangePct": gap, "expVolume": vol, "prevVolume": prev}
early = {"000001": q2(1.0, 3_000),        # 갭이 하한 밑 · 거래 얇음 → 지금 기준 탈락, 예비 목록에는 든다
         "000002": q2(8.0, 20_000),       # 갭 +8% → 상한 밖, 느슨한 범위(+9%) 안 → 예비
         "000003": q2(10.0, 50_000),      # 갭 +10% → 느슨한 범위 밖
         "000004": q2(-2.0, 40_000),      # 갭 -2% → 느슨한 하한(-1.5%) 밖
         "000005": q2(0, 0),              # 예상체결 없음
         "000006": q2(3.0, 10_000)}       # 지금 기준 통과 (갭 3% · 1억)
SP2 = orb_selector.SelectParams()
r40 = orb_selector.select(None, C2, 1_000_000, SP2, _t.time() + 60, lambda: False, quote=lambda a, c: early[c])
check("08:40 엄격한 기준 통과는 따로 낸다 (다시 거르기가 실패할 때 쓴다)", [c["code"] for c in r40["chosen"]] == ["000006"],
      f"{[c['code'] for c in r40['chosen']]}")
check("예비 목록: 갭은 ±3%p 넓게 · 거래대금은 보지 않고 · 점수 순",
      [c["code"] for c in r40["prelist"]] == ["000002", "000006", "000001"], f"{[c['code'] for c in r40['prelist']]}")
r40b = orb_selector.select(None, C2, 1_000_000, orb_selector.SelectParams(topN=1, preN=2), _t.time() + 60,
                           lambda: False, quote=lambda a, c: early[c])
check("예비 목록은 preN 개까지", len(r40b["prelist"]) == 2, f"{len(r40b['prelist'])}")
bad2 = 0
for b_ in ({"preN": 20}, {"preN": 61}, {"looseGapPct": 11}):
    try:
        orb_selector.SelectParams.from_dict(b_)
    except ValueError:
        bad2 += 1
check("예비 목록이 감시 수보다 작거나 60 을 넘으면 거부한다 (08:58 재조회 시간)", bad2 == 3, f"{bad2}/3")

late = {"000001": q2(3.2, 60_000),        # 08:58: 갭 3.2% · 6억 → 통과 (08:40 에는 떨어졌던 종목)
        "000002": q2(8.5, 80_000),        # 여전히 상한 밖
        "000006": q2(0.4, 30_000)}        # 08:58 에 갭이 꺼졌다 → 탈락
orig = (krx_master.load, krx_master.candidates, krx.prev_or_volume, krx.quote)
krx_master.load, krx_master.candidates = (lambda: C2), (lambda rows: rows)
prev2 = []
krx.prev_or_volume = lambda acc, code, today, rng: prev2.append(code) or {"date": "2026-09-30", "volume": 7_000}
try:
    krx.quote = lambda a, c: early[c]
    sc = osc.OrbScanner("ORB-R", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    sc._prepare_day("2026-10-02", at("08:40:00", "2026-10-02"))
    check("08:40 뒤: 감시는 엄격한 통과 · 예비 목록은 따로", sc.watch == ["000006"]
          and [c["code"] for c in sc.prelist] == ["000002", "000006", "000001"], f"{sc.watch} · {[c['code'] for c in sc.prelist]}")
    check("RVOL 기준은 예비 목록 전부를 받는다 (08:58 에 무엇이 뽑혀도 첫날부터 RVOL)",
          sorted(prev2) == ["000001", "000002", "000006"], f"{sorted(prev2)}")
    sc.prepared_date = "2026-10-02"
    sr = osc.OrbScanner.restore(json.loads(json.dumps(sc.snapshot())), None)
    check("08:40 ~ 08:58 사이에 재시작해도 예비 목록이 남는다", [c["code"] for c in sr.prelist] == ["000002", "000006", "000001"]
          and sr.refined_date == "", "")
    krx.quote = lambda a, c: late[c]
    sc._refine_day("2026-10-02", at("08:58:00", "2026-10-02"))
    check("08:58: 예비 목록만 다시 조회해 원래 기준으로 고른다 (08:40 탈락 종목이 들어오고 꺼진 종목은 빠진다)",
          sc.watch == ["000001"], f"{sc.watch}")
    check("선정 기록에 08:40 훑기 요약을 같이 남긴다", sc.selection.get("refined") and sc.selection["sweep"]["prelist"] == 3
          and sc.selection["sweep"]["scanned"] == 6, f"{sc.selection.get('sweep')}")
    sc.refined_date = "2026-10-02"
    sr2 = osc.OrbScanner.restore(json.loads(json.dumps(sc.snapshot())), None)
    check("다시 거른 뒤 재시작해도 다시 하지 않는다", sr2.refined_date == "2026-10-02" and sr2.watch == ["000001"], "")
    # 다시 거르기에서 예상체결을 못 받으면 08:40 결과
    sc2 = osc.OrbScanner("ORB-R2", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    krx.quote = lambda a, c: early[c]
    sc2._prepare_day("2026-10-02", at("08:40:00", "2026-10-02"))
    krx.quote = lambda a, c: q2(0, 0)
    sc2._refine_day("2026-10-02", at("08:58:00", "2026-10-02"))
    check("08:58 에 예상체결을 하나도 못 받으면 08:40 결과로 본다", sc2.watch == ["000006"], f"{sc2.watch}")
    sc3 = osc.OrbScanner("ORB-R3", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    krx.quote = lambda a, c: early[c]
    sc3._prepare_day("2026-10-02", at("08:40:00", "2026-10-02"))
    calls3 = []
    krx.quote = lambda a, c: calls3.append(c) or late[c]
    sc3._refine_day("2026-10-02", at("08:59:30", "2026-10-02"))
    check("08:59:20 이 지났으면 다시 조회하지 않는다 (09:00 전에 구독을 바꿔야 한다)",
          calls3 == [] and sc3.watch == ["000006"], f"{calls3} · {sc3.watch}")
    check("일정: 08:40 기준 미리 받기 · 08:50 넓게 훑기(예상체결이 나오는 시각) · 08:57:40 마감 · 08:57:50 ~ 08:59:20 다시 거르기",
          (osc.PREFETCH_MIN, osc.SELECT_MIN, round(osc.SWEEP_END_MIN * 60), round(osc.REFINE_MIN * 60),
           round(osc.REFINE_END_MIN * 60)) == (-20.0, -10.0, -140, -130, -40), "")
    # 예상체결을 하나도 못 받은 날 (10/02: 08:40 에 훑어 338종목 모두 비었다)
    r0 = orb_selector.select(None, C2, 1_000_000, SP2, _t.time() + 60, lambda: False, quote=lambda a, c: q2(0, 0))
    check("예상체결이 하나도 없으면 시가총액 상위로 예비 목록을 둔다 (다시 거르기에서 살아날 기회)",
          r0["prelistByCap"] and [c["code"] for c in r0["prelist"]][:3] == ["000001", "000002", "000003"], "")
    sc4 = osc.OrbScanner("ORB-R4", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    krx.quote = lambda a, c: q2(0, 0)
    sc4._prepare_day("2026-10-02", at("08:50:00", "2026-10-02"))
    check("그날 08:50 에는 직접 입력 목록으로 두고", sc4.watch == ["005930"] and len(sc4.prelist) == 6, f"{sc4.watch}")
    krx.quote = lambda a, c: late.get(c, q2(0, 0))
    sc4._refine_day("2026-10-02", at("08:57:50", "2026-10-02"))
    check("다시 거르기에서 예상체결이 나오면 그 결과로 감시한다", sc4.watch == ["000001"], f"{sc4.watch}")

    print("── 08:40 RVOL 기준 미리 받기 ──")
    prev3 = []
    krx.prev_or_volume = lambda acc, code, today, rng: prev3.append(code) or {"date": "2026-10-01", "volume": 9_000}
    pf = osc.OrbScanner("ORB-P", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    pf.or_vol_history = {"000001": {"2026-10-01": 1_234}, "000002": {"2026-09-25": 5_555}}
    pf._prefetch_baselines("2026-10-02", at("08:40:00", "2026-10-02"))
    check("살 수 있는 후보 전체를 받는다 · 직전 거래일 기록이 있는 종목은 건너뛴다",
          sorted(prev3) == ["000002", "000003", "000004", "000005", "000006"] and pf.or_vol_history["000001"] == {"2026-10-01": 1_234},
          f"{sorted(prev3)}")
    check("며칠 지난 기록(9/25)은 직전 거래일 값이 아니라 다시 받는다", pf._prev_or_vol("000002", "2026-10-02") == 9_000,
          f"{pf._prev_or_vol('000002', '2026-10-02')}")
    prev3.clear()
    pf2 = osc.OrbScanner("ORB-P2", "PAPER", 3_000_000, {"marketFilter": False, "watchlist": ["005930"]}, None)
    pf2._prefetch_baselines("2026-10-02", at("08:49:55", "2026-10-02"))
    check("08:49:50 이 지나면 받지 않는다 (08:50 넓게 훑기를 늦추지 않는다)", prev3 == [], f"{prev3}")
finally:
    krx_master.load, krx_master.candidates, krx.prev_or_volume, krx.quote = orig

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
