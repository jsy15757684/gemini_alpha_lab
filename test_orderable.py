#!/usr/bin/env python3
"""실제 주문 가능 금액 — 예수금(fc_dca)이 아니라 매수가능금액 조회로 매수 여력 · 증거금 통화를 정하는가.

2026-10-06 실계좌: BMNR 10주를 팔아 달러 예수금 $0 · 주문가능 $269.74, 원화 예수금 0 ·
원화 증거금 주문가능 2,945,222원이었다. 예수금만 보면 '돈이 없다' 로 보이고, 주문은
달러가 있는데도 원화 증거금으로 나간다. 가짜 통신으로 본다.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests                                      # noqa: E402
from services import namuh                           # noqa: E402
from services.namuh import NamuhAccount              # noqa: E402

namuh._NH_MIN_INTERVAL = 0.0
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


class _Resp:
    def __init__(self, code=200, payload=None):
        self.status_code, self._p, self.text = code, payload or {}, ""

    def json(self):
        return self._p


CALLS = []
ORDERABLE = {"1": 269.74, "2": 2_945_222.0}


def post(url, **kw):
    inp = (kw.get("json") or {}).get("Input_0", {})
    if url.endswith("/inquiry/v1/buyableAmount"):
        CALLS.append(inp.get("wtm_cur_knd_cd"))
        v = ORDERABLE.get(inp.get("wtm_cur_knd_cd"))
        if v is None:
            return _Resp(500, {"rsp_cd": "X", "rsp_msg": "실패"})
        return _Resp(200, {"rsp_cd": "00166", "Output_0": {"orr_pbl_amt": v, "fc_dca": 0.0}})
    raise AssertionError(url)


requests.post = post
os.environ["NAMUH_MOCK"] = "0"
BAL = {"usdAvailable": 0.0, "krwDeposit": 0.0, "qtyByTicker": {"BMNR": 5265.0}}


def acct():
    namuh._ORDERABLE_CACHE.clear()
    a = NamuhAccount("APPKEY", "SECRET", "12345678901")
    a._token, a._token_expires_at = "FAKE", time.time() + 9999
    return a


print("── 주문 가능 금액 ──")
a = acct()
o = a.orderable("UPRO")
check("달러 · 원화 증거금 주문 가능 금액을 따로 받는다", o == {"usd": 269.74, "krw": 2_945_222.0} and CALLS == ["1", "2"], f"{o}")
CALLS.clear()
a.orderable("UPRO")
check("15초 안에는 다시 묻지 않는다 (호출 한도)", CALLS == [], f"{CALLS}")
b = acct().with_orderable(BAL, "UPRO")
check("잔고에 얹으면 주문 가능 금액이 usdAvailable · krwDeposit 이 된다 (예수금은 따로 남긴다)",
      b["usdAvailable"] == 269.74 and b["krwDeposit"] == 2_945_222.0 and b["usdDeposit"] == 0.0
      and b["orderableKnown"], f"{b}")
check("원래 잔고는 바꾸지 않는다", BAL["usdAvailable"] == 0.0, "")

print("── 증거금 통화 · 매수 여력 ──")
namuh.MARGIN_PREF = "auto"
check("달러 주문 가능($269.74)이 1주 값($157)보다 크면 달러 증거금 (예전에는 예수금 $0 이라 원화로 나갔다)",
      namuh.margin_code(157.0, b) == namuh.MARGIN_USD and namuh.margin_code(157.0, BAL) == namuh.MARGIN_KRW, "")
check("달러가 모자라면 원화 증거금", namuh.margin_code(400.0, b) == namuh.MARGIN_KRW, "")
bp = namuh.buying_power_usd(b, 1380.0)
check("매수 여력은 달러 · 원화 중 큰 쪽만 센다 (원화 주문가능에 달러가 들어 있을 수 있어 더하지 않는다)",
      abs(bp["total"] - 2_945_222.0 / 1380.0) < 0.01, f"${bp['total']:,.2f}")
check("예수금만 보던 때는 여력 0", namuh.buying_power_usd(BAL, 1380.0)["total"] == 0.0, "")

print("── 실패 · 모의 ──")
ORDERABLE.pop("1")
b2 = acct().with_orderable(BAL, "UPRO")
check("한쪽 조회가 실패하면 그쪽은 예수금 값을 그대로 둔다 (지어내지 않는다)",
      b2["usdAvailable"] == 0.0 and b2["krwDeposit"] == 2_945_222.0, f"{b2}")
ORDERABLE.clear()
b3 = acct().with_orderable(BAL, "UPRO")
check("둘 다 실패하면 orderableKnown=False · 예전처럼 예수금으로 센다", b3["orderableKnown"] is False
      and namuh.buying_power_usd(b3, 1380.0)["total"] == 0.0, "")
os.environ["NAMUH_MOCK"] = "1"
CALLS.clear()
check("모의계좌는 묻지 않는다 (예수금 = 주문가능 · 달러만)", acct().with_orderable(BAL) is BAL and CALLS == [], "")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
