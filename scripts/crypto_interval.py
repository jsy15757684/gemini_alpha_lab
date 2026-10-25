#!/usr/bin/env python3
"""돌고 있는 빗썸 봇의 캔들 간격을 바꾼다 (기본 6h → 24h). 장부는 그대로.

2026-10-25 백테스트(1시간봉 400일로 만든 6h · 24h · 시작일 25개): 6시간봉 40분할은 10일 만에 40회를
다 써서 쿼터매도가 약 3.7일마다 되풀이됐다(실계좌 SOL · XRP 도 10/14 → 19 → 21·22 → 24). 24시간봉은
쿼터매도가 8분의 1로 줄고 시작일 평균 손익이 SOL +6~18%p · XRP +12~18%p 나았다.

바꾸는 것은 interval 하나다. 수량 · 평단 · 회차 · 현금 · 실현손익은 그대로 둔다.
재시작하면 그 시점의 일봉을 '이미 본 봉' 으로 잡으므로(중복 매수 방지) 다음 매수는 다음 일봉부터다.

빗썸 서비스(bithumb-bot)가 **멈춰 있을 때만** 쓴다 — 돌고 있으면 서비스가 자기 장부로 이 파일을 다시 덮는다.
기본은 바뀔 내용만 보여 주고, --apply 일 때 원본을 data/bots.json.before-interval-<시각> 으로 남기고 쓴다.

    sudo systemctl stop bithumb-bot
    sudo -u bithumb venv/bin/python scripts/crypto_interval.py SOL XRP              # 미리 보기
    sudo -u bithumb venv/bin/python scripts/crypto_interval.py SOL XRP --apply
    sudo systemctl start bithumb-bot
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import bithumb, botstore                          # noqa: E402
from services.jsonfile import read_records, write_records       # noqa: E402
import namuh_to_excel                                           # noqa: E402

SERVICE = "bithumb-bot"


def main(argv) -> int:
    apply = "--apply" in argv
    interval = "24h"
    args = []
    it = iter(argv)
    for a in it:
        if a == "--interval":
            interval = next(it, "")
        elif not a.startswith("--"):
            args.append(a.upper())
    if not args:
        print(__doc__)
        return 2
    if interval not in bithumb.INTERVALS:
        print(f"❌ 캔들 간격 {interval!r} 은 쓸 수 없습니다 (가능: {', '.join(bithumb.INTERVALS)})")
        return 2
    namuh_to_excel.SERVICE = SERVICE
    if namuh_to_excel.service_active():
        print(f"❌ {SERVICE} 가 돌고 있습니다. 먼저 'sudo systemctl stop {SERVICE}' 로 멈추세요.")
        return 1
    path = botstore.STORE_FILE
    recs = read_records(path, "bots", "빗썸 봇 상태")
    if not recs:
        print(f"❌ {path} 에 봇이 없습니다.")
        return 1
    changed = 0
    for coin in args:
        hits = [r for r in recs if r.get("coin") == coin and botstore.broker_of(r) != "namuh"]
        if len(hits) != 1:
            print(f"❌ {coin} 빗썸 봇이 {len(hits)}개입니다 — 하나일 때만 바꿉니다.")
            return 1
        r = hits[0]
        if r.get("orderHold") or r.get("inflightOrder"):
            print(f"❌ {coin}: 결과를 모르는 주문 · 보내던 주문이 있습니다. 정리된 뒤에 바꾸세요.")
            return 1
        print(f"{coin} ({r.get('botId')}) · {r.get('mode')} · {float(r.get('units') or 0):.4f}개 · "
              f"평단 {float(r.get('entryPrice') or 0):,.0f}원 · {r.get('turn')}회차 · 현금 {float(r.get('cash') or 0):,.0f}원")
        if r.get("interval") == interval:
            print(f"  이미 {interval} 입니다.")
            continue
        print(f"  interval: {r.get('interval')!r} → {interval!r}")
        r["interval"] = interval
        changed += 1
    if not changed:
        return 0
    if not apply:
        print("\n미리 보기입니다. 쓰려면 --apply 를 붙이세요.")
        return 0
    backup = f"{path}.before-interval-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, backup)
    write_records(path, "bots", recs)
    print(f"\n✅ 저장했습니다 (원본: {os.path.basename(backup)}). 'sudo systemctl start {SERVICE}' 로 다시 띄우세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
