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
krx_master.load = lambda: [{"code": "000001"}]
krx_master.candidates = lambda rows: rows
SEL = {"chosen": [{"code": "000001", "name": "가나", "gap": 3.1, "score": 2.0}],
       "prelist": [{"code": "000001", "name": "가나"}]}
kr_selector.select = lambda *a, **k: dict(SEL, scanned=300, withExpected=280)
kr_selector.refine = lambda *a, **k: {"chosen": SEL["chosen"]}

print("── 장 전 선정 ──")
sc = S.Scalper(namuh_account=object())
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
sc2 = S.Scalper(namuh_account=object())
sc2.step(at(8, 50, 5, day=22)); sc2.step(at(8, 57, 55, day=22))
Q["000001"] = mk(10_060, hoga="09:01:00")
sc2.step(at(9, 1, day=22))
Q["000001"] = mk(10_040, bid=10_030, hoga="09:29:59")
sc2.step(at(9, 30, 2, day=22))
check("09:30 에 남은 물량은 전량 강제 청산", not sc2.positions and sc2.trades[-1]["kind"] == "FORCE" and sc2.day["done"], "")
sc3 = S.Scalper(namuh_account=object())
sc3.step(at(8, 50, 5, day=23)); sc3.step(at(8, 57, 55, day=23))
Q["000001"] = mk(10_060, hoga="15:30:00")   # 어제 호가 그대로
sc3.step(at(9, 3, day=23))
check("호가가 갱신되지 않으면(휴장) 사지 않고 쉰다", not sc3.positions and sc3.day["done"] and "휴장" in sc3.day["note"],
      sc3.day.get("note"))
sc4 = S.Scalper(namuh_account=object())
sc4.step(at(9, 10, day=26))
check("장이 열린 뒤에 뜨면 그날은 쉰다 (장 전 데이터로 고를 수 없다)", sc4.day["done"] and not sc4.positions, sc4.day.get("note"))
check("주말은 쉰다", S.Scalper(namuh_account=object()).step(at(9, 5, day=24)) == 600.0, "토요일")

print("── 재진입 금지 · 하루 손실 한도 · MFE/MAE ──")
check("다 판 종목에 최대 상승 · 하락폭이 남는다", sc.trades[-1].get("mfePct") is not None
      and sc.trades[-1]["mfePct"] == round((10_300 / 10_070 - 1) * 100, 2) and sc.trades[-1]["maePct"] <= 0,
      f"MFE {sc.trades[-1].get('mfePct')}% · MAE {sc.trades[-1].get('maePct')}%")
s5 = S.Scalper(namuh_account=object())
s5.step(at(8, 50, 5, day=27)); s5.step(at(8, 57, 55, day=27))
Q["000001"] = mk(10_060, hoga="09:01:00")
s5.step(at(9, 1, day=27))
Q["000001"] = mk(9_850, bid=9_840, hoga="09:02:00")          # 손절
s5.step(at(9, 2, day=27))
Q["000001"] = mk(10_100, hoga="09:04:00")                    # 다시 시가 돌파
s5.step(at(9, 4, day=27))
check("손절한 종목은 그날 다시 사지 않는다", not s5.positions and s5.trades[-1]["kind"] == "STOP_LOSS"
      and s5.trades[-1].get("maePct", 0) < 0, f"{[t['kind'] for t in s5.trades if t['date'] == '2026-10-27']}")
s6 = S.Scalper(namuh_account=object())
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
s7 = S.Scalper(namuh_account=object())
s7.day = {"date": "2026-10-29", "stops": 0}
s7.trades = [{"date": "2026-10-29", "kind": "STOP_LOSS", "pnl": -100_001}]
check("하루 실현 손실이 시드의 −1%(10만원)를 넘어도 멈춘다", "손실" in s7._day_halt(), s7.day.get("halt"))
SEL["chosen"] = [{"code": "000001", "name": "가나", "gap": 3.1, "score": 2.0}]

print("── 저장 · 끄기 ──")
sc.save()
r = S.Scalper(namuh_account=object()); r.load()
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
