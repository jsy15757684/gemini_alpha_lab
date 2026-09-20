"""나무증권 연동이 값을 지어내지 않는지 검증한다.

이 프로젝트에서 같은 종류의 버그를 세 번 만났다 — 공시환율 1385.0,
바이낸스 BTC $65,000, 빗썸 가격 ×1.015 합성. 나무증권 연동에도 같은 것이
들어와 있었다: 시세 실패 시 TQQQ $75.50, 잔고 실패 시 '성공 · $10,000',
캔들 실패 시 현재가로 만든 합성 봉 200개.

지어낸 값은 화면을 정상으로 보이게 하면서 판단을 통째로 틀리게 만든다.
봇 루프는 예외를 '판단 보류' 로 처리하므로, 못 받았을 때 예외가 나야 한다.

거래소 대조도 함께 본다. 반환 형식과 소비 형식이 어긋나 대조가 통째로
동작하지 않았다(holdings 는 dict 인데 list 로 순회, 키 이름도 달랐다).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests                                    # noqa: E402
from services import namuh                         # noqa: E402
from services.namuh import NamuhAccount, NamuhError  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


class _Resp:
    def __init__(self, code=500, payload=None, text="server error"):
        self.status_code, self._p, self.text = code, payload or {}, text

    def json(self):
        return self._p


def _cut_network():
    def boom(*a, **kw):
        raise requests.ConnectionError("네트워크 단절 (시험)")
    requests.get = boom
    requests.post = boom


_real_get, _real_post = requests.get, requests.post


def _restore_network():
    requests.get, requests.post = _real_get, _real_post


def _acct(token=True):
    a = NamuhAccount("APPKEY", "SECRET", "1234567890")
    if token:
        a._token = "FAKE"
        a._token_expires_at = time.time() + 9999
    return a


print("\n── 시세 ──")
namuh._price_cache.clear()
_cut_network()
a = _acct()
try:
    p = a.get_price("TQQQ")
    check("시세를 못 받으면 값을 지어내지 않는다", False, f"${p} 를 돌려줬다")
except NamuhError as e:
    check("시세를 못 받으면 값을 지어내지 않는다", True, str(e)[:52])
check("실패한 시세를 캐시에 남기지 않는다",
      "TQQQ" not in namuh._price_cache, f"캐시 {len(namuh._price_cache)}건")

print("\n── 캔들 ──")
try:
    c = a.get_candles("TQQQ", "24h", limit=200)
    synth = len(c) > 0 and len({round(x["close"], 6) for x in c}) == 1
    check("캔들을 못 받으면 합성하지 않는다", False,
          f"{len(c)}봉 생성" + (" (전부 같은 종가 = 합성)" if synth else ""))
except NamuhError as e:
    check("캔들을 못 받으면 합성하지 않는다", True, str(e)[:52])

print("\n── 잔고 ──")
try:
    b = a.get_balance()
    check("통신이 끊기면 가짜 잔고를 만들지 않는다", False,
          f"success={b.get('success')} 예수금 ${b.get('usdAvailable'):,.0f}")
except NamuhError as e:
    check("통신이 끊기면 가짜 잔고를 만들지 않는다", True, str(e)[:52])

_restore_network()
requests.get = lambda *a_, **k: _Resp(500, {"rt_cd": "1", "msg1": "조회 권한 없음"})
a2 = _acct()
try:
    b = a2.get_balance()
    check("서버가 거부하면 가짜 잔고를 만들지 않는다", False,
          f"success={b.get('success')} 예수금 ${b.get('usdAvailable'):,.0f}")
except NamuhError as e:
    check("서버가 거부하면 가짜 잔고를 만들지 않는다", True, str(e)[:52])
check("가짜 잔고를 만드는 코드가 남아 있지 않다",
      not hasattr(NamuhAccount, "_mock_balance"), "_mock_balance 제거됨")

print("\n── 거래소 대조 형식 ──")
_restore_network()
payload = {
    "output1": [{"ovrs_pdno": "TQQQ", "ovrs_cblc_qty": "12", "pchs_avg_pric": "70.0",
                 "ovrs_stck_evlu_amt": "900.0"}],
    "output2": {"ovrs_ord_psbl_amt": "500.0", "tot_evlu_pfls_amt": "1400.0"},
}
requests.get = lambda *a_, **k: _Resp(200, payload)
bal = _acct().get_balance()
check("보유 종목을 읽는다", bal["holdings"].get("TQQQ", {}).get("qty") == 12.0,
      f"TQQQ {bal['holdings'].get('TQQQ', {}).get('qty')}주")
check("대조용 '종목→수량' 을 함께 준다",
      bal.get("qtyByTicker") == {"TQQQ": 12.0}, str(bal.get("qtyByTicker")))

# 복원 코드와 같은 방식으로 읽어 본다 (예전에는 여기서 AttributeError 가 났다)
try:
    qty_map = bal.get("qtyByTicker")
    if qty_map is None:
        qty_map = {t: (v or {}).get("qty", 0.0) for t, v in (bal.get("holdings") or {}).items()}
    out = {str(t).upper(): float(q or 0.0) for t, q in qty_map.items()}
    check("복원 코드가 이 형식을 읽을 수 있다", out == {"TQQQ": 12.0}, str(out))
except Exception as e:
    check("복원 코드가 이 형식을 읽을 수 있다", False, f"{type(e).__name__}: {e}")

print("\n── OAuth 토큰 (공식 문서: 만료 전 재발급 금지) ──")
import tempfile as _tf   # noqa: E402
namuh.TOKEN_FILE = os.path.join(_tf.mkdtemp(prefix="ntok-"), "namuh_token.json")
_restore_network()

t1 = NamuhAccount("APPKEY", "SECRET", "1234567890")
t1._token, t1._token_expires_at = "TOK-ABC", time.time() + 86400
t1._save_token_to_disk()
check("발급한 토큰을 디스크에 남긴다", os.path.exists(namuh.TOKEN_FILE),
      f"권한 {oct(os.stat(namuh.TOKEN_FILE).st_mode)[-3:]}")

t2 = NamuhAccount("APPKEY", "SECRET", "1234567890")     # 재시작 흉내
_cut_network()                                           # 발급은 불가능한 상태
check("재시작해도 남은 토큰을 재사용한다 (재발급하지 않는다)",
      t2.get_token() == "TOK-ABC", "통신이 끊겼는데도 토큰을 얻음")
_restore_network()

t3 = NamuhAccount("OTHERKEY", "SECRET", "1234567890")
t3._load_token_from_disk()
check("앱키가 다르면 저장된 토큰을 쓰지 않는다", t3._token is None, "재사용 안 함")

check("토큰 파일에 앱키 원문을 저장하지 않는다",
      "APPKEY" not in open(namuh.TOKEN_FILE, encoding="utf-8").read(), "해시만 저장")

t4 = NamuhAccount("APPKEY", "SECRET", "1234567890")
t4._token, t4._token_expires_at = "TOK-OLD", time.time() + 60   # 만료 임박
t4._save_token_to_disk()
t5 = NamuhAccount("APPKEY", "SECRET", "1234567890")
t5._load_token_from_disk()
check("만료가 임박한 토큰은 재사용하지 않는다", t5._token is None, "5분 미만 남으면 버림")

print("\n── 미국 장 시간 ──")
from datetime import datetime, timezone   # noqa: E402
MS = namuh.market_session
cases = [
    ("평일 10:30 EDT (정규장)", datetime(2026, 9, 17, 14, 30, tzinfo=timezone.utc), True),
    ("평일 01:00 EDT (야간)", datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc), False),
    ("토요일 11:30 EDT", datetime(2026, 9, 19, 15, 30, tzinfo=timezone.utc), False),
    ("겨울 10:30 EST (정규장)", datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc), True),
    ("개장 1분 전 09:29", datetime(2026, 9, 17, 13, 29, tzinfo=timezone.utc), False),
    ("마감 정각 16:00", datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc), False),
]
ok = all(MS(t)["open"] is exp for _, t, exp in cases)
check("정규장 판정이 서머타임까지 맞다", ok,
      " · ".join(f"{n}={'열림' if MS(t)['open'] else '닫힘'}" for n, t, _ in cases[:3]))

print("\n── 주문 수량 ──")
namuh._price_cache.clear()
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a3 = _acct()

try:
    a3.market_buy("TQQQ", 25.0)
    check("1주 값보다 작은 배정액으로 매수하지 않는다", False, "주문이 나갔다")
except NamuhError as e:
    check("1주 값보다 작은 배정액으로 매수하지 않는다", True, str(e)[:56])

namuh._price_cache["TQQQ"] = (time.time(), 75.50)
unconf = NamuhAccount()            # 키 미등록 = 모의 경로
r = unconf.market_buy("TQQQ", 400.0)
check("배정액을 넘지 않게 내림한다", r["units"] == 5.0 and r["amountUsd"] <= 400.0,
      f"$400 → {r['units']:.0f}주 = ${r['amountUsd']:,.2f}")

try:
    a3.market_sell("TQQQ", 0.4)
    check("1주 미만은 매도 주문을 내지 않는다", False, "주문이 나갔다")
except NamuhError as e:
    check("1주 미만은 매도 주문을 내지 않는다", True, str(e)[:56])

print("\n── 장 마감 중 주문 ──")
_restore_network()
requests.get = lambda *a_, **k: _Resp(200, payload)
import services.namuh as _nm       # noqa: E402
_orig_ms = _nm.market_session
_nm.market_session = lambda *a_, **k: {"open": False, "etTime": "-", "tz": "EDT",
                                       "reason": "주말 (미국 정규장 휴장)"}
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a4 = _acct()
try:
    a4.market_buy("TQQQ", 400.0)
    check("장이 닫혀 있으면 매수 주문을 내지 않는다", False, "주문이 나갔다")
except NamuhError as e:
    check("장이 닫혀 있으면 매수 주문을 내지 않는다", True, str(e)[:56])
try:
    a4.market_sell("TQQQ", 5)
    check("장이 닫혀 있으면 매도 주문을 내지 않는다", False, "주문이 나갔다")
except NamuhError as e:
    check("장이 닫혀 있으면 매도 주문을 내지 않는다", True, str(e)[:56])
_nm.market_session = _orig_ms

print("\n── 체결 확인 ──")
_nm.market_session = lambda *a_, **k: {"open": True, "etTime": "-", "tz": "EDT", "reason": ""}
# 주문은 접수되지만(rt_cd=0) 보유 수량이 변하지 않는다 = 지정가 미체결
requests.post = lambda *a_, **k: _Resp(200, {"rt_cd": "0", "output": {"ODNO": "X1"}})
a5 = _acct()
a5._await_fill = lambda *a_, **k: 0.0          # 대기 시간 없이 미체결 상황만 재현
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
try:
    a5.market_buy("TQQQ", 400.0)
    check("접수만 되고 체결 안 되면 장부를 바꾸지 않는다", False, "체결로 기록했다")
except NamuhError as e:
    check("접수만 되고 체결 안 되면 장부를 바꾸지 않는다", True, str(e)[:56])

a6 = _acct()
a6._await_fill = lambda *a_, **k: 3.0          # 5주 요청에 3주만 체결
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
r = a6.market_buy("TQQQ", 400.0)
check("부분 체결은 실제 체결 수량으로 기록한다",
      r["units"] == 3.0 and r["status"] == "PARTIAL" and r["requestedUnits"] == 5.0,
      f"요청 {r['requestedUnits']:.0f}주 → 체결 {r['units']:.0f}주 ({r['status']})")
_nm.market_session = _orig_ms

_restore_network()
namuh._price_cache.clear()

print(f"\n{'=' * 58}")
if FAIL:
    print(f"통과 {len(PASS)}개 · 실패 {len(FAIL)}개")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print(f"전체 {len(PASS)}개 항목 통과")
