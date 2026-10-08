#!/usr/bin/env python3
"""크립토 / 나무증권 프로세스 분리를 실제 두 프로세스로 띄워 확인한다.

임시 폴더에서만 돈다. .env 를 읽지 않고, 키 파일·토큰 파일·장부 경로를
모두 임시 폴더로 돌린다. 봇은 PAPER · 미가동 상태로만 넣어 주문이 나갈
길이 없다.

보는 것
  - 처음 뜰 때 나무증권 봇이 bots_namuh.json 으로 옮겨지고, 원래 파일에는
    빗썸 봇만 남는다 (봇이 사라지거나 두 번 돌지 않는다)
  - 화면(crypto)에서 두 쪽 봇이 모두 보인다
  - 워커는 내부 토큰 없는 요청을 거절한다
  - 매매일지는 두 쪽을 합치되 같은 체결을 두 번 세지 않는다
  - 나무증권 경로(/api/namuh · /api/muma)는 워커로 넘어간다
  - 나무증권 봇 삭제는 워커에서 일어나고 빗썸 장부를 건드리지 않는다
  - 워커가 죽어도 화면과 빗썸 봇은 살아 있고, 그 사실을 알린다
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import requests

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from services.jsonfile import read_records, write_records     # noqa: E402

PASS, FAIL = [], []
PASSWORD = "split-test-local-only"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LAUNCHER = r"""
import os, sys
sb = os.environ["SPLIT_SANDBOX"]
sys.path.insert(0, os.environ["APP_DIR"])
from services import roles, botstore, tradelog, keystore, gemini_service, namuh
roles._DATA_DIR = sb
roles._TOKEN_FILE = os.path.join(sb, ".internal_token")
botstore._DATA_DIR = sb
botstore.STORE_FILE = os.path.join(sb, "bots.json")
botstore.arb_store.path = os.path.join(sb, "arb_bots.json")
tradelog.LEGACY_FILE = os.path.join(sb, "trades.json")
tradelog.LOG_FILE = os.path.join(sb, "trades_namuh.json" if roles.ROLE == "namuh" else "trades.json")
keystore.KEYS_FILE = os.path.join(sb, "bithumb_key.json")
keystore.NAMUH_KEYS_FILE = os.path.join(sb, "namuh_key.json")
gemini_service.GEMINI_KEY_FILE = os.path.join(sb, "gemini_key.json")
namuh.TOKEN_FILE = os.path.join(sb, "namuh_token.json")
import server, uvicorn
uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
"""


def launch(role, port, sandbox, worker_port):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("BITHUMB_", "GEMINI_", "NAMUH_", "BINANCE_", "APP_"))}
    env.update({"APP_SKIP_DOTENV": "1", "APP_ROLE": role, "PORT": str(port),
                "APP_ACCESS_PASSWORD": PASSWORD, "APP_SPREAD_RECORDER": "0",
                "APP_NAMUH_WORKER_URL": f"http://127.0.0.1:{worker_port}",
                "SPLIT_SANDBOX": sandbox, "APP_DIR": APP_DIR, "PYTHONPATH": APP_DIR})
    log = open(os.path.join(sandbox, f"{role}.log"), "w")
    return subprocess.Popen([sys.executable, "-c", LAUNCHER], env=env, cwd=APP_DIR,
                            stdout=log, stderr=subprocess.STDOUT)


def wait_up(port, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1).ok:
                return True
        except requests.RequestException:
            time.sleep(0.3)
    return False


def bot(bid, coin, broker, currency):
    return {"botId": bid, "coin": coin, "interval": "24h" if broker == "namuh" else "1h",
            "mode": "PAPER", "initialKrw": 4000 if broker == "namuh" else 100000,
            "broker": broker, "currency": currency, "params": {},
            "cash": 4000 if broker == "namuh" else 100000, "units": 0, "entryPrice": 0,
            "turn": 0, "totalInvested": 0, "tradeHistory": [], "wasRunning": False}


sb = tempfile.mkdtemp(prefix="split-test-")
write_records(os.path.join(sb, "bots.json"), "bots",
              [bot("XRP-1-aaaaaa", "XRP", "bithumb", "KRW"), bot("TQQQ-1-bbbbbb", "TQQQ", "namuh", "USD")])
write_records(os.path.join(sb, "trades.json"), "trades", [
    {"id": "t-crypto", "botId": "XRP-1-aaaaaa", "coin": "XRP", "broker": "bithumb", "currency": "KRW",
     "action": "BUY", "time": "2026-09-27 10:00:00", "price": 2000, "units": 10, "amountKrw": 20000,
     "pnlKrw": 0, "mode": "PAPER"},
    {"id": "t-namuh", "botId": "TQQQ-1-bbbbbb", "coin": "TQQQ", "broker": "namuh", "currency": "USD",
     "action": "SELL", "time": "2026-09-26 05:00:00", "price": 80, "units": 1, "amountKrw": 80,
     "pnlKrw": 5, "returnPct": 6.7, "mode": "PAPER"},
])

wport, cport = free_port(), free_port()
procs = []
try:
    print("── 기동 ──")
    procs.append(launch("crypto", cport, sb, wport))       # 화면이 먼저 떠도 안전해야 한다
    procs.append(launch("namuh", wport, sb, wport))
    check("crypto 프로세스가 뜬다", wait_up(cport), f"127.0.0.1:{cport}")
    check("namuh 워커가 뜬다", wait_up(wport), f"127.0.0.1:{wport}")

    print("── 장부 분리 ──")
    main_bots = read_records(os.path.join(sb, "bots.json"), "bots", "test") or []
    namuh_bots = read_records(os.path.join(sb, "bots_namuh.json"), "bots", "test") or []
    check("bots.json 에는 빗썸 봇만 남는다", [b["botId"] for b in main_bots] == ["XRP-1-aaaaaa"],
          f"{[b['botId'] for b in main_bots]}")
    check("나무증권 봇은 bots_namuh.json 으로 옮겨진다", [b["botId"] for b in namuh_bots] == ["TQQQ-1-bbbbbb"],
          f"{[b['botId'] for b in namuh_bots]}")
    namuh_trades = read_records(os.path.join(sb, "trades_namuh.json"), "trades", "test") or []
    check("워커 일지는 나무증권 체결만 가져간다", [t["id"] for t in namuh_trades] == ["t-namuh"],
          f"{[t['id'] for t in namuh_trades]}")
    old = read_records(os.path.join(sb, "trades.json"), "trades", "test") or []
    check("원래 일지는 지우지 않는다", len(old) == 2, f"{len(old)}건")

    print("── 워커 보호 ──")
    r = requests.get(f"http://127.0.0.1:{wport}/api/bot/list", timeout=5)
    check("토큰 없는 요청은 403", r.status_code == 403, f"HTTP {r.status_code}")
    r = requests.get(f"http://127.0.0.1:{wport}/api/bot/list",
                     headers={"X-Internal-Token": "x" * 40}, timeout=5)
    check("틀린 토큰도 403", r.status_code == 403, f"HTTP {r.status_code}")
    r = requests.get(f"http://127.0.0.1:{wport}/", timeout=5)
    check("워커는 화면을 내주지 않는다", r.status_code == 403, f"HTTP {r.status_code}")

    print("── 화면(crypto) ──")
    r = requests.post(f"http://127.0.0.1:{cport}/api/auth/login", json={"password": PASSWORD}, timeout=5,
                      headers={"Host": "evil.example", "X-Requested-With": "alpha-console"})
    r2 = requests.get(f"http://127.0.0.1:{cport}/", timeout=5, headers={"Host": "localhost:8888"})
    check("Host 가 localhost · 127.0.0.1 이 아니면 거절한다 (DNS 리바인딩으로 로그인을 두드리지 못하게)",
          r.status_code == 400 and r2.status_code == 200, f"HTTP {r.status_code} · localhost {r2.status_code}")
    r = requests.post(f"http://127.0.0.1:{cport}/api/auth/login", json={"password": PASSWORD}, timeout=5)
    check("CSRF: 화면 헤더 없는 POST 는 거절한다 (다른 사이트의 폼이 전체 정지를 못 누르게)",
          r.status_code == 403, f"HTTP {r.status_code}")
    s = requests.Session()
    s.headers.update({"X-Requested-With": "alpha-console"})
    r = s.post(f"http://127.0.0.1:{cport}/api/auth/login", json={"password": PASSWORD}, timeout=5)
    check("로그인", r.ok, f"HTTP {r.status_code}")
    check("세션 쿠키는 SameSite=Strict · HttpOnly",
          "samesite=strict" in r.headers.get("set-cookie", "").lower() and "httponly" in r.headers.get("set-cookie", "").lower(), "")
    r = s.get(f"http://127.0.0.1:{cport}/api/namuh/..%2Fbot/list", timeout=5)
    r2 = requests.get(f"http://127.0.0.1:{cport}/api/namuh/%2e%2e/internal/trade_rows", cookies=s.cookies, timeout=5)
    check("워커로 넘기는 경로에서 .. 우회를 막는다", r.status_code in (400, 404) and r2.status_code == 400,
          f"HTTP {r.status_code} · {r2.status_code}")
    r = requests.get(f"http://127.0.0.1:{cport}/openapi.json", timeout=5)
    check("API 문서(/docs · /openapi.json)는 내주지 않는다", r.status_code == 404, f"HTTP {r.status_code}")
    r = requests.get(f"http://127.0.0.1:{cport}/", timeout=5)
    check("화면 응답에 X-Frame-Options: DENY", r.headers.get("x-frame-options") == "DENY", "")
    r = requests.get(f"http://127.0.0.1:{wport}/api/bot/list", headers={"X-Internal-Token": "tok\u00e9n-x" * 4}, timeout=5)
    check("워커: ASCII 가 아닌 토큰도 500 이 아니라 403", r.status_code == 403, f"HTTP {r.status_code}")
    r = requests.get(f"http://127.0.0.1:{cport}/api/muma/table", timeout=5)
    check("로그인 없이 나무증권 경로를 넘기지 않는다", r.status_code == 401, f"HTTP {r.status_code}")
    lst = s.get(f"http://127.0.0.1:{cport}/api/bot/list", timeout=20).json()
    ids = sorted(b["botId"] for b in lst["bots"])
    check("두 쪽 봇이 모두 보인다", ids == ["TQQQ-1-bbbbbb", "XRP-1-aaaaaa"], f"{ids}")
    check("워커 오류 표시가 없다", "workerError" not in lst, lst.get("workerError", ""))
    tr = s.get(f"http://127.0.0.1:{cport}/api/bot/trades", timeout=20).json()
    tids = sorted(t["id"] for t in tr["trades"])
    check("매매일지를 합치되 중복이 없다", tids == ["t-crypto", "t-namuh"], f"{tids}")
    mt = s.get(f"http://127.0.0.1:{cport}/api/muma/table", timeout=20)
    check("무매법 표는 워커가 만든다", mt.ok and [r["botId"] for r in mt.json()["rows"]] == ["TQQQ-1-bbbbbb"],
          f"HTTP {mt.status_code}")

    print("── 삭제 경로 ──")
    r = s.post(f"http://127.0.0.1:{cport}/api/bot/delete", json={"botId": "TQQQ-1-bbbbbb"}, timeout=30)
    check("나무증권 봇 삭제를 워커로 넘긴다", r.ok, f"HTTP {r.status_code} {r.text[:80]}")
    namuh_bots = read_records(os.path.join(sb, "bots_namuh.json"), "bots", "test") or []
    main_bots = read_records(os.path.join(sb, "bots.json"), "bots", "test") or []
    check("워커 장부에서 지워진다", namuh_bots == [], f"{len(namuh_bots)}개")
    check("빗썸 장부는 그대로다", [b["botId"] for b in main_bots] == ["XRP-1-aaaaaa"], "")
    r = s.post(f"http://127.0.0.1:{cport}/api/bot/delete", json={"botId": "NOPE-1"}, timeout=10)
    check("없는 봇은 404 (워커 응답을 그대로 전한다)", r.status_code == 404, f"HTTP {r.status_code}")

    print("── 워커가 죽으면 ──")
    procs[1].terminate()
    procs[1].wait(10)
    lst = s.get(f"http://127.0.0.1:{cport}/api/bot/list", timeout=20).json()
    check("화면과 빗썸 봇은 계속 보인다", [b["botId"] for b in lst["bots"]] == ["XRP-1-aaaaaa"], "")
    check("워커가 꺼졌다고 알린다", "나무증권 프로세스" in (lst.get("workerError") or ""),
          (lst.get("workerError") or "")[:50])
    r = s.get(f"http://127.0.0.1:{cport}/api/namuh/market_status", timeout=10)
    check("나무증권 경로는 503 으로 실패를 알린다", r.status_code == 503, f"HTTP {r.status_code}")
    tr = s.get(f"http://127.0.0.1:{cport}/api/bot/trades", timeout=20).json()
    check("일지는 나누기 전 기록으로라도 보이고 경고를 띄운다",
          sorted(t["id"] for t in tr["trades"]) == ["t-crypto", "t-namuh"] and tr.get("ledgerWarning"),
          (tr.get("ledgerWarning") or "")[:50])

    print("── 되돌리기 (한 프로세스 all 로) ──")
    procs[0].terminate()
    procs[0].wait(10)
    # 나눠 돌던 동안 워커 쪽에만 생긴 봇과 체결을 흉내 낸다
    write_records(os.path.join(sb, "bots_namuh.json"), "bots", [bot("SOXL-2-cccccc", "SOXL", "namuh", "USD")])
    rows = read_records(os.path.join(sb, "trades_namuh.json"), "trades", "test") or []
    rows.insert(0, {"id": "t-after-split", "botId": "SOXL-2-cccccc", "coin": "SOXL", "broker": "namuh",
                    "currency": "USD", "action": "BUY", "time": "2026-09-29 05:00:00", "price": 150,
                    "units": 1, "amountKrw": 150, "pnlKrw": 0, "mode": "PAPER"})
    write_records(os.path.join(sb, "trades_namuh.json"), "trades", rows)
    aport = free_port()
    procs.append(launch("all", aport, sb, wport))
    check("all 로 다시 뜬다", wait_up(aport), f"127.0.0.1:{aport}")
    s2 = requests.Session()
    s2.headers.update({"X-Requested-With": "alpha-console"})
    s2.post(f"http://127.0.0.1:{aport}/api/auth/login", json={"password": PASSWORD}, timeout=5)
    ids = sorted(b["botId"] for b in s2.get(f"http://127.0.0.1:{aport}/api/bot/list", timeout=20).json()["bots"])
    check("두 파일의 봇을 모두 복원한다", ids == ["SOXL-2-cccccc", "XRP-1-aaaaaa"], f"{ids}")
    tids = sorted(t["id"] for t in s2.get(f"http://127.0.0.1:{aport}/api/bot/trades", timeout=20).json()["trades"])
    check("나눠 돌던 동안의 체결도 일지에 보인다", "t-after-split" in tids and len(tids) == len(set(tids)), f"{tids}")
finally:
    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
    if FAIL:
        for role in ("crypto", "namuh", "all"):
            print(f"\n── {role}.log ──")
            if os.path.exists(os.path.join(sb, f"{role}.log")):
                print(open(os.path.join(sb, f"{role}.log")).read()[-2000:])

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
