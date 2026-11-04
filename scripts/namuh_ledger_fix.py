#!/usr/bin/env python3
"""나무증권 봇 장부에서 잘못 적힌 매수 한 건을 되돌린다 (그날 그 종목 BUY_CHUNK 한 건).

2026-10-30 SOXL: 봇의 LOC 매수가 앱에서 취소돼 그날 봇은 한 주도 사지 않았는데, 정산이 체결내역을
찾지 못해 잔고 변화(같은 날 직접 산 순증 10주)를 봇 매수로 적었다 — '10주 @ $55.15'(그날 최저가 $70.23).
이 스크립트는 그 한 건을 장부에서 빼고 수량 · 원가 · 평단 · 회차 · 현금을 그 전으로 되돌린다.
같은 체결이 적힌 체결 일지(data/trades_namuh.json)와 봇의 체결 기록에서도 지운다.

나무증권 서비스(bithumb-namuh)가 **멈춰 있을 때만** 쓴다. 기본은 미리 보기, --apply 일 때 원본을 남기고 쓴다.

    sudo systemctl stop bithumb-namuh
    sudo -u bithumb venv/bin/python scripts/namuh_ledger_fix.py SOXL --remove-buy 2026-10-30            # 미리 보기
    sudo -u bithumb venv/bin/python scripts/namuh_ledger_fix.py SOXL --remove-buy 2026-10-30 --apply
    sudo systemctl start bithumb-namuh
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import botstore                                   # noqa: E402
from services.jsonfile import read_records, write_records       # noqa: E402
import namuh_to_excel                                           # noqa: E402

TRADE_LOG = os.path.join(os.path.dirname(botstore.STORE_FILE), "trades_namuh.json")


def reverse_buy(rec: dict, row: dict) -> dict:
    """rec 에서 매수 row 를 되돌린다. 바뀐 칸 {이름: (전, 후)}."""
    fee = float((rec.get("params") or {}).get("feePct") or 0.0) / 100.0
    units, amount = float(row["units"]), float(row["amountKrw"])
    before = {k: rec.get(k) for k in ("units", "totalInvested", "entryPrice", "turn", "cash")}
    rec["units"] = round(float(rec["units"]) - units, 8)
    rec["totalInvested"] = round(float(rec["totalInvested"]) - amount, 6)
    rec["entryPrice"] = rec["totalInvested"] / rec["units"] if rec["units"] > 0 else 0.0
    rec["turn"] = max(0, int(rec["turn"]) - 1)
    rec["cash"] = round(float(rec["cash"]) + amount * (1 + fee), 6)
    rec["tradeHistory"] = [t for t in (rec.get("tradeHistory") or []) if t.get("id") != row.get("id")]
    return {k: (before[k], rec[k]) for k in before}


def main(argv) -> int:
    apply = "--apply" in argv
    args = [a for a in argv if not a.startswith("--")]
    if "--remove-buy" not in argv or len(args) != 2:
        print(__doc__)
        return 2
    coin, date = args[0].upper(), args[1]
    if namuh_to_excel.service_active():
        print(f"❌ {namuh_to_excel.SERVICE} 가 돌고 있습니다. 먼저 'sudo systemctl stop {namuh_to_excel.SERVICE}' 로 멈추세요.")
        return 1
    path = botstore.namuh_store_file()
    recs = read_records(path, "bots", "나무증권 봇 상태") or []
    hits = [r for r in recs if r.get("coin") == coin and botstore.broker_of(r) == "namuh"]
    if len(hits) != 1:
        print(f"❌ {coin} 봇이 {len(hits)}개입니다 — 하나일 때만 고칩니다.")
        return 1
    rec = hits[0]
    if rec.get("orderHold") or rec.get("inflightOrder") or rec.get("pendingOrders") or rec.get("sellOrder"):
        print("❌ 결과를 모르는 주문 · 정산 전 LOC · 걸어 둔 매도가 있습니다. 장 마감 뒤 정리된 다음에 고치세요.")
        return 1
    rows = [t for t in (rec.get("tradeHistory") or [])
            if t.get("action") == "BUY_CHUNK" and str(t.get("time", "")).startswith(date)]
    if len(rows) != 1:
        print(f"❌ {coin} {date} 매수 기록이 {len(rows)}건입니다 — 한 건일 때만 되돌립니다.")
        return 1
    row = rows[0]
    print(f"{coin} ({rec.get('botId')}) {date} 매수 기록: {row['units']:g}주 @ ${row['price']:,.2f} "
          f"(${row['amountKrw']:,.2f}) — {str(row.get('reason', ''))[:40]}")
    for k, (a, b) in reverse_buy(rec, row).items():
        fa = f"{a:,.4f}" if isinstance(a, float) else a
        fb = f"{b:,.4f}" if isinstance(b, float) else b
        print(f"  {k}: {fa} → {fb}")
    log = read_records(TRADE_LOG, "trades", "체결 일지") or []
    log2 = [t for t in log if t.get("id") != row.get("id")]
    print(f"  체결 일지: {len(log) - len(log2)}건 삭제 ({os.path.basename(TRADE_LOG)})")
    if not apply:
        print("\n미리 보기입니다. 쓰려면 --apply 를 붙이세요.")
        return 0
    stamp = time.strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, f"{path}.before-fix-{stamp}")
    if os.path.exists(TRADE_LOG):
        shutil.copy2(TRADE_LOG, f"{TRADE_LOG}.before-fix-{stamp}")
    write_records(path, "bots", recs)
    if len(log2) != len(log):
        write_records(TRADE_LOG, "trades", log2)
    print(f"\n✅ 저장했습니다 (원본: *.before-fix-{stamp}). 'sudo systemctl start {namuh_to_excel.SERVICE}' 로 다시 띄우세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
