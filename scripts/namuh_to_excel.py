#!/usr/bin/env python3
"""돌고 있는 나무증권 봇을 '무매법 엑셀 방식'(locMode=excel)으로 옮긴다.

장부(보유 수량 · 평단 · 회차 · 현금)는 그대로 두고 바꾸는 것은 이것뿐이다.
  · params.locMode      → "excel"
  · params.useMacroGear → False,  params.raoerUseAi → False
  · budgetCarryover     → 0       (엑셀은 1회분할금이 고정이라 이월이 없다)
  · locSession          → 비움    (오늘 LOC 를 새 방식으로 걸 수 있게)

나무증권 서비스(bithumb-namuh)가 **멈춰 있을 때만** 쓴다 — 돌고 있으면 서비스가
자기 장부로 이 파일을 다시 덮는다. 기본은 바뀔 내용만 보여 주고, --apply 일 때 쓴다.
쓰기 전에 원래 파일을 data/bots_namuh.json.before-excel-<시각> 으로 남긴다.

    sudo systemctl stop bithumb-namuh
    sudo -u bithumb venv/bin/python scripts/namuh_to_excel.py TQQQ            # 미리 보기
    sudo -u bithumb venv/bin/python scripts/namuh_to_excel.py TQQQ --apply
    sudo systemctl start bithumb-namuh
"""

import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import botstore                                   # noqa: E402
from services.jsonfile import read_records, write_records       # noqa: E402

SERVICE = "bithumb-namuh"


def service_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False                   # systemd 가 없는 곳(맥 · 시험)
    return r.stdout.strip() in ("active", "activating", "reloading")


def migrate(rec: dict) -> list:
    """rec 를 고치고 바뀐 칸을 [(이름, 전, 후)] 로 돌려준다."""
    p = rec.setdefault("params", {})
    changes = []
    for key, new in (("locMode", "excel"), ("useMacroGear", False), ("raoerUseAi", False)):
        if p.get(key) != new:
            changes.append((f"params.{key}", p.get(key), new))
            p[key] = new
    for key, new in (("budgetCarryover", 0.0), ("locSession", None)):
        if rec.get(key) != new:
            changes.append((key, rec.get(key), new))
            rec[key] = new
    return changes


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    apply = "--apply" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    coin = args[0].upper()
    path = botstore.namuh_store_file()
    if service_active():
        print(f"❌ {SERVICE} 가 돌고 있습니다. 먼저 'sudo systemctl stop {SERVICE}' 로 멈추세요.")
        return 1
    recs = read_records(path, "bots", "나무증권 봇 상태")
    if not recs:
        print(f"❌ {path} 에 봇이 없습니다.")
        return 1
    hits = [r for r in recs if r.get("coin") == coin and botstore.broker_of(r) == "namuh"]
    if len(hits) != 1:
        print(f"❌ {coin} 봇이 {len(hits)}개입니다 — 하나일 때만 옮깁니다.")
        return 1
    rec = hits[0]
    if rec.get("orderHold") or rec.get("inflightOrder") or rec.get("pendingOrders"):
        print("❌ 결과를 모르는 주문 · 보내던 주문 · 정산 전 LOC 가 있습니다. 정리된 뒤에 옮기세요.")
        return 1
    print(f"{coin} ({rec.get('botId')}) · {rec.get('mode')} · 운용자본 ${float(rec.get('initialKrw') or 0):,.2f}")
    print(f"  장부 그대로: {float(rec.get('units') or 0):g}주 · 평단 ${float(rec.get('entryPrice') or 0):,.2f} · "
          f"{rec.get('turn')}회차 · 현금 ${float(rec.get('cash') or 0):,.2f}")
    changes = migrate(rec)
    if not changes:
        print("  이미 엑셀 방식입니다 — 바꿀 것이 없습니다.")
        return 0
    for name, old, new in changes:
        print(f"  {name}: {old!r} → {new!r}")
    if not apply:
        print("\n미리 보기입니다. 쓰려면 --apply 를 붙이세요.")
        return 0
    backup = f"{path}.before-excel-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, backup)
    write_records(path, "bots", recs)
    print(f"\n✅ 저장했습니다 (원본: {os.path.basename(backup)}). 'sudo systemctl start {SERVICE}' 로 다시 띄우세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
