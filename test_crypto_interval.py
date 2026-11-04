#!/usr/bin/env python3
"""scripts/crypto_interval.py — 빗썸 봇 캔들 간격만 바꾸고 장부는 그대로 두는가."""

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
_SANDBOX = tempfile.mkdtemp(prefix="interval-test-")
from services import botstore                          # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
from services.jsonfile import read_records, write_records   # noqa: E402
import crypto_interval as ci                           # noqa: E402
import namuh_to_excel                                  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


def run(*a):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = ci.main(list(a))
    return rc, out.getvalue()


base = [{"botId": "SOL-1", "coin": "SOL", "broker": "bithumb", "mode": "LIVE", "interval": "6h", "units": 4.3114,
         "entryPrice": 146517.73, "turn": 33, "cash": 109974.55, "realizedPnl": -58042.37, "params": {}},
        {"botId": "XRP-1", "coin": "XRP", "broker": "bithumb", "mode": "LIVE", "interval": "6h", "units": 164.15,
         "entryPrice": 1845.44, "turn": 32, "cash": 71294.57, "params": {}},
        {"botId": "TQQQ-1", "coin": "TQQQ", "broker": "namuh", "interval": "24h", "units": 22}]
write_records(botstore.STORE_FILE, "bots", [dict(r) for r in base])
namuh_to_excel.service_active = lambda: False
rc, _ = run("SOL", "XRP")
check("미리 보기는 파일을 바꾸지 않는다", rc == 0 and read_records(botstore.STORE_FILE, "bots", "")[0]["interval"] == "6h", "")
rc, _ = run("SOL", "XRP", "--apply")
got = {r["coin"]: r for r in read_records(botstore.STORE_FILE, "bots", "")}
check("--apply: 두 봇 모두 24h", rc == 0 and got["SOL"]["interval"] == got["XRP"]["interval"] == "24h", "")
check("장부(수량 · 평단 · 회차 · 현금 · 실현손익)는 그대로",
      all(got["SOL"][k] == base[0][k] for k in ("units", "entryPrice", "turn", "cash", "realizedPnl")), "")
check("나무증권 봇은 건드리지 않는다", got["TQQQ"] == base[2], "")
check("원본을 남긴다", any(f.startswith("bots.json.before-interval-") for f in os.listdir(_SANDBOX)), "")
rc, _ = run("SOL", "--interval", "7h", "--apply")
check("없는 간격은 거부", rc == 2, "")
namuh_to_excel.service_active = lambda: True
rc, out = run("SOL", "--apply")
check("빗썸 서비스가 돌고 있으면 쓰지 않는다", rc == 1 and "bithumb-bot" in out, "")
namuh_to_excel.service_active = lambda: False
recs = read_records(botstore.STORE_FILE, "bots", "")
recs[0]["orderHold"] = {"side": "buy"}
write_records(botstore.STORE_FILE, "bots", recs)
rc, _ = run("SOL", "--interval", "6h", "--apply")
check("결과를 모르는 주문이 있으면 바꾸지 않는다", rc == 1, "")

print("── scripts/namuh_ledger_fix.py (잘못 적힌 매수 한 건 되돌리기) ──")
import namuh_ledger_fix as lf                          # noqa: E402
lf.TRADE_LOG = os.path.join(_SANDBOX, "trades_namuh.json")
row = {"id": "t-1", "action": "BUY_CHUNK", "time": "2026-10-30 20:05:12", "units": 10.0, "price": 55.15, "amountKrw": 551.5}
keep = {"id": "t-2", "action": "BUY_CHUNK", "time": "2026-11-02 21:05:22", "units": 2.0, "price": 68.17, "amountKrw": 136.34}
write_records(botstore.namuh_store_file(), "bots", [{
    "botId": "SOXL-1", "coin": "SOXL", "broker": "namuh", "units": 41.0, "totalInvested": 3606.01, "entryPrice": 87.9515,
    "turn": 18, "cash": 2390.7446, "params": {"feePct": 0.09}, "tradeHistory": [keep, row]}])
write_records(lf.TRADE_LOG, "trades", [dict(keep), dict(row)])
with contextlib.redirect_stdout(io.StringIO()):
    rc = lf.main(["SOXL", "2026-10-30", "--remove-buy", "--apply"])
r = read_records(botstore.namuh_store_file(), "bots", "")[0]
check("10/30 매수 한 건을 되돌린다 (31주 · 평단 $98.53 · 17회차 · 현금 +$552.00)",
      rc == 0 and r["units"] == 31.0 and abs(r["entryPrice"] - 98.5326) < 1e-3 and r["turn"] == 17
      and abs(r["cash"] - 2942.7409) < 1e-3, f"{r['units']}주 · ${r['entryPrice']:.2f} · T={r['turn']} · ${r['cash']:.2f}")
check("봇 체결 기록 · 체결 일지에서 그 한 건만 지운다",
      [t["id"] for t in r["tradeHistory"]] == ["t-2"] and [t["id"] for t in read_records(lf.TRADE_LOG, "trades", "")] == ["t-2"], "")
with contextlib.redirect_stdout(io.StringIO()):
    rc = lf.main(["SOXL", "2026-10-30", "--remove-buy", "--apply"])
check("두 번 돌려도 또 빼지 않는다 (그날 기록이 없으면 거부)", rc == 1, "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
