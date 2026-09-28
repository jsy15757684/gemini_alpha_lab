#!/usr/bin/env python3
"""나무증권 국내주식 주문 경로 실주문 점검 (모의계좌 전용).

ORB 봇이 쓰는 services/krx.py 를 그대로 태워 1주를 사고 바로 판다.
아직 한 번도 확인하지 못한 세 가지를 본다.

  1) krstock 매수·매도 주문 응답 형식 (접수 번호 필드)
  2) 잔고 행 필드 (수량 · 평단) — 체결 확인과 평단 역산이 이것에 기댄다
  3) 당일 산 주식을 당일에 팔 수 있는가 — 안 되면 ORB 는 성립하지 않는다

안전장치
  - 모의계좌(NAMUH_MOCK=1, 주문 도메인 moapi)가 아니면 주문 없이 멈춘다.
  - 국내 정규장(평일 09:00~15:20)이 아니거나 호가가 멈춰 있으면 멈춘다.
  - 이미 보유 중인 종목이면 멈춘다 (봇 물량과 섞지 않는다).

서버에서 서비스 계정으로 돌린다:
  ssh <서버> "sudo -u bithumb env \\$(grep -E '^NAMUH_' /opt/gemini_alpha_lab/.env | xargs) \\
             /opt/gemini_alpha_lab/venv/bin/python -" < scripts/krx_order_smoke.py
"""

import json
import sys
import time
from datetime import datetime

sys.path.insert(0, "/opt/gemini_alpha_lab")

from services import krx, namuh, orb                  # noqa: E402
from services.keystore import namuh_keystore          # noqa: E402

CODE = "069500"      # KODEX 200 — 유동성이 가장 두텁다
results = []


def report(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else ('⏭️ ' if ok is None else '❌')} {name}  — {detail}", flush=True)


def raw_balance(acc):
    _, b = krx._post(acc, namuh.trade_base_url(), "/krstock/inquiry/v1/balance",
                     {"act_no": acc.account_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
                      "aet_bse": "2", "qut_dit_cd": "UNT"}, read=True)
    return b


def main():
    print("=" * 60)
    print("나무증권 국내주식 주문 경로 점검 (모의계좌 전용)")
    print("=" * 60)
    if not namuh.use_mock() or "moapi" not in namuh.trade_base_url():
        print("⛔ 모의계좌가 아닙니다. 실제 돈이 나갈 수 있어 주문하지 않고 멈춥니다.")
        return 2
    acc = namuh_keystore.account
    if not acc.configured:
        print("⛔ 나무증권 키가 설정되지 않았습니다.")
        return 2
    now = datetime.now(orb.KST)
    m = orb.minutes_since_open(now)
    if now.weekday() >= 5 or not (0 <= m < 380):
        print(f"⛔ 국내 정규장(평일 09:00~15:20)이 아닙니다 — {now:%a %H:%M}")
        return 3
    q = krx.quote(acc, CODE)
    if not orb._hoga_fresh(q["hogaTime"], now):
        print(f"⛔ 호가가 멈춰 있습니다({q['hogaTime']}) — 휴장일일 수 있습니다.")
        return 3
    print(f"모의계좌 {acc.masked_account()} · {q['name']} 현재가 {q['price']:,} · 매수호가 {q['bid']:,} · "
          f"매도호가 {q['ask']:,} · 호가 {q['hogaTime']}")

    bal0 = krx.balance(acc, fresh=True)
    if CODE in bal0["holdings"]:
        print(f"⛔ 이미 {CODE} 를 {bal0['holdings'][CODE]['qty']}주 들고 있습니다. 섞지 않으려고 멈춥니다.")
        return 3
    print(f"시작 잔고: 예수금 {bal0['cash']:,}원 · 보유 종목 {len(bal0['holdings'])}개\n")

    # ── 1. 매수 ──
    print("── 1. 매수 (/krstock/order/v1/cashBuy) ──")
    limit = krx.shift_ticks(q["ask"] or q["price"], q["etf"], 2)
    try:
        no = krx.order(acc, "buy", CODE, 1, limit)
        report("매수 주문이 접수된다", True, f"접수번호 {no} · 1주 @ {limit:,}원 (매도호가 +2호가)")
    except namuh.NamuhError as e:
        report("매수 주문이 접수된다", False, e.message)
        return 1
    r = krx.await_fill(acc, CODE, 0, 1, "buy", wait_sec=10)
    report("매수가 잔고에 잡힌다 (체결 확인)", r["filled"] == 1,
           f"수량 {r['qtyAfter']} · 평단 {r['avgAfter']:,.0f}원")
    b = raw_balance(acc)
    row = next((x for x in b.get("Output_1") or [] if str(x.get("iem_cd", "")).strip() == CODE), None)
    if row:
        keep = {k: row.get(k) for k in ("iem_cd", "iem_nm", "itg_bnc_qty", "rsdl_qty", "ny_stl_qty", "phs_pr", "now_pr", "pft_rt")}
        print(f"  잔고 행 필드: {json.dumps(keep, ensure_ascii=False)}")
        report("잔고 행에 수량·평단 필드가 있다", bool(row.get("phs_pr")) and any(
            float(row.get(k) or 0) >= 1 for k in ("itg_bnc_qty", "rsdl_qty", "ny_stl_qty")), "")
        report("평단이 매수 지정가 이하다 (역산에 쓸 수 있다)", 0 < r["avgAfter"] <= limit,
               f"평단 {r['avgAfter']:,.0f} ≤ 지정가 {limit:,}")
    else:
        report("잔고 행에 수량·평단 필드가 있다", False, "잔고에 종목이 안 보임")
    print()

    # ── 2. 당일 매도 가능 ──
    print("── 2. 당일 매수분 매도 가능 수량 ──")
    try:
        can = krx.sellable(acc, CODE)
        report("당일 산 주식을 바로 팔 수 있다", can >= 1, f"매도 가능 {can}주")
    except namuh.NamuhError as e:
        report("당일 산 주식을 바로 팔 수 있다", None, f"매도가능 조회 실패 — 매도로 직접 확인: {e.message}")
    print()

    # ── 3. 매도 ──
    print("── 3. 매도 (/krstock/order/v1/cashSell) ──")
    held = r["qtyAfter"]
    if held < 1:
        report("매도", None, "보유가 없어 건너뜀")
    else:
        q2 = krx.quote(acc, CODE)
        sl = krx.shift_ticks(q2["bid"] or q2["price"], q2["etf"], -2)
        try:
            no = krx.order(acc, "sell", CODE, held, sl)
            report("매도 주문이 접수된다", True, f"접수번호 {no} · {held}주 @ {sl:,}원 (매수호가 -2호가)")
            r2 = krx.await_fill(acc, CODE, held, held, "sell", wait_sec=10)
            report("매도가 잔고에서 빠진다", r2["filled"] == held and r2["qtyAfter"] == 0,
                   f"남은 수량 {r2['qtyAfter']}")

        except namuh.NamuhError as e:
            report("매도 주문이 접수된다", False, e.message)
    time.sleep(2)
    bal2 = krx.balance(acc, fresh=True)
    print(f"\n끝 잔고: 예수금 {bal2['cash']:,}원 (시작 대비 {bal2['cash'] - bal0['cash']:+,}원) · "
          f"{CODE} {bal2['holdings'].get(CODE, {}).get('qty', 0)}주")

    passed = sum(1 for _, ok in results if ok is True)
    failed = sum(1 for _, ok in results if ok is False)
    print("=" * 60)
    print(f"통과 {passed} · 실패 {failed} · 건너뜀 {sum(1 for _, ok in results if ok is None)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
