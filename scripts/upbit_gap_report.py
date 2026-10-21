#!/usr/bin/env python3
"""업비트 ↔ 빗썸 괴리 기록(data/upbit_gap/*.csv)을 요약한다 — 국내 양거래소 차익을 만들지 판단용.

종목마다: 관측 수 · 방향별로 순마진이 기준 이상이고 1호가 잔량이 주문 금액 이상이던 비율 ·
빗썸 입출금이 열려 있을 때만 센 횟수 · 양방향이 다 났는지(재고가 저절로 되돌아오는가).

    sudo -u bithumb venv/bin/python scripts/upbit_gap_report.py                # 전체 기간
    sudo -u bithumb venv/bin/python scripts/upbit_gap_report.py --days 7 --net 0.15 --krw 100000
"""

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.upbit_gap_recorder import DATA_DIR       # noqa: E402


def summarize(rows, net_min: float, krw: float):
    s = defaultdict(lambda: {"n": 0, "bb": 0, "bu": 0, "bbOpen": 0, "buOpen": 0, "best": -99.0})
    for r in rows:
        x = s[r["coin"]]
        x["n"] += 1
        opened = r.get("bDeposit") == "1" and r.get("bWithdraw") == "1"
        nb, nu = float(r["netBuyBithumb"]), float(r["netBuyUpbit"])
        x["best"] = max(x["best"], nb, nu)
        if nb >= net_min and float(r["depthBuyBithumbKrw"]) >= krw:
            x["bb"] += 1
            x["bbOpen"] += opened
        if nu >= net_min and float(r["depthBuyUpbitKrw"]) >= krw:
            x["bu"] += 1
            x["buOpen"] += opened
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="최근 며칠만 (0 = 전부)")
    ap.add_argument("--net", type=float, default=0.15, help="순마진 기준 %% (수수료 뺀 뒤)")
    ap.add_argument("--krw", type=float, default=100_000, help="1회 주문 금액 — 1호가 잔량이 이 이상일 때만 센다")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args(argv)
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    if a.days:
        cut = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d")
        files = [f for f in files if os.path.basename(f)[:-4] >= cut]
    if not files:
        print(f"기록이 없습니다 ({DATA_DIR})")
        return 1
    rows = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            rows.extend(csv.DictReader(fh))
    s = summarize(rows, a.net, a.krw)
    print(f"기간 {os.path.basename(files[0])[:-4]} ~ {os.path.basename(files[-1])[:-4]} · 관측 {len(rows):,}행 · "
          f"기준 순마진 {a.net}% · 1호가 {a.krw:,.0f}원 이상")
    print(f"{'종목':<8}{'관측':>7}{'빗썸→업비트':>12}{'업비트→빗썸':>12}{'입출금 열림':>12}{'양방향':>7}{'최고 순마진':>11}")
    order = sorted(s.items(), key=lambda kv: -(kv[1]["bbOpen"] + kv[1]["buOpen"]))
    for c, x in order[:a.top]:
        if not (x["bb"] or x["bu"]):
            continue
        both = "예" if x["bbOpen"] and x["buOpen"] else "-"
        print(f"{c:<8}{x['n']:>7}{x['bb'] / x['n']:>11.1%} {x['bu'] / x['n']:>11.1%} "
              f"{(x['bbOpen'] + x['buOpen']):>11} {both:>6}{x['best']:>+10.2f}%")
    print("\n읽는 법: '입출금 열림' 은 빗썸 입출금이 모두 열려 있을 때 기회 횟수, '양방향' 은 두 방향이 모두 "
          "(입출금 열린 상태로) 났는지. 한 방향만 나는 종목은 재고가 한쪽으로 쏠려 몇 번 뒤 못 한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
