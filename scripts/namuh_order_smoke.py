#!/usr/bin/env python3
"""나무증권 매도·취소 경로 실주문 점검 (모의계좌 전용).

라이브 전환 전에 한 번도 불리지 않은 두 경로를 봇이 실제로 쓰는 코드로
태워 본다.

  1) 매도  — NamuhAccount.market_sell (지정가 · 체결 확인)
             봇 장부보다 계좌에 **남는 물량(고아)만** 판다. 봇의 몫은
             건드리지 않는다.
  2) 취소  — 체결되지 않을 가격에 지정가 매수를 걸면, market_buy 가 6초 안에
             체결을 못 보고 _withdraw_unfilled → cancel_order 로 거둬들인다.
             봇이 쓰지 않는 종목(UPRO)으로 해서 돌고 있는 봇과 엉키지 않는다.

안전장치
  - 모의계좌(NAMUH_MOCK=1, 주문 도메인 moapi)가 아니면 주문 없이 멈춘다.
    라이브로 먼저 바꿔 둬도 실제 돈이 나가지 않는다.
  - 미국 정규장이 열려 있지 않으면 주문 없이 멈춘다.

서버에서 서비스 계정으로 돌린다 (키는 .env 에서 읽는다):
  ssh <서버> "cd /opt/gemini_alpha_lab && sudo -u bithumb ./venv/bin/python -" < scripts/namuh_order_smoke.py
  (키는 스크립트가 .env 에서 읽는다 — 명령줄로 넘기면 sudo 가 키를 시스템 로그에 남긴다)
"""

import json
import sys
import time

sys.path.insert(0, "/opt/gemini_alpha_lab")

# 키는 .env 에서 직접 읽는다 — 명령줄로 넘기면 sudo 가 값을 시스템 로그에 남긴다.
from services.envconf import load_dotenv_keys      # noqa: E402
load_dotenv_keys("/opt/gemini_alpha_lab/.env", ("NAMUH_",))

from services import namuh                         # noqa: E402
from services.keystore import namuh_keystore       # noqa: E402

SELL_TICKER = "TQQQ"      # 고아 물량이 있는 종목
CANCEL_TICKER = "UPRO"    # 어떤 봇도 쓰지 않는 종목
CANCEL_PRICE_RATIO = 0.85  # 현재가의 85% — 6초 안에 체결될 수 없는 가격
# 크립토/나무증권을 두 서비스로 나눈 뒤에는 나무증권 봇이 bots_namuh.json 에 있다.
# 한 파일만 읽으면 봇 장부를 0 으로 보고 봇 몫까지 '남는 물량' 으로 판다.
BOTS_FILES = ("/opt/gemini_alpha_lab/data/bots.json", "/opt/gemini_alpha_lab/data/bots_namuh.json")

results = []


def report(name, ok, detail=""):
    results.append((name, ok))
    mark = "✅" if ok else ("⏭️ " if ok is None else "❌")
    print(f"  {mark} {name}  — {detail}", flush=True)


def bot_ledger_units(ticker):
    """나무증권 봇들이 장부에 들고 있는 이 종목 수량의 합 (두 장부 파일 모두).

    같은 봇이 두 파일에 다 있으면(나누다 멈춘 경우) 한 번만 센다.
    파일이 하나라도 있는데 못 읽으면 None — 모르면 팔지 않는다.
    """
    import os
    seen, total, found = set(), 0.0, False
    for path in BOTS_FILES:
        if not os.path.exists(path):
            continue
        found = True
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"  봇 장부를 읽지 못했습니다 ({path}): {e}")
            return None
        bots = d.get("bots", d) if isinstance(d, dict) else d
        for b in bots:
            if b.get("broker") == "namuh" and b.get("coin") == ticker and b.get("botId") not in seen:
                seen.add(b.get("botId"))
                total += float(b.get("units") or 0)
    return total if found else None


def main():
    print("=" * 60)
    print("나무증권 매도·취소 경로 점검 (모의계좌 전용)")
    print("=" * 60)

    # ── 0. 안전장치 ──
    if not namuh.use_mock() or "moapi" not in namuh.trade_base_url():
        print(f"⛔ 모의계좌가 아닙니다 (NAMUH_MOCK={namuh.use_mock()}, "
              f"{namuh.trade_base_url()}). 실제 돈이 나갈 수 있어 주문하지 않고 멈춥니다.")
        return 2
    acc = namuh_keystore.account
    if not acc.configured:
        print("⛔ 나무증권 키가 설정되지 않았습니다.")
        return 2
    ses = namuh.market_session()
    if not ses.get("open"):
        print(f"⛔ 미국 정규장이 열려 있지 않아 주문하지 않습니다 — {ses.get('reason')}")
        return 3
    print(f"모의계좌 {acc.masked_account()} · {namuh.trade_base_url()} · 정규장 {ses.get('etTime')} ET")

    bal0 = acc.get_balance(fresh=True)
    sell_before = float(bal0["qtyByTicker"].get(SELL_TICKER, 0.0))
    cancel_before = float(bal0["qtyByTicker"].get(CANCEL_TICKER, 0.0))
    print(f"시작 잔고: {SELL_TICKER} {sell_before:.0f}주 · {CANCEL_TICKER} {cancel_before:.0f}주 · "
          f"${bal0['usdAvailable']:,.2f}")
    print()

    # ── 1. 매도 ──
    print("── 1. 매도 (/gbstock/order/v1/sell) ──")
    ledger = bot_ledger_units(SELL_TICKER)
    if ledger is None:
        report("매도", None, "봇 장부를 못 읽어 건너뜀 (봇 몫을 팔 위험이 있어서)")
    else:
        spare = sell_before - ledger
        print(f"  계좌 {sell_before:.0f}주 · 봇 장부 {ledger:.0f}주 · 남는 물량 {spare:.0f}주")
        if spare < 1:
            report("매도", None, "남는 물량이 없어 건너뜀 (봇의 몫은 팔지 않는다)")
        else:
            try:
                r = acc.market_sell(SELL_TICKER, 1)
                bal1 = acc.get_balance(fresh=True)
                after = float(bal1["qtyByTicker"].get(SELL_TICKER, 0.0))
                ok = (after == sell_before - 1) and float(r.get("units") or 0) == 1
                report("매도 주문이 체결된다", ok,
                       f"주문번호 {r.get('orderId')} · {r.get('units')}주 @ ${r.get('price')} · "
                       f"잔고 {sell_before:.0f} → {after:.0f}주")
                report("매도 후 계좌가 봇 장부와 맞는다", after == ledger,
                       f"계좌 {after:.0f}주 = 봇 장부 {ledger:.0f}주")
            except namuh.NamuhError as e:
                report("매도 주문이 체결된다", False, e.message)
    print()

    # ── 2. 미체결 자동 취소 ──
    print("── 2. 미체결 지정가 자동 취소 (/gbstock/order/v1/cancel) ──")
    try:
        px = acc.get_price(CANCEL_TICKER)
    except Exception as e:
        report("취소", False, f"{CANCEL_TICKER} 시세를 받지 못함: {e}")
        px = None
    if px:
        limit = round(px * CANCEL_PRICE_RATIO, 2)
        print(f"  {CANCEL_TICKER} 현재가 ${px:,.2f} → 체결 안 될 지정가 ${limit:,.2f} 로 1주 매수")
        placed_msg = ""
        try:
            r = acc.market_buy(CANCEL_TICKER, units=1, order_type=namuh.ORD_LIMIT,
                               limit_price=limit)
            # 여기까지 오면 체결된 것 — 기대와 다르다
            report("미체결 주문을 취소한다", False,
                   f"뜻밖에 체결됨 ({r.get('units')}주 @ ${r.get('price')}) · 수동 확인 필요")
        except namuh.NamuhError as e:
            placed_msg = e.message
            if "취소했습니다" in placed_msg:
                report("미체결 주문을 취소한다", True, placed_msg[:90])
            elif "체결되지 않았습니다" in placed_msg:
                report("미체결 주문을 취소한다", False,
                       f"취소 실패 — 거래소에 주문이 남아 있을 수 있음: {placed_msg[:90]}")
            else:
                report("미체결 주문을 취소한다", False,
                       f"주문 자체가 거부돼 취소 경로를 타지 못함: {placed_msg[:90]}")
        time.sleep(3)
        bal2 = acc.get_balance(fresh=True)
        cancel_after = float(bal2["qtyByTicker"].get(CANCEL_TICKER, 0.0))
        report("취소 후 보유 수량이 그대로다", cancel_after == cancel_before,
               f"{CANCEL_TICKER} {cancel_before:.0f} → {cancel_after:.0f}주")
    print()

    # ── 결과 ──
    passed = sum(1 for _, ok in results if ok is True)
    failed = sum(1 for _, ok in results if ok is False)
    skipped = sum(1 for _, ok in results if ok is None)
    print("=" * 60)
    print(f"통과 {passed} · 실패 {failed} · 건너뜀 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
