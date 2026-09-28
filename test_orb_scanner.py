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


P = {"rvolMin": 0, "marketFilter": False}
CODES = ["005930", "000660", "035720", "105560"]
bot = osc.OrbScanner("ORB-t", "PAPER", 900_000, {**P, "watchlist": CODES, "maxPositions": 3}, None)
bot.day_date = "2026-09-29"
check("종목당 자본 = 운용자본 ÷ 최대 보유", bot.slot_budget() == 300_000, f"{bot.slot_budget():,.0f}")
check("실시간 구독에 감시 목록이 들어간다", set(CODES) <= set(bot._codes_to_stream()), "")
bot2 = osc.OrbScanner("x", "PAPER", 900_000, {"watchlist": CODES}, None)
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

rv = osc.OrbScanner("ORB-r", "PAPER", 900_000, {"marketFilter": False, "watchlist": ["035720"]}, None)
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
    return {"filled": max(0, min(want, filled)), "qtyAfter": now_q, "avgAfter": state["avg"].get(code, 0)}


orig = (krx.balance, krx.order, krx.await_fill)
krx.balance, krx.order, krx.await_fill = f_balance, f_order, f_await
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
    check("미체결이면 장부를 바꾸지 않고 미체결 주문으로 남긴다", raised and not stuck.positions and stuck.pending_orders, "")
    state.update(fill=True, qty={"122630": 3}, avg={"122630": 110_000.0})
    rc = osc.OrbScanner("ORB-R", "LIVE", 900_000, {**P, "watchlist": ["122630"]}, FakeAcc())
    rc.positions["122630"] = osc._Pos(5, 110_000, 550_000, "2026-09-29")
    why = rc.reconcile()
    check("복원 대조: 장부가 계좌보다 많으면 재가동을 보류한다", why and "보류" in why, (why or "")[:40])
finally:
    krx.balance, krx.order, krx.await_fill = orig
    krx.quote = orig_quote

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
