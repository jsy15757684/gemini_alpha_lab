#!/usr/bin/env python3
"""scripts/namuh_live_smoke.py 의 안전장치 — 가짜 계좌로 본다 (나무증권에 아무것도 보내지 않는다).

실계좌 시험 스크립트는 실제 돈을 쓴다. 막아야 할 때 막는지, 승인 없이 주문하지 않는지,
결과를 모르면 거기서 멈추는지를 본다.
"""

import builtins
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import namuh_live_smoke as sm                      # noqa: E402
from services.namuh import NamuhError, OrderUnknown  # noqa: E402

PASS, FAIL = [], []
TMP = tempfile.mkdtemp(prefix="live-smoke-")
sm.STATE_FILE = os.path.join(TMP, "state.json")
sm.LOG_FILE = os.path.join(TMP, "log.jsonl")
sm.BOTS_FILES = (os.path.join(TMP, "bots_namuh.json"),)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


class Fake:
    configured = True

    def __init__(self, qty=0.0, price=150.0, acct_ok=True, verified=True, buy_err=None, sell_err=None):
        self.qty, self.px, self.orders = qty, price, []
        self.at = {"verified": verified, "ok": acct_ok, "type": "01", "message": "시험"}
        self.buy_err, self.sell_err = buy_err, sell_err

    def masked_account(self):
        return "500****0000"

    def check_account_type(self):
        return self.at

    def get_balance(self, fresh=False):
        return {"qtyByTicker": {"UPRO": self.qty}, "usdAvailable": 10_000.0, "krwDeposit": 5_000_000.0}

    def get_price(self, sym):
        return self.px

    def market_buy(self, sym, units=0, order_type="", limit_price=0.0, await_fill=True, **k):
        self.orders.append(("buy", order_type, limit_price))
        if self.buy_err:
            raise self.buy_err
        if order_type == sm.namuh.ORD_LOC:
            return {"orderId": "777", "status": "ACCEPTED", "units": 0}
        self.qty += 1
        return {"orderId": "101", "units": 1.0, "price": round(self.px * 1.005, 2), "status": "FILLED"}

    def market_sell(self, sym, units, **k):
        self.orders.append(("sell",))
        if self.sell_err:
            raise self.sell_err
        self.qty -= 1
        return {"orderId": "102", "units": 1.0, "price": round(self.px * 0.995, 2), "status": "FILLED"}


def setup(fake, mock=False, open_=True, tty=True, typed="UPRO", bots=None, et=(10, 0)):
    sm.namuh.use_mock = lambda: mock
    sm.namuh.trade_base_url = lambda: "https://moapi.nhplug.com:8443" if mock else "https://api.nhplug.com:8443"
    sm.namuh_keystore.account = fake
    sm.namuh.market_session = lambda now_utc=None: {"open": open_, "etTime": "10:00", "reason": "시험"}
    sm.sys.stdin.isatty = lambda: tty
    builtins.input = lambda prompt="": typed
    sm.now_et = lambda: datetime(2026, 10, 7, et[0], et[1], tzinfo=sm.ET)
    with open(sm.BOTS_FILES[0], "w", encoding="utf-8") as f:
        json.dump({"bots": bots or []}, f)
    sm.namuh.MARGIN_PREF = "auto"


def run(step):
    try:
        sm.STEPS[step]()
        return None
    except SystemExit as e:
        return e.code


print("── 실계좌가 아니면 멈춘다 ──")
f = Fake(); setup(f, mock=True)
check("모의계좌 모드면 어느 단계도 하지 않는다", run("buy-sell") == 2 and f.orders == [], "")
f = Fake(verified=False); setup(f)
check("계좌 종류를 확인하지 못하면 주문하지 않는다", run("buy-sell") == 2 and f.orders == [], "")
f = Fake(acct_ok=False); setup(f)
check("실전 계좌로 확인되지 않으면 주문하지 않는다", run("buy-sell") == 2 and f.orders == [], "")

print("── 봇 · 계좌 상태 ──")
f = Fake(); setup(f, bots=[{"coin": "UPRO", "units": 0, "isRunning": True}])
check("시험 종목을 쓰는 봇이 있으면 (0주라도) 멈춘다", run("buy-sell") == 2 and f.orders == [], "")
f = Fake(qty=3); setup(f)
check("시험 전 시험 종목 보유가 0주가 아니면 멈춘다", run("buy-sell") == 2 and f.orders == [], "")
f = Fake(price=480.0); setup(f)
check("1주 값이 $400 를 넘으면 멈춘다", run("buy-sell") == 2 and f.orders == [], "")
f = Fake(); setup(f, open_=False)
check("정규장이 닫혀 있으면 주문 단계를 하지 않는다", run("buy-sell") == 3 and f.orders == [], "")
f = Fake(); setup(f, open_=False)
check("check 단계는 장이 닫혀도 읽기만 한다", run("check") is None and f.orders == [], "")

print("── 승인 ──")
f = Fake(); setup(f, tty=False)
check("터미널이 아니면(파이프 · 자동 입력) 주문하지 않는다", run("buy-sell") == 2 and f.orders == [], "")
f = Fake(); setup(f, typed="y")
check("종목 이름을 그대로 치지 않으면 취소 · 주문 없음", run("buy-sell") == 0 and f.orders == [], "")

print("── 단계 ──")
f = Fake(); setup(f)
check("buy-sell: 승인하면 1주 사고 1주 판다", run("buy-sell") is None and [o[0] for o in f.orders] == ["buy", "sell"]
      and f.qty == 0, f"{f.orders}")
f = Fake(buy_err=OrderUnknown("응답 끊김 (시험)", side="buy", ticker="UPRO", qty=1, price=150, qty_before=0)); setup(f)
check("매수 결과를 모르면 거기서 멈추고 팔지 않는다", run("buy-sell") == 4 and [o[0] for o in f.orders] == ["buy"], "")
f = Fake(sell_err=NamuhError("매도 거부 (시험)")); setup(f)
check("매도가 실패하면 1주가 남았다고 알리고 끝낸다", run("buy-sell") is None and f.qty == 1, "")
rows = [json.loads(x) for x in open(sm.LOG_FILE, encoding="utf-8")]
check("결과를 기록 파일에 남긴다", any(r["step"] == "매도" and not r["ok"] and "sell" in r["detail"] for r in rows), "")

f = Fake(buy_err=NamuhError("매수 주문(548597)이 체결되지 않았습니다 — 지정가 $127.50. 장부를 바꾸지 않습니다. "
                            "남은 미체결 주문은 취소했습니다.")); setup(f)
check("cancel: 체결 안 될 값(85%)으로 사고, 안 붙고 취소되면 통과",
      run("cancel") is None and abs(f.orders[0][2] - 127.5) < 0.01, f"{f.orders}")
last = json.loads(open(sm.LOG_FILE, encoding="utf-8").read().splitlines()[-1])
check("cancel 결과가 통과로 기록된다", last["step"] == "미체결 취소" and last["ok"] is True, "")

print("── LOC ──")
f = Fake(); setup(f, et=(15, 50))
check("15:45 ET 이 지나면 LOC 시험을 하지 않는다 (15:50 이후 거래소가 받지 않는다)", run("loc") == 3 and f.orders == [], "")
f = Fake(); setup(f, et=(14, 0))
check("loc: 현재가 +3% 한도 LOC 1주 접수 · 상태 저장", run("loc") is None and f.orders[0][1] == sm.namuh.ORD_LOC
      and abs(f.orders[0][2] - 154.5) < 0.01 and sm.load_state()["loc"]["orderId"] == "777", f"{f.orders}")
setup(f, et=(14, 30))
check("확인하지 않은 LOC 가 있으면 새 LOC 를 내지 않는다", run("loc") == 2 and len(f.orders) == 1, "")
setup(f, et=(15, 0))
check("loc-check: 그 정규장이 끝나기 전에는 확인하지 않는다", run("loc-check") == 3, "")
f.qty = 1
setup(f, open_=False, et=(17, 0))
check("loc-check: 마감 뒤 잔고 +1주면 통과", run("loc-check") is None and sm.load_state()["loc"]["checked"]
      and sm.load_state()["loc"]["filled"] == 1, "")
f.qty = 2
setup(f)
check("sell: 시험 종목이 1주보다 많으면 무엇을 파는지 몰라 멈춘다", run("sell") == 2, "")
f.qty = 1
setup(f)
check("sell: 시험으로 산 1주를 판다", run("sell") is None and f.qty == 0, "")

print("── 원화 증거금 ──")
sm_fx = sys.modules.get("services.fx")
import services.fx as fxmod                          # noqa: E402
fxmod.get_official_fx_rate = lambda: (1380.0, "")
f = Fake(); setup(f)
check("krw: 원화 증거금으로 사고 판다", run("krw") is None and sm.namuh.MARGIN_PREF == "krw"
      and [o[0] for o in f.orders] == ["buy", "sell"], "")
f = Fake(); setup(f)
f.get_balance = lambda fresh=False: {"qtyByTicker": {"UPRO": 0.0}, "usdAvailable": 10_000.0, "krwDeposit": 100_000.0}
check("krw: 원화 예수금이 1주 값보다 적으면 멈춘다", run("krw") == 2 and f.orders == [], "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
