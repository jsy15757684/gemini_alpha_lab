#!/usr/bin/env python3
"""scripts/daily_report.py 의 보고서 만들기 — 가짜 데이터로 본다 (서버 · 텔레그램에 닿지 않는다)."""

import copy
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import daily_report as dr          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


SERVER_NOW = datetime(2026, 9, 30, 0, 35, 0)          # 서버 시계(UTC) = 09:35 KST
BASE = {
    "now": "2026-09-30T09:35:00+09:00",
    "services": {"bithumb-bot": "active", "bithumb-namuh": "active"},
    "errors": [], "tokenHours": 22.7, "problems": [],
    "account": {"mock": True, "buyingPower": {"usd": 99316.24},
                "holdings": [{"symbol": "TQQQ", "quantity": 6.0}, {"symbol": "SOXL", "quantity": 3.0}]},
    "bots": {"bots": [
        {"botId": "TQQQ-1", "coin": "TQQQ", "broker": "namuh", "mode": "LIVE", "units": 6.0, "turn": 4,
         "params": {"splitCount": 40}, "unrealizedPnlPct": -0.27, "isRunning": True},
        {"botId": "SOXL-1", "coin": "SOXL", "broker": "namuh", "mode": "LIVE", "units": 3.0, "turn": 3,
         "params": {"splitCount": 40}, "unrealizedPnlPct": -0.84, "isRunning": True},
        {"botId": "ORB-1", "coin": "ORB", "broker": "namuh", "mode": "PAPER", "units": 0, "isRunning": True,
         "lastDecision": "오늘 ORB 끝", "totalTrades": 0, "realizedPnlKrw": 0,
         "orbScan": {"preparedDate": "2026-09-30",
                     "selection": {"elapsedSec": 379, "scanned": 281, "affordable": 339, "withExpected": 184,
                                   "passed": 7, "truncated": True},
                     "watch": [{"name": "필옵틱스", "reason": "시초가 갭 +5.06% — 범위 +2~+5% 밖이라 쉽니다"},
                               {"name": "성호전자", "reason": "시초가 갭 +5.50% — 범위 +2~+5% 밖이라 쉽니다"},
                               {"name": "브이엠", "reason": "09:25 까지 돌파가 없어 오늘은 쉽니다"},
                               {"name": "와이씨", "reason": "돌파 대기 · 가격 ≤ OR고 · RVOL 473%"},
                               {"name": "새종목", "reason": "전 거래일 09:05 거래량 기록이 없어 RVOL 을 볼 수 없습니다 — 오늘은 사지 않고 기록만 합니다"}]}},
    ]},
    "trades": {"trades": [
        {"botId": "TQQQ-1", "coin": "TQQQ", "action": "BUY_CHUNK", "units": 2, "price": 77.73, "time": "2026-09-29 14:19:30"},
        {"botId": "TQQQ-1", "coin": "TQQQ", "action": "BUY_CHUNK", "units": 2, "price": 70.00, "time": "2026-09-27 14:19:30"},
    ]},
}


def rep(mut=None):
    d = copy.deepcopy(BASE)
    if mut:
        mut(d)
    return dr.build(d, server_now=SERVER_NOW)


print("── 정상 ──")
r = rep()
check("이상 없으면 첫 줄이 '이상 없음'", r.splitlines()[0] == "✅ 이상 없음", r.splitlines()[0])
check("모의계좌라고 적는다", "나무증권 모의계좌" in r, "")
check("장부 = 계좌면 ✅", "✅ TQQQ 장부 6 = 계좌 6주" in r and "✅ SOXL 장부 3 = 계좌 3주" in r, "")
check("지난 24시간 체결만 센다 (이틀 전 체결은 뺀다)", "지난 24시간 체결 1건" in r and "$77.73" in r and "$70.00" not in r, "")
check("ORB 선정 요약 · 잘림 표시 (예전 한 번 훑기 기록)", "조회 281/339 ⚠️ 시간 부족으로 일부만" in r and "통과 7" in r, "")


def refined(d):
    o = d["bots"]["bots"][2]["orbScan"]
    o["selection"] = {"refined": True, "scanned": 50, "withExpected": 48, "passed": 9, "truncated": False,
                      "sweep": {"elapsedSec": 460, "scanned": 339, "affordable": 339, "withExpected": 230,
                                "truncated": False, "prelist": 50}}


r = rep(refined)
check("08:40 훑기 → 08:58 다시 거르기 두 줄로 적는다",
      "08:40 훑기 460초 · 조회 339/339 · 예상체결 230 · 예비 50종목" in r and "08:58 다시 거르기 · 조회 50 · 예상체결 48 · 통과 9" in r
      and "시간 부족" not in r, "")
r = rep(lambda d: d["bots"]["bots"][2]["orbScan"]["selection"].__setitem__("refine", {"scanned": 0, "withExpected": 0}))
check("08:58 다시 거르기가 실패하면 경보", "ORB 08:58 다시 거르기 실패" in r.splitlines()[0], r.splitlines()[0])
check("09:05 판단을 사유별로 묶는다", "시초가 갭 범위 밖 2 — 필옵틱스, 성호전자" in r and "돌파 없음 1 — 브이엠" in r, "")
check("'돌파 대기' 문장 속 RVOL 숫자에 속지 않는다", "돌파 대기 1 — 와이씨" in r and "RVOL 부족" not in r, "")
check("첫날 RVOL 기준 없음을 따로 묶는다 ('쉽니다' 가 없는 문장)", "RVOL 기준 없음(첫날) 1 — 새종목" in r, "")

print("── 경보 ──")
r = rep(lambda d: d["account"]["holdings"].__setitem__(0, {"symbol": "TQQQ", "quantity": 5.0}))
check("장부 ≠ 계좌면 첫 줄 경보 + ⚠️", r.startswith("🚨 확인 필요: TQQQ 장부 6주 ≠ 계좌 5주") and "⚠️ TQQQ" in r, r.splitlines()[0])
r = rep(lambda d: d["account"].__setitem__("mock", False))
check("실계좌면 눈에 띄게 적는다", "⚠️ 실계좌" in r, "")
r = rep(lambda d: d["services"].__setitem__("bithumb-namuh", "failed"))
check("서비스가 멈추면 경보", "서비스 멈춤: bithumb-namuh(failed)" in r.splitlines()[0], r.splitlines()[0])
r = rep(lambda d: d.__setitem__("errors", ["ERROR 연결 실패 203.0.113.9:7070 계좌 5012345678 token=abcdefghijklmnop12"]))
check("ERROR 는 개수를 경보하고 IP · 긴 숫자 · 토큰을 가린다",
      "ERROR 1건" in r.splitlines()[0] and "<ip>" in r and "<숫자>" in r and "token=<숨김>" in r
      and "203.0.113.9" not in r and "5012345678" not in r and "abcdefghijklmnop12" not in r, "")
r = rep(lambda d: d.__setitem__("errors", None))
check("로그를 못 읽으면 그렇다고 쓴다 (0건으로 속이지 않는다)", "로그를 읽지 못했습니다" in r, "")
r = rep(lambda d: d["bots"]["bots"][2]["orbScan"].__setitem__("preparedDate", "2026-09-29"))
check("평일인데 오늘 선정이 없으면 경보", "ORB 오늘 선정 없음" in r.splitlines()[0], r.splitlines()[0])
r = rep(lambda d: (d.__setitem__("bots", None), d.__setitem__("problems", ["워커 /api/bot/list 조회 실패: ConnectionError"])))
check("워커에 못 닿으면 경보하고 멈추지 않는다", r.startswith("🚨") and "워커 /api/bot/list 조회 실패" in r, "")


def holiday(d):
    o = d["bots"]["bots"][2]["orbScan"]
    o["selection"]["withExpected"] = 0
    o["watch"] = [{"name": n, "reason": "OR 을 재지 못해 오늘은 쉽니다 (휴장 · 지연 개장 · 기동 지연)"} for n in ("가", "나")]


r = rep(holiday)
check("한국 휴장일(모든 종목 OR 못 잼)은 경보하지 않는다", r.splitlines()[0] == "✅ 이상 없음" and "휴장일로 보입니다" in r,
      r.splitlines()[0])
r = rep(lambda d: d.__setitem__("now", "2026-10-03T09:35:00+09:00"))
check("주말은 ORB 매매 없음으로 적는다", "주말 — 매매 없음" in r, "")

print("── 텔레그램 ──")
sent = []
dr._tg = lambda method, token, **p: sent.append(p) or {"ok": True}
dr.env_value = lambda k: {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}.get(k, "")
dr.send("\n".join(f"줄 {i} " + "가" * 50 for i in range(200)))
check("긴 보고는 4,096자 안으로 나눠 보낸다", len(sent) > 1 and all(len(p["text"]) <= dr.TG_LIMIT for p in sent),
      f"{len(sent)}개")
dr.env_value = lambda k: ""
try:
    dr.send("x")
    ok = False
except RuntimeError as e:
    ok = "TELEGRAM_BOT_TOKEN" in str(e)
check("설정이 없으면 무엇이 없는지 말하고 멈춘다", ok, "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
