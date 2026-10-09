#!/usr/bin/env python3
"""나무증권 봇의 수수료율을 실제 값(namuh.FEE_PCT · 기본 0.09%)으로 고친다.

봇을 만들 때 빗썸 기본값(0.04%)이 들어가 있었다. 장부 현금은 '매수금 × (1 + 수수료)' 를
빼 왔으므로, 지금 들고 있는 물량의 매수금(totalInvested)에 수수료 차이만큼을 현금에서 더 뺀다.
(실현 손익이 있는 지난 사이클은 고치지 않는다 — 있으면 알려만 준다.)

나무증권 서비스가 **멈춰 있을 때만** 쓴다. 기본은 미리 보기, --apply 일 때 쓴다.

    sudo systemctl stop bithumb-namuh
    sudo -u bithumb venv/bin/python scripts/namuh_fee_fix.py            # 미리 보기
    sudo -u bithumb venv/bin/python scripts/namuh_fee_fix.py --apply
    sudo systemctl start bithumb-namuh
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import botstore, namuh                            # noqa: E402
from services.jsonfile import read_records, write_records       # noqa: E402
import namuh_to_excel                                           # noqa: E402


def fix(rec: dict, fee_pct: float):
    """rec 를 고치고 (전 수수료, 현금 전, 현금 후) 를 돌려준다. 고칠 게 없으면 None."""
    p = rec.setdefault("params", {})
    old = float(p.get("feePct", 0.04))
    if abs(old - fee_pct) < 1e-12:
        return None
    cash0 = float(rec.get("cash") or 0.0)
    extra = float(rec.get("totalInvested") or 0.0) * (fee_pct - old) / 100.0
    p["feePct"] = fee_pct
    rec["cash"] = max(0.0, cash0 - extra)
    return old, cash0, rec["cash"]


def main(argv) -> int:
    apply = "--apply" in argv
    path = botstore.namuh_store_file()
    if namuh_to_excel.service_active():
        print(f"❌ {namuh_to_excel.SERVICE} 가 돌고 있습니다. 먼저 'sudo systemctl stop {namuh_to_excel.SERVICE}' 로 멈추세요.")
        return 1
    recs = read_records(path, "bots", "나무증권 봇 상태")
    if not recs:
        print(f"❌ {path} 에 봇이 없습니다.")
        return 1
    busy = [r.get("coin") for r in recs if r.get("orderHold") or r.get("inflightOrder") or r.get("pendingOrders")]
    if busy:
        print(f"❌ 결과를 모르는 주문 · 보내던 주문 · 정산 전 LOC 가 있는 봇이 있습니다 ({', '.join(busy)}). 정리된 뒤에 고치세요.")
        return 1
    changed = 0
    for r in recs:
        if botstore.broker_of(r) != "namuh":
            continue
        res = fix(r, namuh.FEE_PCT)
        if res is None:
            print(f"{r.get('coin')}: 이미 {namuh.FEE_PCT}% — 바꿀 것이 없습니다.")
            continue
        changed += 1
        old, c0, c1 = res
        print(f"{r.get('coin')} ({r.get('botId')}): 수수료 {old}% → {namuh.FEE_PCT}% · "
              f"매수금 ${float(r.get('totalInvested') or 0):,.2f} 기준 현금 ${c0:,.2f} → ${c1:,.2f}")
        if float(r.get("realizedPnl") or 0.0):
            print(f"  ⚠️ 실현 손익 ${float(r['realizedPnl']):,.2f} 은 예전 수수료로 계산된 그대로 둡니다.")
    if not changed:
        return 0
    if not apply:
        print("\n미리 보기입니다. 쓰려면 --apply 를 붙이세요.")
        return 0
    backup = f"{path}.before-fee-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, backup)
    write_records(path, "bots", recs)
    print(f"\n✅ 저장했습니다 (원본: {os.path.basename(backup)}). 'sudo systemctl start {namuh_to_excel.SERVICE}' 로 다시 띄우세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
