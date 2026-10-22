#!/usr/bin/env python3
"""국내 장초반 스캘퍼(모의) — 시계 · 시세 · 선정기를 가짜로 두고 하루를 돌린다. 주문은 원래 없다."""

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_SANDBOX = tempfile.mkdtemp(prefix="scalper-test-")
from services import botstore                          # noqa: E402
botstore._DATA_DIR = _SANDBOX
from services import scalper as S, krx, kr_selector, krx_master   # noqa: E402
S.store = botstore.JsonStore("scalper.json", "test")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def at(h, m, s=0, day=21):
    return datetime(2026, 10, day, h, m, s, tzinfo=S.KST)       # 2026-10-21 수요일


Q = {}           # code -> 다음 시세
CALLS = []


def fake_quote(acc, code):
    CALLS.append(code)
    q = dict(Q[code])
    q.setdefault("code", code)
    return q


def mk(price, open_=10_000, prev=9_700, bid=None, ask=None, hoga="09:01:00", name="가나"):
    return {"price": price, "open": open_, "prevClose": prev, "changePct": round((price / prev - 1) * 100, 2),
            "bid": bid if bid is not None else price - 10, "ask": ask if ask is not None else price + 10,
            "hogaTime": hoga, "name": name}


krx.quote = fake_quote


def orig():
    """원본 규칙만 (추가 스킬 끔) — 원래 규칙을 따로 시험한다."""
    x = S.Scalper(namuh_account=object())
    x.params.timeStopOn = x.params.imbalanceOn = x.params.pyramidOn = False
    return x
krx_master.load = lambda: [{"code": "000001"}]
krx_master.candidates = lambda rows: rows
SEL = {"chosen": [{"code": "000001", "name": "가나", "gap": 3.1, "score": 2.0}],
       "prelist": [{"code": "000001", "name": "가나"}]}
kr_selector.select = lambda *a, **k: dict(SEL, scanned=300, withExpected=280)
kr_selector.refine = lambda *a, **k: {"chosen": SEL["chosen"]}

print("── 장 전 선정 ──")
sc = orig()
check("08:49 에는 기다린다", sc.step(at(8, 49)) >= 1 and sc.day["phase"] == "wait", "")
sc.step(at(8, 50, 5))
check("08:50 후보 훑기 → 예비 목록", sc.day["phase"] == "selected" and len(sc.day["prelist"]) == 1, sc.day["phase"])
sc.step(at(8, 57, 55))
check("08:57:50 다시 거르기 → 오늘의 대장주", sc.day["phase"] == "ready" and sc.day["chosen"][0]["code"] == "000001", "")

print("── 진입 ──")
Q["000001"] = mk(10_030)                 # 시가 대비 +0.3% — 돌파 기준(+0.5%) 미달
sc.step(at(9, 1))
check("시가 +0.5% 를 넘기 전에는 사지 않는다", not sc.positions, "")
Q["000001"] = mk(10_060)
sc.step(at(9, 1, 5))
pos = sc.positions.get("000001")
check("시가 +0.5% 돌파 → 매도1호가로 300만원어치", pos and pos["entry"] == 10_070 and pos["qty"] == 3_000_000 // 10_070,
      f"{pos and (pos['entry'], pos['qty'])}")
Q["000001"] = mk(11_200, prev=9_700)     # 등락률 +15% — 이미 보유 중이라 상관없음
print("── 청산 규칙 ──")
p = S.ScalpParams()
po = {"entry": 10_000.0, "peak": 10_000.0, "remaining": 100, "partialDone": False}
check("손절: 순수익률(비용 0.20% 차감) −1.5% 이하 → 전량", S.decide_exit(dict(po), 9_870, p)["kind"] == "STOP_LOSS", "9,870원 = −1.5%")
check("1차 익절: +1.8% → 절반", (lambda a: a["kind"] == "PARTIAL" and a["qty"] == 50)(S.decide_exit(dict(po), 10_200, p)), "")
check("목표 익절: +2.5% → 전량", S.decide_exit(dict(po), 10_270, p)["kind"] == "TARGET", "")
pk = dict(po, peak=10_250.0, partialDone=True)
check("트레일링: 고점이 +1.8% 를 넘은 뒤 −0.8% → 전량", S.decide_exit(pk, 10_160, p)["kind"] == "TRAIL", "")
check("그 사이면 아무것도 안 한다", S.decide_exit(dict(po), 10_050, p) is None, "")
check("등락률 +10% 넘는 종목은 진입하지 않는다", not S.can_enter(mk(10_800, prev=9_700), p), "+11.3%")

print("── 하루 흐름 · 손익 ──")
Q["000001"] = mk(10_300, bid=10_290)     # 매수가 10,070 → +2.08% (비용 뺀 뒤 +1.98%) → 1차 익절
sc.step(at(9, 3))
pos = sc.positions["000001"]
check("1차 익절은 매수1호가로 절반", pos["partialDone"] and pos["remaining"] == pos["qty"] - pos["qty"] // 2
      and sc.trades[-1]["kind"] == "PARTIAL" and sc.trades[-1]["price"] == 10_290, f"{sc.trades[-1]}")
Q["000001"] = mk(10_200, bid=10_190)     # 고점 10,300 대비 −0.97% → 트레일링
sc.step(at(9, 5))
check("나머지는 트레일링으로 전량 · 보유 없음", not sc.positions and sc.trades[-1]["kind"] == "TRAIL", "")
Q["000001"] = mk(10_100, hoga="09:12:00")     # 한산해 호가 시각이 9분 전이어도 휴장으로 보지 않는다
sc.step(at(9, 21))
check("장중에는 호가가 잠시 멈춰도 휴장으로 끝내지 않는다", not sc.day["done"], sc.day.get("note"))
check("09:20 뒤에는 새로 사지 않는다", not sc.positions, "")
sc.step(at(9, 30, 1))
check("09:30 이후 하루를 닫고 결과를 남긴다", sc.day["done"] and sc.days[-1]["date"] == "2026-10-21"
      and sc.days[-1]["positions"] == 1 and sc.days[-1]["wins"] == 1 and sc.days[-1]["pnl"] > 0
      and "청산 완료" in sc.days[-1]["note"], f"{sc.days[-1]}")

print("── 강제 청산 · 휴장 · 늦은 기동 ──")
sc2 = orig()
sc2.step(at(8, 50, 5, day=22)); sc2.step(at(8, 57, 55, day=22))
Q["000001"] = mk(10_060, hoga="09:01:00")
sc2.step(at(9, 1, day=22))
Q["000001"] = mk(10_040, bid=10_030, hoga="09:29:59")
sc2.step(at(9, 30, 2, day=22))
check("09:30 에 남은 물량은 전량 강제 청산", not sc2.positions and sc2.trades[-1]["kind"] == "FORCE" and sc2.day["done"], "")
sc3 = orig()
sc3.step(at(8, 50, 5, day=23)); sc3.step(at(8, 57, 55, day=23))
Q["000001"] = mk(10_060, hoga="15:30:00")   # 어제 호가 그대로
sc3.step(at(9, 3, day=23))
check("호가가 갱신되지 않으면(휴장) 사지 않고 쉰다", not sc3.positions and sc3.day["done"] and "휴장" in sc3.day["note"],
      sc3.day.get("note"))
sc4 = orig()
sc4.step(at(9, 10, day=26))
check("장이 열린 뒤에 뜨면 그날은 쉰다 (장 전 데이터로 고를 수 없다)", sc4.day["done"] and not sc4.positions, sc4.day.get("note"))
check("주말은 쉰다", orig().step(at(9, 5, day=24)) == 600.0, "토요일")

print("── 재진입 금지 · 하루 손실 한도 · MFE/MAE ──")
check("다 판 종목에 최대 상승 · 하락폭이 남는다", sc.trades[-1].get("mfePct") is not None
      and sc.trades[-1]["mfePct"] == round((10_300 / 10_070 - 1) * 100, 2) and sc.trades[-1]["maePct"] <= 0,
      f"MFE {sc.trades[-1].get('mfePct')}% · MAE {sc.trades[-1].get('maePct')}%")
s5 = orig()
s5.step(at(8, 50, 5, day=27)); s5.step(at(8, 57, 55, day=27))
Q["000001"] = mk(10_060, hoga="09:01:00")
s5.step(at(9, 1, day=27))
Q["000001"] = mk(9_850, bid=9_840, hoga="09:02:00")          # 손절
s5.step(at(9, 2, day=27))
Q["000001"] = mk(10_100, hoga="09:04:00")                    # 다시 시가 돌파
s5.step(at(9, 4, day=27))
check("손절한 종목은 그날 다시 사지 않는다", not s5.positions and s5.trades[-1]["kind"] == "STOP_LOSS"
      and s5.trades[-1].get("maePct", 0) < 0, f"{[t['kind'] for t in s5.trades if t['date'] == '2026-10-27']}")
s6 = orig()
s6.params.maxStocks = 3
SEL["chosen"] = [{"code": c, "name": c, "gap": 3.0, "score": 1.0} for c in ("A1", "A2", "A3")]
for c in ("A1", "A2", "A3"):
    Q[c] = mk(10_060, hoga="09:01:00", name=c)
s6.step(at(8, 50, 5, day=28)); s6.step(at(8, 57, 55, day=28))
Q["A1"] = mk(10_060, hoga="09:01:00", name="A1")
Q["A2"] = mk(10_000, hoga="09:01:00", name="A2"); Q["A3"] = mk(10_000, hoga="09:01:00", name="A3")
s6.step(at(9, 1, day=28))                                    # A1 만 산다
Q["A1"] = mk(9_850, bid=9_840, hoga="09:02:00", name="A1")
s6.step(at(9, 2, day=28))                                    # A1 손절 (1번)
s6.day["stops"] = 2                                          # 두 번째 손절이 났다고 치고
Q["A2"] = mk(10_100, hoga="09:03:00", name="A2")
s6.step(at(9, 3, day=28))
check("하루 손절 2번이면 그날 신규 진입을 멈춘다", "A2" not in s6.positions and "손절 2번" in s6.day.get("halt", ""),
      s6.day.get("halt"))
s6.step(at(9, 31, day=28))
check("일별 결과에 손절 수 · 중단 사유가 남는다", s6.days[-1]["stops"] == 2 and s6.days[-1]["halt"], f"{s6.days[-1]}")
s7 = orig()
s7.day = {"date": "2026-10-29", "stops": 0}
s7.trades = [{"date": "2026-10-29", "kind": "STOP_LOSS", "pnl": -100_001}]
check("하루 실현 손실이 시드의 −1%(10만원)를 넘어도 멈춘다", "손실" in s7._day_halt(), s7.day.get("halt"))
SEL["chosen"] = [{"code": "000001", "name": "가나", "gap": 3.1, "score": 2.0}]

print("── 호가 잔량 체결 · VI · 조회 줄이기 · 미청산 정리 ──")
qd = {"price": 10_000, "ask": 10_010, "asks": [(10_010, 100), (10_020, 100), (10_030, 1_000)]}
qty, avg = S.walk_buy(qd, 3_000_000)
check("살 때 매도호가를 먹어 내려간다 (1호가 100주 · 2호가 100주 · 나머지 3호가)",
      qty == 299 and abs(avg - (100 * 10_010 + 100 * 10_020 + 99 * 10_030) / 299) < 1e-6, f"{qty}주 · 평균 {avg:,.1f}원")
check("10단계를 다 먹어도 못 채우면 사지 않는다", S.walk_buy({"price": 10_000, "asks": [(10_010, 10)]}, 3_000_000) is None, "")
check("팔 때 매수호가를 먹어 내려간다 · 모자라면 마지막 단계 가격",
      S.walk_sell({"price": 10_000, "bids": [(9_990, 100), (9_980, 50)]}, 200) == (100 * 9_990 + 100 * 9_980) / 200, "")
check("호가 잔량이 없으면 1호가 한 가격 (예전 방식)", S.walk_buy({"price": 10_000, "ask": 10_010}, 3_000_000) == (299, 10_010.0), "")
p0 = S.ScalpParams()
check("정적 VI 상단까지 +2.5% 여유가 없으면 진입하지 않는다",
      not S.vi_room_ok({"viUp": 10_300}, 10_060, p0) and S.vi_room_ok({"viUp": 11_000}, 10_060, p0)
      and S.vi_room_ok({"viUp": 0}, 10_060, p0), "VI 10,300 · 진입 10,060 → 목표 10,311")
s8 = orig()
SEL["chosen"] = [{"code": c, "name": c, "gap": 3.0, "score": 1.0} for c in ("B1", "B2", "B3")]
s8.step(at(8, 50, 5, day=30)); s8.step(at(8, 57, 55, day=30))
Q["B1"] = dict(mk(10_060, hoga="09:01:00", name="B1"), viUp=10_300)           # VI 가까움 → 그날 제외
Q["B2"] = dict(mk(10_060, hoga="09:01:00", name="B2"), asks=[(10_070, 5)])     # 호가 얇음 → 계속 봄
Q["B3"] = mk(10_000, hoga="09:01:00", name="B3")
s8.step(at(9, 1, day=30))
check("VI 로 거른 종목은 그날 제외, 호가가 얇은 종목은 계속 본다",
      "B1" in s8.day.get("skipped", {}) and "B2" in s8.day.get("thin", {}) and not s8.positions, "")
CALLS.clear()
Q["B2"] = mk(10_060, hoga="09:02:00", name="B2")
s8.step(at(9, 2, day=30))
check("다음 판단에는 제외한 종목을 조회하지 않고, 얇았던 종목은 다시 보고 산다",
      "B1" not in CALLS and "B2" in s8.positions, f"{CALLS}")
CALLS.clear()
s8.step(at(9, 3, day=30))
check("보유 · 아직 살 수 있는 종목만 조회한다", sorted(CALLS) == ["B2", "B3"], f"{CALLS}")
CALLS.clear()
s8.step(at(9, 21, day=30))
check("09:20 뒤에는 보유 종목만 조회한다", CALLS == ["B2"], f"{CALLS}")
s9 = orig()
s9.day = {"date": "2026-10-30"}
s9.positions = {"X": {"code": "X", "name": "X", "entry": 10_000.0, "qty": 100, "remaining": 100, "peak": 10_000.0,
                      "low": 10_000.0, "partialDone": False, "pnl": 0.0}}
s9.step(at(7, 0, day=31))
check("서버 중단으로 남은 미청산분은 손익 0 으로 정리한다 (비용도 빼지 않는다)",
      not s9.positions and s9.trades[-1]["kind"] == "STALE" and s9.trades[-1]["pnl"] == 0, f"{s9.trades[-1]['pnl']}")
SEL["chosen"] = [{"code": "000001", "name": "가나", "gap": 3.1, "score": 2.0}]

print("── 추가 스킬 (② 30초 타임컷 · ③ 호가 불균형 · ⑥ 불타기) ──")
check("새 봇은 세 스킬이 켜진 채 시작한다", (lambda p: p.timeStopOn and p.imbalanceOn and p.pyramidOn)(S.ScalpParams()), "")
SEL["chosen"] = [{"code": "C1", "name": "C1", "gap": 3.0, "score": 1.0}]
k1 = S.Scalper(namuh_account=object())
k1.step(at(8, 50, 5, day=2)); k1.step(at(8, 57, 55, day=2))      # 2026-10-02 금
Q["C1"] = dict(mk(10_060, hoga="09:01:00", name="C1"), totalAsk=100_000, totalBid=80_000)
k1.step(at(9, 1, day=2))
check("③ 매도잔량 ÷ 매수잔량 < 2배면 사지 않고 계속 본다", not k1.positions and "C1" in k1.day.get("thin", {}), k1.day.get("thin"))
Q["C1"] = dict(mk(10_060, hoga="09:01:10", name="C1"), totalAsk=300_000, totalBid=100_000)
k1.step(at(9, 1, 10, day=2))
check("③ 3배면 산다 (스킬 기록)", "C1" in k1.positions and "호가불균형 3.0배" in k1.positions["C1"]["skills"], "")
Q["C1"] = dict(mk(10_120, hoga="09:01:30", name="C1"), totalAsk=300_000, totalBid=100_000)
k1.step(at(9, 1, 30, day=2))
check("② 30초가 안 됐으면 기다린다", "C1" in k1.positions, "")
Q["C1"] = dict(mk(10_110, hoga="09:01:41", name="C1"), totalAsk=300_000, totalBid=100_000)
k1.step(at(9, 1, 41, day=2))
t = k1.trades[-1]
check("② 30초 안에 +1.5% 를 못 찍으면 판다 (손절선까지 기다리지 않음)", not k1.positions and t["kind"] == "TIME"
      and "30초 타임컷" in t.get("skills", []), f"{t['kind']} {t['pct']}%")

k2 = S.Scalper(namuh_account=object())
k2.step(at(8, 50, 5, day=19)); k2.step(at(8, 57, 55, day=19))
Q["C1"] = dict(mk(10_060, hoga="09:01:00", name="C1", ask=10_070), totalAsk=300_000, totalBid=100_000, volume=100_000)
k2.step(at(9, 1, day=19))
first_qty = k2.positions["C1"]["qty"]
Q["C1"] = dict(mk(10_250, hoga="09:01:20", name="C1", ask=10_260), totalAsk=300_000, totalBid=100_000, volume=110_000)
k2.step(at(9, 1, 20, day=19))       # +1.79% (타임컷 통과) · 거래 속도 첫 측정
Q["C1"] = dict(mk(10_260, hoga="09:01:25", name="C1", ask=10_270), totalAsk=300_000, totalBid=100_000, volume=160_000)
k2.step(at(9, 1, 25, day=19))       # 5초에 5만주 = 1만주/초 ≥ 평균(160,000/85초 ≈ 1,882) × 2
pos = k2.positions.get("C1") or {}
check("⑥ +1.5% 넘고 거래가 몰리면 처음 수량의 30% 를 더 산다", pos.get("pyramided") and pos["qty"] == first_qty + int(first_qty * 0.3)
      and k2.trades[-1]["kind"] == "PYRAMID", f"{first_qty} → {pos.get('qty')}")
check("⑥ 보존선을 새 평단(비용 포함) 위로 올린다", pos.get("floor") and pos["floor"] > pos["entry"], f"평단 {pos.get('entry', 0):,.1f} · 보존선 {pos.get('floor')}")
Q["C1"] = dict(mk(int(pos["floor"]) - 5, hoga="09:01:40", name="C1"), totalAsk=300_000, totalBid=100_000, volume=170_000)
k2.step(at(9, 1, 40, day=19))
check("⑥ 보존선 아래로 내려오면 전량 (원금 보존)", not k2.positions and k2.trades[-1]["kind"] == "FLOOR", "")
k3 = S.Scalper(namuh_account=object())
k3.params.seedKrw = 3_100_000                     # 불타기하면 시드를 넘는다
k3.positions = {"C1": {"code": "C1", "name": "C1", "entry": 10_000.0, "firstEntry": 10_000.0, "qty": 300, "remaining": 300,
                       "peak": 10_200.0, "low": 10_000.0, "partialDone": False, "pnl": 0.0, "cost": 3_000_000,
                       "timeOk": True, "pyramided": False, "floor": 0.0, "skills": []}}
k3._pyramid("C1", {"code": "C1", "price": 10_200, "ask": 10_210}, at(9, 2, day=20))
check("⑥ 불타기로 시드를 넘으면 더 사지 않는다", k3.positions["C1"]["qty"] == 300 and k3.positions["C1"]["pyramided"], "")
SEL["chosen"] = [{"code": "000001", "name": "가나", "gap": 3.1, "score": 2.0}]

k4 = S.Scalper(namuh_account=object())
k4.set_skills({"pyramidOn": False, "timeStopOn": None})
check("스킬을 하나씩 끌 수 있다 (안 보낸 것은 그대로)", not k4.params.pyramidOn and k4.params.timeStopOn and k4.params.imbalanceOn, "")

print("── 시세 오류 표시 ──")
ke = S.Scalper(namuh_account=object())
orig_q = krx.quote
krx.quote = lambda acc, code: (_ for _ in ()).throw(S.NamuhError("Failed to resolve 'api.nhplug.com' (시험)"))
ke._quote("X"); ke._quote("X")
check("연속 실패는 시각 · 횟수와 함께 보이고 로그에는 한 번만", "연속 2회" in ke.last_error
      and sum("시세 조회 실패" in l["message"] for l in ke.logs) == 1, ke.last_error)
krx.quote = orig_q
ke._quote("000001")
check("회복하면 오류 표시를 지운다 (예전에는 계속 떠 있었다)", ke.last_error == "" and "회복" in ke.logs[-1]["message"], "")

print("── 저장 · 끄기 ──")
sc.save()
r = orig(); r.load()
check("재시작해도 기록이 이어진다", len(r.trades) == len(sc.trades) and r.days[-1]["date"] == "2026-10-21", "")
r.set_enabled(False)
n = len(CALLS)
check("끄면 아무것도 조회하지 않는다", r.step(at(9, 5, day=27)) == 30.0 and len(CALLS) == n, "")
st = sc.status()
check("화면용 요약 (모의 · 손익 · 승률 · 1건당 손익 · 평균 MFE/MAE)", st["paper"] and st["summary"]["wins"] == 1
      and st["summary"]["winRate"] == 100.0 and st["summary"]["perPosition"] > 0 and st["summary"]["avgMfePct"] > 0,
      f"{st['summary']}")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
