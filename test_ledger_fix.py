#!/usr/bin/env python3
"""scripts/namuh_ledger_fix.py — 잘못 적힌 매수 되돌리기 · 앱에서 직접 판 봇 몫 반영(+봇 삭제)."""

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
_SANDBOX = tempfile.mkdtemp(prefix="ledgerfix-test-")
from services import botstore                          # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
from services.jsonfile import read_records, write_records   # noqa: E402
import namuh_ledger_fix as lf                          # noqa: E402
import namuh_to_excel                                  # noqa: E402

lf.TRADE_LOG = os.path.join(_SANDBOX, "trades_namuh.json")
namuh_to_excel.service_active = lambda: False
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def run(argv):
    with contextlib.redirect_stdout(io.StringIO()):
        return lf.main(argv)


print("── 잘못 적힌 매수 되돌리기 ──")
row = {"id": "t-1", "action": "BUY_CHUNK", "time": "2026-10-30 20:05:12", "units": 10.0, "price": 55.15, "amountKrw": 551.5}
keep = {"id": "t-2", "action": "BUY_CHUNK", "time": "2026-11-02 21:05:22", "units": 2.0, "price": 68.17, "amountKrw": 136.34}
write_records(botstore.namuh_store_file(), "bots", [{
    "botId": "SOXL-1", "coin": "SOXL", "broker": "namuh", "units": 41.0, "totalInvested": 3606.01, "entryPrice": 87.9515,
    "turn": 18, "cash": 2390.7446, "params": {"feePct": 0.09}, "tradeHistory": [keep, row]}])
write_records(lf.TRADE_LOG, "trades", [dict(keep), dict(row)])
rc = run(["SOXL", "2026-10-30", "--remove-buy", "--apply"])
r = read_records(botstore.namuh_store_file(), "bots", "")[0]
check("10/30 매수 한 건을 되돌린다 (31주 · 평단 $98.53 · 17회차 · 현금 +$552.00)",
      rc == 0 and r["units"] == 31.0 and abs(r["entryPrice"] - 98.5326) < 1e-3 and r["turn"] == 17
      and abs(r["cash"] - 2942.7409) < 1e-3, f"{r['units']}주 · ${r['entryPrice']:.2f} · T={r['turn']}")
check("봇 체결 기록 · 체결 일지에서 그 한 건만 지운다",
      [t["id"] for t in r["tradeHistory"]] == ["t-2"] and [t["id"] for t in read_records(lf.TRADE_LOG, "trades", "")] == ["t-2"], "")
check("두 번 돌려도 또 빼지 않는다", run(["SOXL", "2026-10-30", "--remove-buy", "--apply"]) == 1, "")

print("── 앱에서 직접 판 봇 몫 반영 · 봇 삭제 ──")
write_records(botstore.namuh_store_file(), "bots", [
    {"botId": "SOXL-9", "coin": "SOXL", "broker": "namuh", "mode": "LIVE", "units": 3.0, "totalInvested": 107.07,
     "entryPrice": 35.69, "turn": 2, "cash": 3906.67, "realizedPnl": 10.0, "params": {"feePct": 0.09}, "tradeHistory": []},
    {"botId": "TQQQ-9", "coin": "TQQQ", "broker": "namuh", "units": 10.0, "totalInvested": 471.9, "turn": 5}])
write_records(lf.TRADE_LOG, "trades", [])
rc = run(["SOXL", "--external-sell", "3", "42.72", "2026-11-23", "--delete"])
check("미리 보기는 아무것도 바꾸지 않는다", rc == 0 and len(read_records(botstore.namuh_store_file(), "bots", "")) == 2, "")
rc = run(["SOXL", "--external-sell", "3", "42.72", "2026-11-23", "--delete", "--apply"])
left = read_records(botstore.namuh_store_file(), "bots", "")
log = read_records(lf.TRADE_LOG, "trades", "")
check("직접 판 3주 @ $42.72 를 매도로 적고 SOXL 봇을 지운다 · TQQQ 봇은 그대로",
      rc == 0 and [x["coin"] for x in left] == ["TQQQ"] and log and log[0]["action"] == "SELL"
      and abs(log[0]["pnlKrw"] - (3 * 42.72 * (1 - 0.0009) - 107.07)) < 0.01, f"손익 {log and log[0]['pnlKrw']}")
write_records(botstore.namuh_store_file(), "bots", [
    {"botId": "SOXL-8", "coin": "SOXL", "broker": "namuh", "units": 3.0, "totalInvested": 107.07, "params": {}}])
check("봇 장부보다 많이 팔았다고 적지 않는다", run(["SOXL", "--external-sell", "5", "42.72", "2026-11-23", "--apply"]) == 1, "")
namuh_to_excel.service_active = lambda: True
check("서비스가 돌고 있으면 쓰지 않는다", run(["SOXL", "--external-sell", "3", "42.72", "2026-11-23", "--apply"]) == 1, "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
