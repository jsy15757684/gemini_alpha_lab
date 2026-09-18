"""상태 파일 안전장치 검증.

확인하려는 사고는 하나다. 2026-09-18 에 data/ 안의 파일이 잠깐 root 소유가
되면서 서비스가 그 파일을 못 읽었고, 프로그램은 '기록이 없다' 고 믿고
진행한 뒤 다음 저장에서 과거 기록을 덮어썼다. 체결 일지 126건이 6건이 됐다.
같은 경로가 bots.json 에도 있어서, 거기서 터졌다면 실전 포지션을 들고 있는
봇이 통째로 사라질 수 있었다.

그래서 이 시험은 '못 읽었을 때 파일이 그대로 남아 있는가' 만 본다.

운영 데이터를 건드리지 않으려고 import 전에 모든 경로를 임시 폴더로 돌린다.
"""

import os
import sys
import json
import stat
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="store-safety-")

from services import botstore, tradelog, jsonfile   # noqa: E402

botstore._DATA_DIR = _SANDBOX
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore.arb_store.path = os.path.join(_SANDBOX, "arb_bots.json")
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def reset():
    """빗장과 메모리를 초기 상태로 돌린다."""
    botstore.guard.reset()
    botstore.arb_store.guard.reset()
    tradelog.guard.reset()
    tradelog._rows.clear()
    tradelog._loaded = False
    for f in os.listdir(_SANDBOX):
        os.remove(os.path.join(_SANDBOX, f))


def raw(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


print("\n── 봇 상태 파일 (bots.json) ──")

reset()
check("파일이 없으면 빈 목록이다 (첫 실행)",
      botstore.load() == [], "예외 없이 []")

botstore.save([{"botId": "A"}, {"botId": "B"}])
check("저장한 봇을 다시 읽는다",
      [b["botId"] for b in botstore.load()] == ["A", "B"], "A, B")

# ── 권한 오류 ──
reset()
botstore.save([{"botId": "LIVE-XRP", "units": 151.5}])
before = raw(botstore.STORE_FILE)
os.chmod(botstore.STORE_FILE, 0o000)

raised = False
try:
    botstore.load()
except jsonfile.StoreReadError:
    raised = True
except PermissionError:
    raised = False
check("권한 오류는 빈 목록이 아니라 예외다",
      raised, "StoreReadError" if raised else "예외가 안 났다")

os.chmod(botstore.STORE_FILE, 0o600)
check("읽기 실패 후에는 저장을 거부한다",
      botstore.guard.blocked, botstore.guard.reason or "")

botstore.save([])                       # 사고 재현: 빈 목록 저장 시도
check("실전 포지션 기록이 덮어써지지 않았다",
      raw(botstore.STORE_FILE) == before,
      f"{len(json.loads(raw(botstore.STORE_FILE))['bots'])}개 그대로")

check("빗장이 걸리면 삭제도 거부한다",
      (botstore.clear() or os.path.exists(botstore.STORE_FILE)), "파일 유지")

# ── 파손 ──
reset()
botstore.save([{"botId": "LIVE-SOL"}])
with open(botstore.STORE_FILE, "w", encoding="utf-8") as f:
    f.write('{"version": 1, "bots": [{"botId": "LIVE-SO')      # 잘린 파일
broken = raw(botstore.STORE_FILE)

raised = False
try:
    botstore.load()
except jsonfile.StoreReadError:
    raised = True
check("깨진 JSON 도 예외다", raised, "StoreReadError")

botstore.save([])
check("깨진 파일도 덮어쓰지 않는다 (사람이 볼 수 있게 남긴다)",
      raw(botstore.STORE_FILE) == broken, "원본 유지")

# ── 형식 이상 ──
reset()
with open(botstore.STORE_FILE, "w", encoding="utf-8") as f:
    json.dump({"version": 1}, f)                                # bots 키 없음
raised = False
try:
    botstore.load()
except jsonfile.StoreReadError:
    raised = True
check("'bots' 목록이 없는 파일도 예외다", raised, "StoreReadError")


print("\n── 체결 일지 (trades.json) ──")

reset()
tradelog.append({"id": "t-1", "action": "SELL", "pnlKrw": 1000})
tradelog.append({"id": "t-2", "action": "SELL", "pnlKrw": 2000})
check("체결이 장부에 쌓인다", len(tradelog.all_rows()) == 2, "2건")

before = raw(tradelog.LOG_FILE)
tradelog._rows.clear()
tradelog._loaded = False
os.chmod(tradelog.LOG_FILE, 0o000)

raised = False
try:
    tradelog.load()
except jsonfile.StoreReadError:
    raised = True
check("일지 읽기 실패도 예외다", raised, "StoreReadError")

os.chmod(tradelog.LOG_FILE, 0o600)
check("실패 사유를 화면에 줄 수 있다",
      bool(tradelog.warning()), (tradelog.warning() or "")[:40])

# 사고 재현: 읽지 못한 뒤 새 체결이 1건 들어온다
tradelog.append({"id": "t-3", "action": "BUY", "pnlKrw": 0})
kept = json.loads(raw(tradelog.LOG_FILE))["trades"]
check("새 체결 1건이 과거 장부를 지우지 않는다",
      raw(tradelog.LOG_FILE) == before and len(kept) == 2,
      f"파일에 {len(kept)}건 그대로 (사고 당시엔 126→6 이 됐다)")

check("체결 기록 중 예외가 매매 흐름을 끊지 않는다",
      len(tradelog.all_rows()) == 1, "append 가 예외를 올리지 않았다")

check("읽지 못한 장부에는 기존 기록 합치기도 하지 않는다",
      tradelog.seed([{"id": "old-1"}]) == 0, "0건 합침")


print("\n── 시뮬레이터 상태 (arb_bots.json) ──")

reset()
botstore.arb_store.save([{"botId": "SIM-1"}])
before = raw(botstore.arb_store.path)
os.chmod(botstore.arb_store.path, 0o000)
raised = False
try:
    botstore.arb_store.load()
except jsonfile.StoreReadError:
    raised = True
check("시뮬레이터도 같은 규칙을 따른다", raised, "StoreReadError")
os.chmod(botstore.arb_store.path, 0o600)
botstore.arb_store.save([])
check("시뮬레이터 기록도 덮어써지지 않는다",
      raw(botstore.arb_store.path) == before, "원본 유지")


print("\n── 봇 복원 (BotManager.restore) ──")

reset()
from services.trader import BotManager   # noqa: E402

mgr = BotManager()
botstore.save([{"botId": "XRP-live", "coin": "XRP", "mode": "LIVE"}])
os.chmod(botstore.STORE_FILE, 0o000)
summary = mgr.restore(None)
os.chmod(botstore.STORE_FILE, 0o600)

check("상태 파일을 못 읽으면 봇을 하나도 가동하지 않는다",
      summary["restored"] == 0 and summary["resumed"] == 0 and len(mgr.bots) == 0,
      "복원 0 · 가동 0")
check("복원 실패를 치명 오류로 보고한다 (화면 배너용)",
      summary.get("fatal") is True and bool(mgr.restore_error),
      (mgr.restore_error or "")[:50] + "…")
check("복원에 실패해도 상태 파일은 남는다",
      len(json.loads(raw(botstore.STORE_FILE))["bots"]) == 1, "1개 그대로")

shutil.rmtree(_SANDBOX, ignore_errors=True)

print(f"\n{'='*58}")
if FAIL:
    print(f"통과 {len(PASS)}개 · 실패 {len(FAIL)}개")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print(f"전체 {len(PASS)}개 항목 통과")
