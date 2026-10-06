#!/usr/bin/env python3
"""나무증권 **실계좌** 소액 시험 — 모의계좌로는 확인할 수 없는 경로를 1주씩 태워 본다.

    실제 돈이 나간다. 단계마다 따로 실행하고, 주문 직전에 터미널에서 직접 입력해
    승인해야 주문이 나간다(파이프 · 자동 입력으로는 진행되지 않는다).

  단계                  하는 일                                              주문
  check                 계좌 종류(실전) · 잔고 · 장 상태 · 시험 종목 보유 확인  없음
  buy-sell              1주 지정가 매수 → 체결 확인 → 1주 매도                  2건
  cancel                현재가 85% 지정가 1주 매수 → 안 붙음 → 자동 취소 확인   1건 (+취소)
  loc                   LOC 1주 매수 접수 (현재가 +3% 한도 → 마감가에 붙는다)   1건
  loc-check             (장 마감 뒤) LOC 가 1주 체결됐는지 잔고로 확인          없음
  sell                  시험 종목 1주 매도 (loc 로 산 것을 다음 정규장에 정리)  1건
  krw                   원화 증거금으로 1주 매수 → 1주 매도 (통합증거금)        2건

안전장치 (하나라도 걸리면 주문하지 않는다)
  - 실계좌 모드(NAMUH_MOCK=0, 주문 도메인이 moapi 가 아님)이고, 나무증권이 이 키의
    계좌를 '실전' 으로 확인해 줘야 한다 (확인을 못 받아도 멈춘다).
  - 시험 종목(기본 UPRO)은 어떤 봇 장부에도 없어야 한다 — 봇 물량과 섞지 않는다.
  - 시험 전 계좌의 시험 종목 보유가 예상과 같아야 한다 (0주, 또는 loc 뒤의 1주).
  - 1주만, 1주 값 $400 이하만.
  - 터미널(tty)에서 실행해야 하고, 종목 이름을 직접 입력해 승인해야 한다.
  - 주문 결과를 모르게 되면(응답 끊김 등) 그 자리에서 멈추고 앱에서 확인하라고 알린다.

서버에서 서비스 계정으로, 터미널을 붙여서(ssh -t) 실행한다:
  ssh -t <서버>
  cd /opt/gemini_alpha_lab
  sudo -u bithumb env $(grep -E '^NAMUH_' .env | xargs) venv/bin/python scripts/namuh_live_smoke.py check

결과는 화면과 data/live_smoke_log.jsonl 에 남는다. loc 단계의 기준 수량은
data/live_smoke_state.json 에 둔다 (다음 날 loc-check 가 읽는다).
"""

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.getenv("GAL_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import namuh                          # noqa: E402
from services.keystore import namuh_keystore        # noqa: E402

TICKER = (os.getenv("SMOKE_TICKER") or "UPRO").upper().strip()
MAX_PRICE = 400.0
CANCEL_RATIO = 0.85
LOC_CAP_RATIO = 1.03
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "live_smoke_state.json")
LOG_FILE = os.path.join(DATA, "live_smoke_log.jsonl")
BOTS_FILES = (os.path.join(DATA, "bots.json"), os.path.join(DATA, "bots_namuh.json"))
ET = ZoneInfo("America/New_York")


def now_et():
    return datetime.now(ET)


def say(msg=""):
    print(msg, flush=True)


def log(step, ok, detail, **extra):
    row = {"at": datetime.now().isoformat(timespec="seconds"), "step": step, "ticker": TICKER,
           "ok": ok, "detail": detail, **extra}
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        say(f"  (기록 파일에 쓰지 못함: {e})")
    mark = "✅" if ok else ("⏭️ " if ok is None else "❌")
    say(f"  {mark} {step} — {detail}")


def stop(msg, code=2):
    say(f"⛔ {msg}")
    sys.exit(code)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(d):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def bot_ledger_units(ticker):
    """봇 장부에 이 종목이 얼마 있는가. 못 읽으면 None (그러면 주문하지 않는다)."""
    total = 0.0
    try:
        for p in BOTS_FILES:
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            for b in (d.get("bots", d) if isinstance(d, dict) else d):
                if str(b.get("coin", "")).upper() == ticker:
                    total += float(b.get("units") or 0)
                    if b.get("isRunning", True):
                        total += 1e-9           # 돌고 있는 봇이 있다는 표시 (0주여도)
    except (OSError, ValueError):
        return None
    return total


def confirm(lines):
    """주문 직전 확인. 터미널에서 종목 이름을 직접 쳐야 진행한다."""
    if not sys.stdin.isatty():
        stop("터미널에서 직접 실행해야 합니다 (ssh -t). 파이프 · 자동 입력으로는 주문하지 않습니다.")
    say("")
    say("┌─ 실계좌 주문 확인 " + "─" * 40)
    for ln in lines:
        say(f"│ {ln}")
    say("└" + "─" * 58)
    try:
        typed = input(f"진행하려면 종목 이름 {TICKER} 를 그대로 입력하세요 (그 밖의 입력은 취소): ").strip()
    except EOFError:
        typed = ""
    if typed != TICKER:
        stop("취소했습니다. 주문을 내지 않았습니다.", 0)


def preflight(order_step: bool):
    """모든 단계 공통 확인. 실패하면 멈춘다."""
    if namuh.use_mock() or "moapi" in namuh.trade_base_url():
        stop(f"모의계좌 모드입니다 (NAMUH_MOCK={os.getenv('NAMUH_MOCK')}, {namuh.trade_base_url()}). "
             f"이 스크립트는 실계좌 전용입니다 — 모의계좌 점검은 scripts/namuh_order_smoke.py 를 쓰세요.")
    acc = namuh_keystore.account
    if not acc.configured:
        stop("나무증권 키가 설정되지 않았습니다.")
    at = acc.check_account_type()
    if not at.get("verified"):
        stop(f"계좌 종류를 확인하지 못했습니다 — 확인 없이는 주문하지 않습니다: {at.get('message')}")
    if not at.get("ok"):
        stop(f"이 키의 계좌가 실전 계좌로 확인되지 않습니다: {at.get('message')}")
    say(f"실계좌 {acc.masked_account()} · {namuh.trade_base_url()} · 계좌 종류 확인됨 ({at.get('type')})")
    ses = namuh.market_session()
    say(f"미국 정규장: {'열림' if ses.get('open') else '닫힘'} · {ses.get('etTime')} ET"
        + ("" if ses.get("open") else f" · {ses.get('reason')}"))
    led = bot_ledger_units(TICKER)
    if led is None:
        stop("봇 장부를 읽지 못했습니다 — 봇 물량과 섞일 위험이 있어 멈춥니다.")
    if led > 0:
        stop(f"{TICKER} 를 쓰는 봇이 있습니다 (장부 {led:g}주) — 다른 종목으로 시험하세요 (SMOKE_TICKER=...).")
    if order_step and not ses.get("open"):
        stop("정규장이 닫혀 있어 주문하지 않습니다.", 3)
    return acc, ses


def balance(acc):
    """잔고 + 실제 주문 가능 금액(결제 전 매도 대금 · 원화 증거금 포함 — 예수금이 아니다)."""
    b = acc.get_balance(fresh=True)
    if hasattr(acc, "with_orderable"):
        b = acc.with_orderable(b, TICKER)
    return b, float(b.get("qtyByTicker", {}).get(TICKER, 0.0))


def price_of(acc):
    px = float(acc.get_price(TICKER))
    if not (0 < px <= MAX_PRICE):
        stop(f"{TICKER} 1주 값 ${px:,.2f} 가 시험 한도(${MAX_PRICE:,.0f})를 벗어납니다.")
    return px


def expect_qty(have, want, step):
    if abs(have - want) > 1e-9:
        stop(f"{TICKER} 계좌 보유가 {have:g}주입니다 (이 단계는 {want:g}주에서 시작해야 합니다) — {step} 을 하지 않습니다. "
             f"나무증권 앱에서 확인하세요.")


def unknown(step, e):
    log(step, False, f"결과를 모르는 주문 — {e.message}")
    stop("주문 결과를 모릅니다. 더 진행하지 말고 나무증권 앱에서 체결 · 미체결 내역을 확인하세요.", 4)


# ── 단계 ──
def step_check():
    acc, ses = preflight(order_step=False)
    b, q = balance(acc)
    say(f"잔고: 주문 가능 ${float(b.get('usdAvailable') or 0):,.2f} (달러 예수금 ${float(b.get('usdDeposit') or 0):,.2f}) · "
        f"원화 증거금 주문 가능 {float(b.get('krwDeposit') or 0):,.0f}원 · {TICKER} {q:g}주")
    held = {k: v for k, v in (b.get("qtyByTicker") or {}).items() if v}
    say(f"보유 종목: {held or '없음'}")
    try:
        px = acc.get_price(TICKER)
        say(f"{TICKER} 현재가 ${px:,.2f} (시험 한도 ${MAX_PRICE:,.0f})")
    except Exception as e:
        say(f"{TICKER} 시세를 받지 못했습니다: {e}")
    log("check", True, f"실계좌 확인 · {TICKER} {q:g}주 · 정규장 {'열림' if ses.get('open') else '닫힘'}")


def step_buy_sell():
    acc, _ = preflight(order_step=True)
    b0, q0 = balance(acc)
    expect_qty(q0, 0, "buy-sell")
    px = price_of(acc)
    limit = round(px * (1 + namuh.LIMIT_SLIP_PCT / 100), 2)
    confirm([f"{TICKER} 1주 지정가 매수 ${limit:,.2f} (현재가 ${px:,.2f} +{namuh.LIMIT_SLIP_PCT}%)",
             f"체결되면 바로 1주 지정가 매도 (현재가 -{namuh.LIMIT_SLIP_PCT}%)",
             f"예상 비용: 호가 차이 + 수수료 · 증거금 통화는 설정({namuh.MARGIN_PREF})대로",
             f"주문 가능 ${float(b0.get('usdAvailable') or 0):,.2f}"])
    try:
        r = acc.market_buy(TICKER, units=1)
    except namuh.OrderUnknown as e:
        unknown("매수", e)
    except namuh.NamuhError as e:
        log("매수", False, e.message)
        return
    _, q1 = balance(acc)
    log("매수", r.get("units") == 1 and q1 == q0 + 1,
        f"주문번호 {r.get('orderId')} · {r.get('units')}주 @ 한도 ${r.get('price')} · 잔고 {q0:g} → {q1:g}주")
    if q1 < 1:
        stop("매수가 잔고에 보이지 않아 매도하지 않습니다.")
    try:
        s = acc.market_sell(TICKER, 1)
    except namuh.OrderUnknown as e:
        unknown("매도", e)
    except namuh.NamuhError as e:
        log("매도", False, e.message + f" — {TICKER} 1주가 남았습니다. 'sell' 단계로 정리하세요.")
        return
    b2, q2 = balance(acc)
    log("매도", s.get("units") == 1 and q2 == q0,
        f"주문번호 {s.get('orderId')} · {s.get('units')}주 @ 한도 ${s.get('price')} · 잔고 {q1:g} → {q2:g}주 · "
        f"주문 가능 ${float(b0.get('usdAvailable') or 0):,.2f} → ${float(b2.get('usdAvailable') or 0):,.2f}")


def step_cancel():
    acc, _ = preflight(order_step=True)
    _, q0 = balance(acc)
    expect_qty(q0, 0, "cancel")
    px = price_of(acc)
    limit = round(px * CANCEL_RATIO, 2)
    confirm([f"{TICKER} 1주 지정가 매수 ${limit:,.2f} (현재가 ${px:,.2f} 의 {CANCEL_RATIO:.0%} — 체결되지 않을 값)",
             "6초 안에 안 붙으면 코드가 스스로 취소하는지 본다"])
    try:
        acc.market_buy(TICKER, units=1, order_type=namuh.ORD_LIMIT, limit_price=limit)
        _, q1 = balance(acc)
        log("미체결 취소", False, f"체결됐습니다 (예상 밖) — 잔고 {q0:g} → {q1:g}주. 'sell' 단계로 정리하세요.")
    except namuh.OrderUnknown as e:
        unknown("미체결 취소", e)
    except namuh.NamuhError as e:
        _, q1 = balance(acc)
        ok = "체결되지 않았습니다" in e.message and "취소했습니다" in e.message and q1 == q0
        log("미체결 취소", ok, f"{e.message} · 잔고 {q0:g} → {q1:g}주")


def step_loc():
    acc, ses = preflight(order_step=True)
    t_et = now_et()
    if (t_et.hour, t_et.minute) >= (15, 45):
        stop("15:45 ET 이 지났습니다 — 거래소가 15:50 이후 LOC 를 받지 않아 시험하지 않습니다.", 3)
    st = load_state()
    if st.get("loc") and not st["loc"].get("checked"):
        stop(f"아직 확인하지 않은 LOC 시험이 있습니다 ({st['loc'].get('at')}) — 먼저 loc-check 를 하세요.")
    _, q0 = balance(acc)
    expect_qty(q0, 0, "loc")
    px = price_of(acc)
    cap = round(px * LOC_CAP_RATIO, 2)
    confirm([f"{TICKER} 1주 LOC(장마감 지정가) 매수 · 한도 ${cap:,.2f} (현재가 ${px:,.2f} +3%)",
             "마감 동시호가 가격이 한도 이하면 마감가에 1주 체결된다",
             "체결 확인은 장 마감 뒤 'loc-check' · 정리는 다음 정규장에 'sell'"])
    try:
        r = acc.market_buy(TICKER, units=1, order_type=namuh.ORD_LOC, limit_price=cap, await_fill=False)
    except namuh.OrderUnknown as e:
        unknown("LOC 접수", e)
    except namuh.NamuhError as e:
        log("LOC 접수", False, e.message)
        return
    st["loc"] = {"at": datetime.now().isoformat(timespec="seconds"), "etDate": t_et.strftime("%Y-%m-%d"),
                 "orderId": r.get("orderId"), "cap": cap, "qtyBefore": q0, "checked": False}
    save_state(st)
    log("LOC 접수", r.get("status") == "ACCEPTED" and bool(r.get("orderId")),
        f"주문번호 {r.get('orderId')} · 한도 ${cap:,.2f} · 장 마감 뒤 loc-check 를 실행하세요")


def step_loc_check():
    acc, ses = preflight(order_step=False)
    st = load_state()
    loc = st.get("loc")
    if not loc:
        stop("기록된 LOC 시험이 없습니다 — 먼저 loc 단계를 하세요.")
    if ses.get("open") and now_et().strftime("%Y-%m-%d") == loc.get("etDate"):
        stop("LOC 를 낸 정규장이 아직 끝나지 않았습니다 — 마감(16:00 ET) 뒤에 확인하세요.", 3)
    b, q = balance(acc)
    moved = q - float(loc.get("qtyBefore") or 0)
    ok = abs(moved - 1) < 1e-9
    loc.update(checked=True, filled=moved, checkedAt=datetime.now().isoformat(timespec="seconds"))
    save_state(st)
    log("LOC 정산", ok, f"주문번호 {loc.get('orderId')} · 잔고 {loc.get('qtyBefore'):g} → {q:g}주"
        + (" · 마감가에 1주 체결 — 다음 정규장에 'sell' 로 정리하세요" if ok
           else " · 체결되지 않았거나 수량이 다릅니다 — 나무증권 앱의 체결 내역을 확인하세요"))


def step_sell():
    acc, _ = preflight(order_step=True)
    _, q0 = balance(acc)
    if q0 < 1:
        stop(f"{TICKER} 계좌 보유가 {q0:g}주라 팔 것이 없습니다.")
    if q0 > 1:
        stop(f"{TICKER} 계좌 보유가 {q0:g}주입니다 — 시험으로 산 1주보다 많아, 무엇을 파는지 확실하지 않아 멈춥니다.")
    px = price_of(acc)
    confirm([f"{TICKER} 1주 지정가 매도 (현재가 ${px:,.2f} -{namuh.LIMIT_SLIP_PCT}%) — 시험으로 산 1주 정리"])
    try:
        s = acc.market_sell(TICKER, 1)
    except namuh.OrderUnknown as e:
        unknown("매도", e)
    except namuh.NamuhError as e:
        log("매도", False, e.message)
        return
    _, q1 = balance(acc)
    log("매도", s.get("units") == 1 and q1 == q0 - 1,
        f"주문번호 {s.get('orderId')} · {s.get('units')}주 @ 한도 ${s.get('price')} · 잔고 {q0:g} → {q1:g}주")


def step_krw():
    acc, _ = preflight(order_step=True)
    b0, q0 = balance(acc)
    expect_qty(q0, 0, "krw")
    px = price_of(acc)
    from services import fx as fxmod
    rate, why = fxmod.get_official_fx_rate()
    if not rate:
        stop(f"환율을 받지 못해 원화로 1주를 살 수 있는지 판단할 수 없습니다: {why}")
    need_krw = px * rate * 1.05
    krw = float(b0.get("krwDeposit") or 0)
    if krw < need_krw:
        stop(f"원화 증거금 주문 가능 금액 {krw:,.0f}원이 1주 값(약 {need_krw:,.0f}원)보다 적습니다 — 원화를 넣은 뒤 시험하세요.")
    confirm([f"{TICKER} 1주 지정가 매수 · 증거금 통화 = 원화(통합증거금) · 원화 주문 가능 {krw:,.0f}원",
             f"현재가 ${px:,.2f} · 체결되면 바로 1주 매도",
             "통합증거금(원화 주문) 신청이 안 돼 있으면 나무증권이 거부한다 — 그것도 확인 결과다"])
    namuh.MARGIN_PREF = "krw"
    try:
        r = acc.market_buy(TICKER, units=1)
    except namuh.OrderUnknown as e:
        unknown("원화 매수", e)
    except namuh.NamuhError as e:
        log("원화 매수", False, e.message)
        return
    b1, q1 = balance(acc)
    log("원화 매수", r.get("units") == 1 and q1 == q0 + 1,
        f"주문번호 {r.get('orderId')} · 잔고 {q0:g} → {q1:g}주 · 원화 예수금 {krw:,.0f} → "
        f"{float(b1.get('krwDeposit') or 0):,.0f}원")
    if q1 < 1:
        stop("매수가 잔고에 보이지 않아 매도하지 않습니다.")
    try:
        s = acc.market_sell(TICKER, 1)
    except namuh.OrderUnknown as e:
        unknown("매도", e)
    except namuh.NamuhError as e:
        log("매도", False, e.message + f" — {TICKER} 1주가 남았습니다. 'sell' 단계로 정리하세요.")
        return
    _, q2 = balance(acc)
    log("매도", s.get("units") == 1 and q2 == q0, f"주문번호 {s.get('orderId')} · 잔고 {q1:g} → {q2:g}주 "
                                                 f"(매도 대금은 달러로 들어온다)")


STEPS = {"check": step_check, "buy-sell": step_buy_sell, "cancel": step_cancel, "loc": step_loc,
         "loc-check": step_loc_check, "sell": step_sell, "krw": step_krw}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        say(__doc__)
        say("단계: " + " · ".join(STEPS))
        return 1
    say(f"── 나무증권 실계좌 시험 · {sys.argv[1]} · {TICKER} · {now_et():%Y-%m-%d %H:%M} ET ──")
    STEPS[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
