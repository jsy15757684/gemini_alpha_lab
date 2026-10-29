"""화면 확인용 로컬 인스턴스의 격리가 실제로 걸리는지 검증한다.

지난번 사고를 그대로 재현한다. `os.environ.pop()` 으로 키를 지우고 서버를
띄웠는데 실계좌 인증 호출이 나갔다 — server.py 가 import 시점에 `.env` 를
읽어 키를 다시 주입했기 때문이다. 빗썸은 IP 화이트리스트가 막아줬지만
Gemini 는 IP 제한이 없어 호출이 나가고 할당량을 썼다.

그래서 '키를 지웠다' 를 믿지 않고, **지운 뒤에도 남아 있으면 기동을
멈추는지** 와 **키가 들어와도 호출 자체가 막히는지** 를 본다.

각 항목을 별도 프로세스로 돌린다. 차단이 모듈 전역을 건드리므로 한 프로세스
안에서 섞으면 서로 오염된다.
"""

import os
import sys
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(APP_DIR, "scripts", "ui_preview.py")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def run(code, env=None):
    e = dict(os.environ)
    for k in ("BITHUMB_API_KEY", "BITHUMB_SECRET_KEY", "GEMINI_API_KEY",
              "BINANCE_API_KEY", "BINANCE_SECRET_KEY"):
        e.pop(k, None)
    e.update(env or {})
    e["PYTHONPATH"] = APP_DIR
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=e, cwd=APP_DIR, timeout=120)


print("\n── 화면 확인용 격리 (scripts/ui_preview.py) ──")

r = run(f"import runpy,sys; sys.argv=['ui_preview','--check']; "
        f"runpy.run_path({SCRIPT!r}, run_name='__main__')")
check("정상 경로에서 격리 점검을 통과한다", r.returncode == 0,
      f"종료코드 {r.returncode}")
check("빗썸 인증 API 를 차단한다", "빗썸 인증 API" in r.stdout, "4종")
check("나무증권 호출을 차단한다", "나무증권 토큰" in r.stdout, "")
check(".env 를 읽지 않는다", "APP_SKIP_DOTENV=1" in r.stdout, "server.py 가 건너뜀")
check("운영 데이터 경로를 쓰지 않는다",
      "데이터 경로 격리" in r.stdout and "/opt/" not in r.stdout, "임시 디렉터리")

# ── 사고 재현: .env 가 다시 주입되면? ──
r = run(f"""
import runpy, os, sys
sys.path.insert(0, {APP_DIR!r})
mod = runpy.run_path({SCRIPT!r})
mod['isolate']()
os.environ['BITHUMB_API_KEY'] = 'leaked-from-dotenv'   # server.py 가 주입한 상황
mod['block_live_calls']()
mod['verify']()
print('ERROR: 통과해버렸다')
""")
check("키가 다시 주입되면 기동을 멈춘다 (fail closed)",
      r.returncode != 0 and "기동을 중단" in (r.stdout + r.stderr),
      (r.stderr.strip().splitlines() or ["-"])[-1][:60])

# ── 차단이 뚫려 있으면? ──
r = run(f"""
import runpy, sys
sys.path.insert(0, {APP_DIR!r})
mod = runpy.run_path({SCRIPT!r})
mod['isolate']()
# 차단을 일부러 걸지 않는다 (block_live_calls 생략)
mod['verify']()
print('ERROR: 통과해버렸다')
""")
check("호출 차단이 없으면 기동을 멈춘다",
      r.returncode != 0 and "기동을 중단" in (r.stdout + r.stderr),
      (r.stderr.strip().splitlines() or ["-"])[-1][:60])

# ── 키가 있어도 주문이 나갈 수 없는가 ──
r = run(f"""
import runpy, sys
sys.path.insert(0, {APP_DIR!r})
mod = runpy.run_path({SCRIPT!r})
mod['isolate']()
mod['block_live_calls']()
from services import bithumb
acct = bithumb.BithumbAccount('real-looking-key', 'real-looking-secret')
try:
    acct.market_buy('BTC', 10000)
    print('NOT_BLOCKED')
except Exception as e:
    print('BLOCKED', type(e).__name__)
""")
check("실키를 쥐여줘도 시장가 매수가 나가지 않는다",
      "BLOCKED" in r.stdout and "NOT_BLOCKED" not in r.stdout,
      r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[:60])

print(f"\n{'='*58}")
if FAIL:
    print(f"통과 {len(PASS)}개 · 실패 {len(FAIL)}개")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print(f"전체 {len(PASS)}개 항목 통과")
