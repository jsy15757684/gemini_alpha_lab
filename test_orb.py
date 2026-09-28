#!/usr/bin/env python3
"""국내주식 ORB — 판단(services/orb.py) · 호가 단위 · 국내 잔고 해석(krx).

봇(스캐너)의 주문·장부는 test_orb_scanner.py 가 본다.

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


# 기본 ORB 동작은 필터를 끈 설정으로 본다. 필터 · 갭 앤 고 조건은 아래에서 따로 본다.
OFF = dict(gapMinPct=0, gapMaxPct=0, openHold=False)
P = orb.OrbParams(rvolMin=0, marketFilter=False, **OFF)


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
p_novwap = orb.OrbParams(useVwap=False, rvolMin=0, marketFilter=False, **OFF)
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
PR = orb.OrbParams(marketFilter=False, **OFF)     # RVOL 200% 기본값
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
PM = orb.OrbParams(rvolMin=0, **OFF)
_, r = breakout(PM, index={"name": "코스피", "price": 108_000, "open": 109_000})
check("지수 ETF 가 시가 아래면 돌파여도 사지 않는다", r["action"] is None and "시가 아래" in r["reason"], r["reason"][-30:])
_, r = breakout(PM, index=None)
check("지수 시세가 없으면 사지 않는다 (모르면 쉰다)", r["action"] is None and "지수 시세 없음" in r["reason"], "")
_, r = breakout(PM, index=IDX_UP)
check("지수 ETF 가 시가 위면 산다", r["action"] == "buy" and "시가 위" in r["reason"], "")
_, r = breakout(orb.OrbParams(**OFF), prev=int(end_vol / 3), index=IDX_UP)
check("세 필터를 다 켜고 다 통과하면 산다 (기본 설정)", r["action"] == "buy", r["reason"][:50])

print("── 갭 앤 고 조건: 시초가 갭 · 시초가 지지 (기본 켜짐) ──")
PG = orb.OrbParams(rvolMin=0, marketFilter=False)          # 갭 2~5% · 시초가 지지 기본값
check("기본값: 실제 시초가 갭 2~5% · 시초가 지지 켜짐", PG.gap_on and PG.openHold
      and (PG.gapMinPct, PG.gapMaxPct) == (2.0, 5.0), "")


def gng(params, open_=10000, prev=9709, dip=False, wick=False, after=10150, stream_tick=False, no_open=False):
    """시가 open_ · 전일 종가 prev.
    dip   09:03 한 분 동안 가격이 시가 아래(9,950)에 머문다 → 그 분 1분 종가가 시가 아래
    wick  저가만 시가 아래(9,950)를 찍고 가격은 곧 돌아온다 → 1분 종가는 시가 위"""
    dday = orb.OrbDay("2026-09-29")
    for sec in range(5, 300, 10):
        now_ = orb_now = at(f"09:0{sec // 60}:{sec % 60:02d}")
        px = 9950 if (dip and 180 <= sec < 240) else open_ + (100 if sec >= 125 else 0)
        q = snap(now_, px, high=open_ + 100 if sec >= 125 else open_,
                 low=9950 if ((dip or wick) and sec >= 180) else open_, vol=100_000 + 100 * sec)
        q["open"] = 0 if no_open else open_
        orb.decide(params, dday, q, orb_now, None)
    rr = None
    for sec in (360, 380, 400):
        now_ = at(f"09:0{sec // 60}:{sec % 60:02d}")
        q = snap(now_, after, high=max(after, open_ + 100), low=9950 if (dip or wick) else open_,
                 vol=129_500 + 300 * (sec - 360), vwap=10050)
        q["open"] = 0 if no_open else open_
        if stream_tick:
            q["changePct"] = round((after / prev - 1) * 100, 2)      # 실시간 체결에는 전일 종가가 없다
        else:
            q["prevClose"] = prev
        rr = orb.decide(params, dday, q, now_, None)
        if rr["action"] or dday.done:
            break
    return dday, rr


dg, r = gng(PG)
check("갭 +3% · 첫 5분 시가 지지 · 5분 양봉 → 돌파를 산다", r["action"] == "buy", r["reason"][:40])
check("OR 구간 1분 종가 최저를 잰다 (시가 이상)", dg.orMinClose == 10000, f"{dg.orMinClose}")
dg, r = gng(PG, wick=True)
check("저가 꼬리만 시가 아래를 찍고 1분 종가가 버티면 산다 (첫 1분 흔들림)", r["action"] == "buy", r["reason"][:40])
dg, r = gng(PG, prev=9901)
check("갭 +1% 는 범위 밖이라 쉰다", dg.done and r["action"] is None and "시초가 갭 +1.00%" in r["reason"], r["reason"][:40])
dg, r = gng(PG, prev=9434)
check("갭 +6% 도 범위 밖이라 쉰다 (상한)", dg.done and "시초가 갭 +6.00%" in r["reason"], r["reason"][:40])
dg, r = gng(PG, dip=True)
check("첫 5분 중 한 분이라도 1분 종가가 시가 아래면 쉰다",
      dg.done and "시초가 지지 실패" in r["reason"] and "9,950" in r["reason"], r["reason"][:60])
dg, r = gng(PG, after=10000)
check("09:05 가격이 시가 이하(음봉)면 쉰다", dg.done and "음봉" in r["reason"], r["reason"][:50])
dg, r = gng(PG, stream_tick=True)
check("실시간 체결(전일 종가 없음)은 등락률로 전일 종가를 되돌려 갭을 본다", r["action"] == "buy", r["reason"][:40])
dg, r = gng(PG, no_open=True)
check("시가를 모르면 사지 않는다", dg.done and r["action"] is None and "시가" in r["reason"], r["reason"][:40])
dg, r = gng(orb.OrbParams(rvolMin=0, marketFilter=False, gapMinPct=0, gapMaxPct=0), prev=9901)
check("갭 조건을 끄면(0 · 0) 갭 +1% 도 본다 — 시초가 지지는 그대로", r["action"] == "buy", r["reason"][:40])
dg, r = gng(orb.OrbParams(rvolMin=0, marketFilter=False, openHold=False), dip=True)
check("시초가 지지를 끄면 시가 아래로 밀렸던 종목도 산다", r["action"] == "buy", r["reason"][:40])
bad = 0
for b in ({"gapMinPct": 5, "gapMaxPct": 2}, {"gapMinPct": -40, "gapMaxPct": 5}):
    try:
        orb.OrbParams.from_dict(b)
    except ValueError:
        bad += 1
check("갭 범위가 거꾸로이거나 ±30% 밖이면 거부한다", bad == 2, f"{bad}/2")
check("예전에 만든 봇(설정에 새 칸 없음)을 되살리면 새 조건이 켜진다",
      orb.OrbParams.from_dict({"rvolMin": 2.0}).openHold is True, "")

print("── 필터 2: 트레일링 스탑 ──")
PT = orb.OrbParams(rvolMin=0, marketFilter=False, exitMode="trailing", trailPct=1.0, **OFF)
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

print("── 국내 잔고 해석 (모의계좌 실측 응답 · 2026-09-28) ──")


class _Res:
    status_code = 200


def fake_post(rows, o0):
    return lambda acc, base, path, inp, read: (_Res(), {"Output_0": o0, "Output_1": rows})


class _Acc:
    configured = True
    account_no = "5000000000"


O0 = {"dca": 500000000, "orr_pbl_amt1": 499013753}
orig_post = krx._post
try:
    krx._post = fake_post([{"iem_cd": "069500", "iem_nm": "KODEX 200", "itg_bnc_qty": 0.0, "rsdl_qty": 1.0,
                            "ny_stl_qty": 1.0, "phs_pr": 109917, "now_pr": 109915}], O0)
    bb = krx.balance(_Acc(), fresh=True)
    check("산 직후: 보유 1주 · 평단 109,917 (결제 잔고 0 이어도)",
          bb["holdings"]["069500"]["qty"] == 1 and bb["holdings"]["069500"]["avg"] == 109917, "")
    check("주문가능 현금은 예수금이 아니라 주문가능금액", bb["cash"] == 499013753 and bb["deposit"] == 500000000, "")
    krx._post = fake_post([{"iem_nm": "KODEX 200", "iem_cd": "069500", "ny_stl_qty": 1.0, "rsdl_qty": 0.0,
                            "phs_pr": 109917, "now_pr": 109910, "sll_amt": 109900, "sll_pls_amt": -17},
                           {"ny_stl_qty": -1.0}], O0)
    bb = krx.balance(_Acc(), fresh=True)
    check("판 직후: 미결제 1주가 남아도 보유 0 으로 본다 (broker.py 의 '최댓값' 규칙이 틀렸던 곳)",
          "069500" not in bb["holdings"], f"{bb['holdings']}")
    check("종목코드 없는 매도 미결제 행은 무시한다", len(bb["holdings"]) == 0, "")
finally:
    krx._post = orig_post

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
