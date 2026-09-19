#!/usr/bin/env python3
"""기록한 괴리로 '무전송 양방향이 성립하는가' 에 답한다.

물어야 할 것은 하나다 — **수수료를 빼고도 남는 순간이 얼마나 자주 오는가.**

지금까지의 근거는 스냅샷 1,600관측 중 2회였다. 그 2회로 세 전략 중 구현
난도가 제일 높은 것을 만들 수는 없어서, 먼저 자금 0원으로 분포를 모은다.

    python3 scripts/spread_report.py                 # 전체
    python3 scripts/spread_report.py --days 7        # 최근 7일
    python3 scripts/spread_report.py --min-net 0.05  # 최소 순이익 기준 변경
"""

import os
import csv
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(APP_DIR, "data", "spread_log.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=LOG_FILE)
    ap.add_argument("--days", type=float, default=0.0, help="최근 N일만 (0=전체)")
    ap.add_argument("--min-net", type=float, default=0.0,
                    help="'성립' 으로 볼 최소 순이익 %% (기본 0 = 수수료만 넘으면)")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        print(f"기록이 없습니다: {a.file}\n"
              f"서버가 기동되면 만들어집니다 (APP_SPREAD_RECORDER=0 이면 꺼져 있습니다).")
        raise SystemExit(1)

    cutoff = None
    if a.days > 0:
        cutoff = datetime.now() - timedelta(days=a.days)

    rows = []
    with open(a.file, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if cutoff:
                try:
                    if datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S") < cutoff:
                        continue
                except ValueError:
                    continue
            rows.append(r)

    if not rows:
        print("해당 구간에 기록이 없습니다.")
        raise SystemExit(1)

    ts = [r["ts"] for r in rows]
    span = "?"
    try:
        d0 = datetime.strptime(min(ts), "%Y-%m-%d %H:%M:%S")
        d1 = datetime.strptime(max(ts), "%Y-%m-%d %H:%M:%S")
        span = f"{(d1 - d0).total_seconds() / 86400:.2f}일"
    except ValueError:
        pass

    print(f"\n기록 {len(rows):,}행 · {min(ts)} ~ {max(ts)} ({span})")
    print(f"판정 기준: 왕복 수수료를 뺀 순이익이 {a.min_net:+.3f}% 를 넘는 순간\n")

    by = defaultdict(list)
    for r in rows:
        by[r["coin"]].append(r)

    print(f"{'종목':6} {'관측':>8} {'성립':>7} {'비율':>8} "
          f"{'최대순이익':>10} {'평균(국내매도)':>14} {'체결가기준 평균':>14}")
    print("─" * 74)

    total_hits = 0
    for coin in sorted(by):
        rs = by[coin]
        nets = []
        hits = 0
        for r in rs:
            try:
                nd = float(r["netSellDomPct"])
                nf = float(r["netSellForPct"])
            except (ValueError, KeyError):
                continue
            best = max(nd, nf)
            nets.append((nd, nf, best))
            if best > a.min_net:
                hits += 1
        if not nets:
            continue
        total_hits += hits
        best_max = max(x[2] for x in nets)
        avg_dom = sum(x[0] for x in nets) / len(nets)
        gross = [float(r["grossSpreadPct"]) for r in rs if r.get("grossSpreadPct")]
        avg_gross = sum(gross) / len(gross) if gross else 0.0
        print(f"{coin:6} {len(nets):>8,} {hits:>7,} {hits/len(nets)*100:>7.2f}% "
              f"{best_max:>+10.3f} {avg_dom:>+14.3f} {avg_gross:>+14.3f}")

    print("─" * 74)
    n = sum(len(v) for v in by.values())
    print(f"{'합계':6} {n:>8,} {total_hits:>7,} {total_hits/n*100:>7.2f}%\n")

    if total_hits == 0:
        print("성립한 순간이 한 번도 없습니다. 구현할 근거가 없습니다.")
    else:
        print("성립한 순간의 예시 (최근 10건):")
        shown = 0
        for r in reversed(rows):
            try:
                best = max(float(r["netSellDomPct"]), float(r["netSellForPct"]))
            except (ValueError, KeyError):
                continue
            if best > a.min_net:
                side = ("국내매도·해외매수"
                        if float(r["netSellDomPct"]) >= float(r["netSellForPct"])
                        else "해외매도·국내매수")
                print(f"  {r['ts']}  {r['coin']:5} {side}  순이익 {best:+.3f}%")
                shown += 1
                if shown >= 10:
                    break

    print("\n주의: 순이익은 최우선 호가 1틱 기준입니다. 실제로는 그 호가의 수량만큼만\n"
          "      체결되고, 한쪽만 체결되면 이 전략의 전제(환율 중립)가 깨집니다.")


if __name__ == "__main__":
    main()
