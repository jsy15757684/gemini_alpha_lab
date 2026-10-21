#!/usr/bin/env python3
"""업비트 ↔ 빗썸 괴리 기록기 — 호가 계산 · 날짜 파일 · 오래된 파일 정리 · 요약. 통신 없이 본다."""

import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services import upbit_gap_recorder as g          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


g.DATA_DIR = tempfile.mkdtemp(prefix="gap-test-")
# 업비트 (매수호가, 잔량, 매도호가, 잔량) · 빗썸 같은 순서
r = g.gap_row("2026-10-21 15:00:00", "DOGE", (106.0, 2000.0, 106.1, 3000.0), (104.9, 5000.0, 105.0, 900.0), (1, 1))
check("빗썸 매도호가에 사서 업비트 매수호가에 판다 — 수수료 0.09% 차감",
      abs(r[10] - ((106.0 - 105.0) / 105.0 * 100 - 0.09)) < 1e-3, f"{r[10]}%")
check("반대 방향은 업비트 매도호가에 사서 빗썸 매수호가에 판다", abs(r[11] - ((104.9 - 106.1) / 106.1 * 100 - 0.09)) < 1e-3,
      f"{r[11]}%")
check("1호가 잔량 = 양쪽 중 작은 쪽 (빗썸 매도 900개 × 105원)", r[12] == round(900 * 105.0), f"{r[12]:,}원")
check("입출금 상태를 남긴다 · 모르면 빈칸", r[14:16] == [1, 1]
      and g.gap_row("t", "X", (1, 1, 1, 1), (1, 1, 1, 1), None)[14:16] == ["", ""], "")

g._append("2026-10-21", [r])
g._append("2026-10-21", [r])
with open(g._file_for("2026-10-21"), encoding="utf-8") as f:
    lines = list(csv.reader(f))
check("하루 한 파일 · 머리글은 한 번", lines[0] == g.COLUMNS and len(lines) == 3, f"{len(lines)}줄")
old = g._file_for("2026-08-01")
open(old, "w").close()
g._prune("2026-10-21")
check("보관 기간(45일)이 지난 파일은 지운다", not os.path.exists(old) and os.path.exists(g._file_for("2026-10-21")), "")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import upbit_gap_report as rep                        # noqa: E402
rows = [dict(zip(g.COLUMNS, map(str, x))) for x in (
    g.gap_row("t1", "AAA", (101.0, 10000, 101.1, 10000), (99.9, 10000, 100.0, 10000), (1, 1)),   # 빗썸→업비트 +0.91%
    g.gap_row("t2", "AAA", (99.9, 10000, 100.0, 10000), (101.0, 10000, 101.1, 10000), (1, 1)),   # 업비트→빗썸
    g.gap_row("t1", "BBB", (101.0, 10000, 101.1, 10000), (99.9, 10000, 100.0, 10000), (0, 0)),   # 입출금 막힘
    g.gap_row("t1", "CCC", (101.0, 10, 101.1, 10), (99.9, 10, 100.0, 10), (1, 1)),               # 잔량 부족
)]
s = rep.summarize(rows, 0.15, 100_000)
check("양방향 · 입출금 열림을 센다", s["AAA"]["bbOpen"] == 1 and s["AAA"]["buOpen"] == 1, f"{dict(s['AAA'])}")
check("입출금이 막힌 때는 '열림' 에 넣지 않는다", s["BBB"]["bb"] == 1 and s["BBB"]["bbOpen"] == 0, "")
check("1호가 잔량이 주문 금액보다 적으면 기회로 세지 않는다", s["CCC"]["bb"] == 0, "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
