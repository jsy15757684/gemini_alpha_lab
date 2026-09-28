#!/usr/bin/env python3
"""국내주식 ORB — 판단(services/orb.py) · 호가 단위(krx) · 봇 주문/장부(orb_bot).

시세는 가짜 스냅샷으로 만든다. 나무증권에 아무것도 보내지 않는다
(LIVE 경로도 krx 함수를 바꿔 끼워 확인한다).
"""

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="orb-test-")
from services import botstore, tradelog                 # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

from services import krx, orb                           # noqa: E402
from services import orb_bot as ob                       # noqa: E402
from services.namuh import NamuhError                   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def at(hms, date="2026-09-29"):          # 화요일
    h, m, s = (int(x) for x in hms.split(":"))
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, h, m, s, tzinfo=orb.KST)


def snap(now, price, high=None, low=None, vol=0, vwap=None, hoga=None, bid=None, ask=None):
    return {"price": price, "high": high or price, "low": low or price, "volume": vol,
            "vwap": vwap if vwap is not None else price, "hogaTime": hoga or now.strftime("%H:%M:%S"),
            "bid": bid or price - 5, "ask": ask or price + 5, "upperLimit": 0, "lowerLimit": 0}


# 기본 ORB 동작은 세 필터를 끈 설정으로 본다. 필터는 아래에서 따로 본다.
P = orb.OrbParams(rvolMin=0, marketFilter=False)


def run_range(day, base=10000, vol0=100_000, rate=100):
    """09:00:05 ~ 09:04:55 를 10초마다. 고가 10,100 · 저가 9,950 · 거래 속도 rate 주/초."""
    for i, sec in enumerate(range(5, 300, 10)):
        now = at(f"09:0{sec // 60}:{sec % 60:02d}")
        px = base + (100 if sec == 125 else (-50 if sec == 205 else 0))
        orb.decide(P, day, snap(now, px, high=max(base, 10100 if sec >= 125 else base),
                                low=min(base, 9950 if sec >= 205 else base), vol=vol0 + rate * sec), now, None)


print("── 호가 단위 (KRX 2023 개편) ──")
check("ETF 11만원대는 5원", krx.tick_size(110_895, True) == 5, "")
check("ETF 2,000원 미만은 1원", krx.tick_size(1_995, True) == 1, "")
check("주식 27.5만원은 500원", krx.tick_size(275_000, False) == 500, "")
check("주식 1만원대는 10원", krx.tick_size(15_000, False) == 10, "")
check("매수는 호가 올림 · 매도는 내림",
      krx.round_tick(110_891, True, True) == 110_895 and krx.round_tick(110_894, True, False) == 110_890, "")
check("2호가 위 (ETF)", krx.shift_ticks(110_905, True, 2) == 110_915, f"{krx.shift_ticks(110_905, True, 2)}")
check("호가 단위 경계를 넘으면 단위가 바뀐다 (주식 19,990 → +2 = 20,050)",
      krx.shift_ticks(19_990, False, 2) == 20_050, f"{krx.shift_ticks(19_990, False, 2)}")
check("2호가 아래 경계 (주식 20,000 → -2 = 19,980)",
      krx.shift_ticks(20_000, False, -2) == 19_980, f"{krx.shift_ticks(20_000, False, -2)}")

print("── OR 측정 · 돌파 ──")
day = orb.OrbDay("2026-09-29")
now = at("08:59:30")
check("개장 전에는 아무것도 안 한다", orb.decide(P, day, snap(now, 10000, hoga="08:59:30"), now, None)["phase"] == "pre", "")
run_range(day)
check("OR 고가 · 저가를 당일 고저로 잰다", (day.orHigh, day.orLow) == (10100, 9950), f"{day.orHigh} · {day.orLow}")
check("OR 거래 속도 (시가 동시호가량 제외)", round(day.or_rate) == 100, f"{day.or_rate:.1f}주/초")

d1 = day
now = at("09:06:00")
r = orb.decide(P, d1, snap(now, 10090, vol=100_000 + 100 * 360), now, None)
check("OR 고가 아래면 대기", r["action"] is None and "돌파 대기" in r["reason"], r["reason"][:40])

# 거래가 붙지 않은 돌파
d2 = orb.OrbDay("2026-09-29"); run_range(d2)
v = 100_000 + 100 * 300
for sec in (360, 380, 400):
    now = at(f"09:0{sec // 60}:{sec % 60:02d}")
    r = orb.decide(P, d2, snap(now, 10150, vol=v + 100 * (sec - 300), vwap=10050), now, None)
check("거래량이 안 붙은 돌파는 사지 않는다", r["action"] is None and "거래속도" in r["reason"], r["reason"][-40:])

# 거래가 붙은 돌파
d3 = orb.OrbDay("2026-09-29"); run_range(d3)
for sec in (360, 380, 400):
    now = at(f"09:0{sec // 60}:{sec % 60:02d}")
    r = orb.decide(P, d3, snap(now, 10150, vol=v + 300 * (sec - 360), vwap=10050), now, None)
    if r["action"]:
        break
check("OR 고가 돌파 + VWAP 위 + 거래 3배 → 매수", r["action"] == "buy", r["reason"][:60])
now = at("09:07:00")
r = orb.decide(P, d3, snap(now, 10200, vol=v + 99_999, vwap=10050), now, None)
check("하루에 한 번만 들어간다", r["action"] is None and d3.done, r["reason"])

# VWAP 아래 돌파
d4 = orb.OrbDay("2026-09-29"); run_range(d4)
for sec in (360, 380, 400):
    now = at(f"09:0{sec // 60}:{sec % 60:02d}")
    r = orb.decide(P, d4, snap(now, 10150, vol=v + 300 * (sec - 360), vwap=10200), now, None)
check("VWAP 아래 돌파는 사지 않는다", r["action"] is None and "VWAP 10,200 아래" in r["reason"], "")
p_novwap = orb.OrbParams(useVwap=False, rvolMin=0, marketFilter=False)
d4b = orb.OrbDay("2026-09-29"); run_range(d4b)
for sec in (360, 380, 400):
    now = at(f"09:0{sec // 60}:{sec % 60:02d}")
    r = orb.decide(p_novwap, d4b, snap(now, 10150, vol=v + 300 * (sec - 360), vwap=10200), now, None)
    if r["action"]:
        break
check("VWAP 필터를 끄면 산다", r["action"] == "buy", "")

d5 = orb.OrbDay("2026-09-29"); run_range(d5)
now = at("09:25:00")
r = orb.decide(P, d5, snap(now, 10000, vol=v), now, None)
check("09:25 까지 돌파가 없으면 그날은 쉰다", r["action"] is None and d5.done and "09:25" in r["reason"], r["reason"])

print("── 청산 ──")
H = {"entryPrice": 10150, "date": "2026-09-29"}
dd = orb.OrbDay("2026-09-29"); dd.orLow = 9950
def ex(hms, px, day_=dd, h=H, hoga=None):
    now = at(hms)
    return orb.decide(P, day_, snap(now, px, hoga=hoga), now, h)
check("익절 +2%", ex("09:10:00", 10353)["action"] == "sell" and "익절" in ex("09:10:00", 10353)["reason"], "")
check("+1.9% 는 들고 간다", ex("09:10:00", 10343)["action"] is None, "")
check("손절 -1%", "손절" in ex("09:10:00", 10048)["reason"], ex("09:10:00", 10048)["reason"])
check("OR 저가를 모르면(재시작 등) 그 조건만 건너뛴다",
      ex("09:10:00", 10100, day_=orb.OrbDay("x"))["action"] is None, "")
d_or = orb.OrbDay("2026-09-29"); d_or.orLow = 10100
check("OR 저가 이탈이면 판다", "OR 저가" in ex("09:10:00", 10090, day_=d_or)["reason"], ex("09:10:00", 10090, day_=d_or)["reason"])
check("09:30 타임컷", "타임컷" in ex("09:30:00", 10200)["reason"] and "09:30" in ex("09:30:00", 10200)["reason"], "")
check("전날 포지션은 오늘 장 시작에 판다", "전날" in ex("09:00:10", 10200, h={"entryPrice": 10150, "date": "2026-09-28"})["reason"], "")
check("호가가 멈춰 있으면 (휴장) 청산 주문을 내지 않는다",
      ex("09:00:10", 10200, h={"entryPrice": 10150, "date": "2026-09-28"}, hoga="15:30:00")["action"] is None, "")

print("── 휴장 · 호출 절약 ──")
dh = orb.OrbDay("2026-09-29")
for sec in range(5, 300, 10):
    now = at(f"09:0{sec // 60}:{sec % 60:02d}")
    orb.decide(P, dh, snap(now, 10000, hoga="15:30:00"), now, None)
now = at("09:05:10")
r = orb.decide(P, dh, snap(now, 10200, hoga="15:30:00"), now, None)
check("호가가 멈춘 날(휴장·지연 개장)은 OR 을 못 재고 쉰다", dh.done and r["action"] is None, r["reason"][:30])
check("그날은 내일 아침까지 잔다 (2초 폴링을 안 한다)", orb.next_wake(P, now, False, dh.done) >= 60, "")
check("장 초반에는 2초마다 본다", orb.next_wake(P, at("09:03:00"), False, False) == 2.0, "")
sat = datetime(2026, 10, 3, 9, 3, 0, tzinfo=orb.KST)
check("주말에는 촘촘히 보지 않는다", orb.next_wake(P, sat, False, False) >= 60, f"{orb.next_wake(P, sat, False, False):.0f}초")
check("새벽에는 10분 단위로 잔다", orb.next_wake(P, at("03:00:00"), False, False) == 600, "")

print("── 필터 1: RVOL (전일 같은 시각 거래량) ──")
PR = orb.OrbParams(marketFilter=False)            # RVOL 200% 기본값
IDX_UP = {"name": "코스피", "price": 110_000, "open": 109_000}


def breakout(params, prev=None, index=None):
    dday = orb.OrbDay("2026-09-29"); run_range(dday)
    rr = None
    for sec in (360, 380, 400):
        now_ = at(f"09:0{sec // 60}:{sec % 60:02d}")
        rr = orb.decide(params, dday, snap(now_, 10150, vol=v + 300 * (sec - 360), vwap=10050), now_, None,
                        index=index, prev_or_vol=prev)
        if rr["action"] or dday.done:
            break
    return dday, rr


end_vol = 100_000 + 100 * 295                    # OR 마지막 스냅샷의 누적 거래량
dd1, r = breakout(PR, prev=None)
check("전일 기록이 없으면 첫날은 사지 않고 기록만 한다", dd1.done and r["action"] is None and "기록만" in r["reason"], "")
dd2, r = breakout(PR, prev=int(end_vol / 1.5))
check("RVOL 150% < 200% 면 쉰다", dd2.done and r["action"] is None and "RVOL 150%" in r["reason"], r["reason"][:40])
dd3, r = breakout(PR, prev=int(end_vol / 2.5))
check("RVOL 250% 면 돌파를 산다", r["action"] == "buy" and "RVOL 250%" in r["reason"], r["reason"][-30:])
check("RVOL 은 OR 이 끝난 뒤 한 번만 본다", dd3.rvolChecked and abs(dd3.rvol - 2.5) < 0.01, f"{dd3.rvol:.2f}")

print("── 필터 3: 지수 (당일 시가 위) ──")
PM = orb.OrbParams(rvolMin=0)
_, r = breakout(PM, index={"name": "코스피", "price": 108_000, "open": 109_000})
check("지수 ETF 가 시가 아래면 돌파여도 사지 않는다", r["action"] is None and "시가 아래" in r["reason"], r["reason"][-30:])
_, r = breakout(PM, index=None)
check("지수 시세가 없으면 사지 않는다 (모르면 쉰다)", r["action"] is None and "지수 시세 없음" in r["reason"], "")
_, r = breakout(PM, index=IDX_UP)
check("지수 ETF 가 시가 위면 산다", r["action"] == "buy" and "시가 위" in r["reason"], "")
_, r = breakout(orb.OrbParams(), prev=int(end_vol / 3), index=IDX_UP)
check("세 필터를 다 켜고 다 통과하면 산다 (기본 설정)", r["action"] == "buy", r["reason"][:50])

print("── 필터 2: 트레일링 스탑 ──")
PT = orb.OrbParams(rvolMin=0, marketFilter=False, exitMode="trailing", trailPct=1.0)
dt_ = orb.OrbDay("2026-09-29"); dt_.orLow = 9950
def tr(hms, px, peak):
    now_ = at(hms)
    return orb.decide(PT, dt_, snap(now_, px), now_, {"entryPrice": 10150, "date": "2026-09-29", "peak": peak})
check("고점 10,400 에서 -1% (10,296) 아래로 오면 판다", "트레일링" in (tr("10:30:00", 10290, 10400)["reason"]), "")
check("10,300 이면 들고 간다", tr("10:30:00", 10300, 10400)["action"] is None, tr("10:30:00", 10300, 10400)["reason"])
check("트레일링에서는 09:30 타임컷을 하지 않는다", tr("09:30:00", 10300, 10300)["action"] is None, "")
check("트레일링에서는 고정 익절(+2%)로 끊지 않는다", tr("09:40:00", 10500, 10500)["action"] is None, "")
check("15:15 최종 청산 (밤을 넘기지 않는다)", "15:15" in (tr("15:15:00", 10500, 10600)["reason"]), "")
check("고점이 진입가 이하일 때는 손절(-1%)이 지킨다",
      tr("09:20:00", 10100, 10150)["action"] is None and "손절" in tr("09:20:00", 10048, 10150)["reason"], "")
check("트레일링 보유 중에는 오후에 5초마다 본다 (호출 한도)", orb.next_wake(PT, at("11:00:00"), True) == 5.0, "")
bad_final = 0
try:
    orb.OrbParams.from_dict({"exitMode": "trailing", "finalCutMin": 385})
except ValueError:
    bad_final = 1
check("최종 청산을 15:20 뒤로 두면 거부한다 (종가 동시호가)", bad_final == 1, "")

print("── 설정 검증 ──")
bad = [{"rangeMin": 10, "entryEndMin": 5}, {"takeProfitPct": 0}, {"volSurge": 50}]
errs = 0
for b in bad:
    try:
        orb.OrbParams.from_dict(b)
    except ValueError:
        errs += 1
check("말이 안 되는 설정은 거부한다", errs == 3, f"{errs}/3")

print("── 봇 장부 (모의) ──")
bot = ob.OrbBot("ORB-t1", "122630", "PAPER", 1_000_000, {}, None)
bot.day = orb.OrbDay("2026-09-29")
bot._buy({"price": 110_900, "ask": 110_905, "bid": 110_895, "upperLimit": 0}, "테스트 돌파")
fee = krx.FEE_PCT / 100
check("자본으로 살 수 있는 만큼 정수로 산다", bot.pos.units == int(1_000_000 // (110_915 * (1 + fee))),
      f"{bot.pos.units}주 @ {bot.pos.entryPrice:,.0f}")
cash_after_buy = bot.cash
bot._sell({"price": 113_200, "bid": 113_195, "ask": 113_205, "lowerLimit": 0}, "익절")
check("매도하면 포지션이 비고 현금이 돌아온다", not bot.pos.open and bot.cash > cash_after_buy, f"현금 {bot.cash:,.0f}")
check("ETF 는 거래세 없이 수수료만 뺀다",
      abs(bot.realized_pnl - (9 * 113_195 * (1 - fee) - 9 * 110_905 * (1 + fee))) < 1,
      f"실현 {bot.realized_pnl:+,.0f}원")
acts = [t["action"] for t in reversed(bot.trade_history)]
check("체결 일지에 BUY · SELL 이 원화·국내로 남는다",
      acts == ["BUY", "SELL"] and bot.trade_history[0]["currency"] == "KRW" and bot.trade_history[0]["market"] == "KRX", "")
check("나무증권 워커 일지로 분류된다", tradelog.is_namuh_row(bot.trade_history[0]), "")

bot2 = ob.OrbBot("ORB-t2", "005930", "PAPER", 1_000_000, {"takeProfitPct": 1.5}, None)
bot2.day = orb.OrbDay("2026-09-29")
bot2._buy({"price": 275_000, "ask": 275_500, "bid": 274_500, "upperLimit": 0}, "t")
bot2.day.entered = True
snap_ = bot2.snapshot()
r2 = ob.OrbBot.restore(snap_, None)
check("저장 → 복원하면 포지션·설정·오늘 진입 여부가 그대로다",
      (r2.pos.units, r2.pos.entryPrice, r2.orb.takeProfitPct, r2.day.entered) == (bot2.pos.units, 275_500, 1.5, True),
      f"{r2.pos.units}주 @ {r2.pos.entryPrice:,.0f}")
check("상태는 원화로 보인다", r2.status()["currency"] == "KRW" and r2.status()["strategyType"] == "orb", "")
tiny = ob.OrbBot("ORB-t3", "005930", "PAPER", 200_000, {}, None)
tiny.day = orb.OrbDay("2026-09-29")
tiny._buy({"price": 275_000, "ask": 275_500, "bid": 274_500, "upperLimit": 0}, "t")
check("1주 값이 안 되는 자본이면 사지 않고 그날을 끝낸다 (20만원 · 27.5만원)", not tiny.pos.open and tiny.day.done, "")

hb = ob.OrbBot("ORB-h", "122630", "PAPER", 1_000_000, {}, None)
hb.or_vol_history = {"2026-09-23": 900, "2026-09-28": 1200, "2026-09-29": 5000}
check("RVOL 기준은 오늘 이전의 가장 최근 거래일 기록", hb._prev_or_vol("2026-09-29") == 1200, "")
rb = ob.OrbBot.restore(hb.snapshot(), None)
check("거래량 기록도 저장·복원된다", rb.or_vol_history == hb.or_vol_history, "")
check("종목에 맞는 지수를 고른다 (코스닥150 레버리지 → 코스닥)",
      ob.OrbBot("x", "233740", "PAPER", 1e6, {}, None)._index_key() == "kosdaq"
      and ob.OrbBot("x", "005930", "PAPER", 1e6, {}, None)._index_key() == "kospi", "")

print("── 봇 주문 (실전 경로 · krx 함수를 바꿔 끼움) ──")
calls = []
state = {"qty": 0, "avg": 0.0, "fill": True}


class FakeAcc:
    configured = True
    account_no = "00000000000"


def f_balance(acc, fresh=False):
    return {"cash": 5_000_000, "holdings": {"122630": {"qty": state["qty"], "avg": state["avg"]}} if state["qty"] else {}}


def f_order(acc, side, code, qty, limit):
    calls.append((side, qty, limit))
    if state["fill"]:
        if side == "buy":
            state["avg"] = (state["qty"] * state["avg"] + qty * (limit - 10)) / (state["qty"] + qty)
            state["qty"] += qty
        else:
            state["qty"] -= qty
    return f"NO-{len(calls)}"


def f_await(acc, code, before, want, side, wait_sec=8.0):
    now = state["qty"]
    filled = now - before if side == "buy" else before - now
    return {"filled": max(0, min(want, filled)), "qtyAfter": now, "avgAfter": state["avg"]}


orig = (krx.balance, krx.order, krx.await_fill)
krx.balance, krx.order, krx.await_fill = f_balance, f_order, f_await
try:
    live = ob.OrbBot("ORB-L1", "122630", "LIVE", 1_000_000, {}, FakeAcc())
    live.day = orb.OrbDay("2026-09-29")
    live._buy({"price": 110_900, "ask": 110_905, "bid": 110_895, "upperLimit": 130_000}, "돌파")
    check("시장성 지정가: 매도호가 + 2호가", calls[-1] == ("buy", live.pos.units, 110_915), f"{calls[-1]}")
    check("체결가는 잔고 평단으로 역산한다 (지정가보다 10원 싸게 붙음)", live.pos.entryPrice == 110_905,
          f"{live.pos.entryPrice:,.0f}")
    live._sell({"price": 112_000, "bid": 111_995, "ask": 112_005, "lowerLimit": 90_000}, "익절")
    check("매도: 매수호가 - 2호가 · 전량", calls[-1] == ("sell", 9, 111_985) and not live.pos.open, f"{calls[-1]}")

    state.update(qty=0, avg=0.0, fill=False)
    stuck = ob.OrbBot("ORB-L2", "122630", "LIVE", 1_000_000, {}, FakeAcc())
    stuck.day = orb.OrbDay("2026-09-29")
    try:
        stuck._buy({"price": 110_900, "ask": 110_905, "bid": 110_895, "upperLimit": 0}, "돌파")
        raised = False
    except NamuhError as e:
        raised = "체결되지 않았습니다" in e.message
    check("미체결이면 장부를 바꾸지 않고 미체결 주문으로 남긴다",
          raised and not stuck.pos.open and len(stuck.pending_orders) == 1, "")

    state.update(qty=3, avg=110_000.0, fill=True)
    partial = ob.OrbBot("ORB-L3", "122630", "LIVE", 1_000_000, {}, FakeAcc())
    partial.day = orb.OrbDay("2026-09-29")
    partial.pos = ob._Pos(5, 110_000, 550_000, "2026-09-29")
    partial._sell({"price": 111_000, "bid": 110_995, "ask": 111_005, "lowerLimit": 0}, "타임컷")
    check("계좌에 장부보다 적게 있으면 있는 만큼만 판다", calls[-1][:2] == ("sell", 3) and partial.pos.units == 2,
          f"{calls[-1]} · 남음 {partial.pos.units}주")
finally:
    krx.balance, krx.order, krx.await_fill = orig

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
