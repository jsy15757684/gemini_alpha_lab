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
namuh._NH_MIN_INTERVAL = 0.0   # 호출 간격은 아래 전용 검증에서 따로 본다
namuh._NH_BACKOFF_SEC = 0.0
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
    a = NamuhAccount("APPKEY", "SECRET", "12345678901")
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
requests.post = lambda *a_, **k: _Resp(500, {"rsp_cd": "IGW9999", "rsp_msg": "조회 권한 없음"})
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
# 공식 문서의 응답 형식 그대로
payload = {
    "rsp_cd": "00166", "rsp_msg": "조회가 완료되었습니다.",
    "Output_0": {"fc_dca": 500.0, "fc_aet_amt": 1400.0,
                 "krw_dca": 700000, "tot_aet_amt": 1900000},
    "Output_1": [{"iem_cd": "TQQQ", "iem_nm": "프로셰어즈 QQQ 3배",
                  "cns_bse_bnc_qty": 12, "sll_pbl_qty1": 10,
                  "fc_phs_uit_pr": 70.0, "fc_sec_end_pr": 75.0,
                  "fc_eal_amt": 900.0, "fc_eal_pls_amt": 60.0, "cur_cd": "USD"}],
}
requests.post = lambda *a_, **k: _Resp(200, payload)
bal = _acct().get_balance()
check("보유 종목을 읽는다", bal["holdings"].get("TQQQ", {}).get("qty") == 12.0,
      f"TQQQ {bal['holdings'].get('TQQQ', {}).get('qty')}주")
check("체결기준잔고와 매도가능수량을 구분한다",
      bal["qtyByTicker"] == {"TQQQ": 12.0} and bal["sellableByTicker"] == {"TQQQ": 10.0},
      "잔고 12주 · 매도가능 10주")
check("외화 예수금·자산을 Output_0 에서 읽는다",
      bal["usdAvailable"] == 500.0 and bal["usdTotal"] == 1400.0,
      f"예수금 ${bal['usdAvailable']:,.0f} · 자산 ${bal['usdTotal']:,.0f}")
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

print("\n── 취급 종목 (3배 레버리지 ETF 만) ──")
from services.namuh import NAMUH_STOCKS   # noqa: E402
check("3배 ETF 5종만 남아 있다", set(NAMUH_STOCKS) == {"TQQQ", "SOXL", "UPRO", "TECL", "FNGU"},
      " · ".join(sorted(NAMUH_STOCKS)))
check("1배 개별주가 없다", all(v["leverage"] == "3x" for v in NAMUH_STOCKS.values()),
      "NVDA·AAPL·TSLA 제거됨")
check("거래소 코드가 종목 마스터와 맞다",
      NAMUH_STOCKS["TQQQ"]["market"] == "NASDAQ"
      and all(NAMUH_STOCKS[t]["market"] == "NYSE" for t in ("SOXL", "UPRO", "TECL", "FNGU")),
      "TQQQ=NQQ 나스닥 · 나머지 NYY 뉴욕")
_srv = open("server.py", encoding="utf-8").read()
check("서버가 목록에 없는 종목의 배포를 막는다",
      "나무증권 지원 종목이 아닙니다" in _srv, "deploy 가드")
_trd = open("services/trader.py", encoding="utf-8").read()
check("목록에서 빠진 종목의 봇은 재가동하지 않는다",
      "지원하지 않는 해외주식 종목이라 재가동하지 않습니다" in _trd, "restore 가드")

print("\n── 모의/실계좌 도메인 분리 ──")
_prev_mock = os.environ.get("NAMUH_MOCK")
os.environ.pop("NAMUH_MOCK", None)
check("기본은 실계좌(운영 도메인)다",
      not namuh.use_mock() and namuh.trade_base_url() == namuh.BASE_URL,
      namuh.trade_base_url())
os.environ["NAMUH_MOCK"] = "1"
check("NAMUH_MOCK=1 이면 잔고·주문이 모의 도메인으로 간다",
      namuh.use_mock() and namuh.trade_base_url() == namuh.MOCK_BASE_URL,
      namuh.trade_base_url())
check("토큰·시세는 모의여도 운영 도메인을 쓴다 (모의 미제공)",
      namuh.BASE_URL != namuh.MOCK_BASE_URL and "moapi" not in namuh.BASE_URL,
      namuh.BASE_URL)
if _prev_mock is None:
    os.environ.pop("NAMUH_MOCK", None)
else:
    os.environ["NAMUH_MOCK"] = _prev_mock

print("\n── OAuth 토큰 (공식 문서: 만료 전 재발급 금지) ──")
import tempfile as _tf   # noqa: E402
namuh.TOKEN_FILE = os.path.join(_tf.mkdtemp(prefix="ntok-"), "namuh_token.json")
_restore_network()

t1 = NamuhAccount("APPKEY", "SECRET", "12345678901")
t1._token, t1._token_expires_at = "TOK-ABC", time.time() + 86400
t1._save_token_to_disk()
check("발급한 토큰을 디스크에 남긴다", os.path.exists(namuh.TOKEN_FILE),
      f"권한 {oct(os.stat(namuh.TOKEN_FILE).st_mode)[-3:]}")

t2 = NamuhAccount("APPKEY", "SECRET", "12345678901")     # 재시작 흉내
_cut_network()                                           # 발급은 불가능한 상태
check("재시작해도 남은 토큰을 재사용한다 (재발급하지 않는다)",
      t2.get_token() == "TOK-ABC", "통신이 끊겼는데도 토큰을 얻음")
_restore_network()

t3 = NamuhAccount("OTHERKEY", "SECRET", "12345678901")
t3._load_token_from_disk()
check("앱키가 다르면 저장된 토큰을 쓰지 않는다", t3._token is None, "재사용 안 함")

check("토큰 파일에 앱키 원문을 저장하지 않는다",
      "APPKEY" not in open(namuh.TOKEN_FILE, encoding="utf-8").read(), "해시만 저장")

t4 = NamuhAccount("APPKEY", "SECRET", "12345678901")
t4._token, t4._token_expires_at = "TOK-OLD", time.time() + 60   # 만료 임박
t4._save_token_to_disk()
t5 = NamuhAccount("APPKEY", "SECRET", "12345678901")
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


_ORDERS = []


def _route(url, *a_, **k):
    """잔고와 주문이 같은 POST 라 URL 로 갈라준다."""
    u = url if isinstance(url, str) else ""
    if "/balance" in u:
        return _Resp(200, payload)                       # 보유 12주 고정
    # 공식 문서의 주문 응답: Output_0.orr_no · rsp_cd 00171
    _ORDERS.append((u, k.get("json")))
    return _Resp(200, {"rsp_cd": "00171", "rsp_msg": "주문이 완료되었습니다.",
                       "Output_0": {"orr_no": 548597}})


requests.post = _route
namuh._price_cache["TQQQ"] = (time.time(), 75.50)

a5 = _acct()
a5._await_fill = lambda *a_, **k: 0.0          # 미체결 상황 재현 (대기 없이)
try:
    a5.market_buy("TQQQ", 400.0)
    check("접수만 되고 체결 안 되면 장부를 바꾸지 않는다", False, "체결로 기록했다")
except NamuhError as e:
    check("접수만 되고 체결 안 되면 장부를 바꾸지 않는다",
          "체결되지 않았습니다" in str(e), str(e)[:56])

a6 = _acct()
a6._await_fill = lambda *a_, **k: 3.0          # 5주 요청에 3주만 체결
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
r = a6.market_buy("TQQQ", 400.0)
check("부분 체결은 실제 체결 수량으로 기록한다",
      r["units"] == 3.0 and r["status"] == "PARTIAL" and r["requestedUnits"] == 5.0,
      f"요청 {r['requestedUnits']:.0f}주 → 체결 {r['units']:.0f}주 ({r['status']})")

check("주문 응답의 orr_no 를 주문번호로 쓴다", r["orderId"] == "548597", r["orderId"])

print("\n── 주문 규격 (공식 문서 대조) ──")
_ORDERS.clear()
a8 = _acct(); a8._await_fill = lambda *a_, **k: 5.0
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a8.market_buy("TQQQ", 400.0)
url, body = _ORDERS[-1]
inp = (body or {}).get("Input_0", {})
check("매수 URL 이 /gbstock/order/v1/buy 다", url.endswith("/gbstock/order/v1/buy"), url[-32:])
check("계좌번호를 11자리 그대로 보낸다 (쪼개지 않는다)",
      inp.get("act_no") == a8.account_no and len(inp.get("act_no", "")) == 11,
      f"act_no {len(inp.get('act_no',''))}자리")
check("국가코드·종목코드가 문서 형식이다",
      inp.get("fc_sec_trd_nat_cd") == "200" and inp.get("iem_cd") == "TQQQ",
      f"{inp.get('fc_sec_trd_nat_cd')} · {inp.get('iem_cd')}")
# 기본은 지정가다. 모의계좌는 시장가를 아예 받지 않고("14650 모의투자
# 지정가만 가능한 상품입니다"), 실계좌에서도 3배 ETF 를 시장가로 던지면
# 체결가를 통제할 수 없다. 현재가 +0.5% 상한이면 사실상 즉시 붙는다.
_want_lim = round(75.50 * (1 + namuh.LIMIT_SLIP_PCT / 100.0), 2)
check("기본 주문이 지정가(00)다 (시장가 아님)",
      inp.get("ahi_nmn_pr_tp_cd") == "00", f"유형 {inp.get('ahi_nmn_pr_tp_cd')}")
check("기본 지정가 상한은 현재가 + 여유폭이다",
      inp.get("fc_orr_uit_pr") == _want_lim,
      f"현재가 $75.50 → 상한 ${inp.get('fc_orr_uit_pr')} (+{namuh.LIMIT_SLIP_PCT}%)")
check("매수에는 증거금통화종류코드를 보낸다", inp.get("wtm_cur_knd_cd") == "1", "1.해당통화")

_ORDERS.clear()
a9 = _acct(); a9._await_fill = lambda *a_, **k: 5.0
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a9.market_buy("TQQQ", 400.0, order_type=namuh.ORD_LOC)
inp = (_ORDERS[-1][1] or {}).get("Input_0", {})
check("LOC(12) 등 지정가 계열은 단가를 함께 보낸다",
      inp.get("ahi_nmn_pr_tp_cd") == "12" and inp.get("fc_orr_uit_pr") == _want_lim,
      f"유형 12 · 단가 ${inp.get('fc_orr_uit_pr')}")

# 상한을 직접 주면 그 값이 그대로 나가야 한다 (반반 매수의 두 다리)
_ORDERS.clear()
a9b = _acct(); a9b._await_fill = lambda *a_, **k: 5.0
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a9b.market_buy("TQQQ", units=2, order_type=namuh.ORD_LIMIT, limit_price=70.00)
inp = (_ORDERS[-1][1] or {}).get("Input_0", {})
check("상한을 직접 주면 여유폭을 더하지 않는다",
      inp.get("fc_orr_uit_pr") == 70.00, f"단가 ${inp.get('fc_orr_uit_pr')}")

# 즉시 체결을 기대한 지정가가 안 붙으면 거둬들여야 한다
_ORDERS.clear()
_CANCELS = []
a9c = _acct()
a9c._await_fill = lambda *a_, **k: 0.0
a9c.cancel_order = lambda oid, tkr, qty=0.0: _CANCELS.append(oid) or {"cancelOrderId": "9"}
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
try:
    a9c.market_buy("TQQQ", 400.0)
    _msg = ""
except NamuhError as e:
    _msg = e.message
check("즉시 지정가가 미체결이면 주문을 취소한다",
      len(_CANCELS) == 1 and "취소했습니다" in _msg,
      f"취소 {len(_CANCELS)}건 — 안 하면 거래소에 남아 봉마다 쌓인다")

# LOC 는 마감에 붙는 것이라 취소하면 안 된다
_CANCELS2 = []
a9d = _acct()
a9d._await_fill = lambda *a_, **k: 0.0
a9d.cancel_order = lambda oid, tkr, qty=0.0: _CANCELS2.append(oid)
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
try:
    a9d.market_buy("TQQQ", 400.0, order_type=namuh.ORD_LOC)
except NamuhError:
    pass
check("LOC 은 미체결이어도 취소하지 않는다", not _CANCELS2,
      "마감 동시호가에 붙는 주문이다")

_ORDERS.clear()
a10 = _acct(); a10._await_fill = lambda *a_, **k: 10.0
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a10.market_sell("TQQQ", 10)
url, body = _ORDERS[-1]
inp = (body or {}).get("Input_0", {})
check("매도 URL 이 /gbstock/order/v1/sell 다", url.endswith("/gbstock/order/v1/sell"), url[-32:])
check("매도에는 증거금통화종류코드를 보내지 않는다",
      "wtm_cur_knd_cd" not in inp, "문서에 없는 필드는 보내지 않는다")

# 잔고보다 많이 팔려고 하면 있는 만큼만
a7 = _acct()
a7._await_fill = lambda *a_, **k: 12.0
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
r = a7.market_sell("TQQQ", 50)
check("계좌 보유량을 넘겨 팔지 않는다", r["requestedUnits"] == 12.0,
      f"50주 요청 → 계좌 보유 12주만 주문")
_nm.market_session = _orig_ms

_restore_network()
namuh._price_cache.clear()

# ── 주문이 실패해도 장부가 틀어지지 않는가 ──
#
# USD 분기를 만들면서 현금 차감이 주문보다 **앞**으로 갔다. 주문이 거부되면
# return 하므로 현금만 줄고 주식은 늘지 않았다 (실측 $250.10 / $294.12 증발).
# 디스크에는 바로 안 쓰이지만, 다음 매수가 성공하는 순간 틀어진 값이 저장된다.
# 저장 경로를 임시 디렉터리로 갈아끼운 뒤에 봇 모듈을 import 한다.
# 이걸 빼먹으면 테스트가 개발 기계의 data/bots.json · trades.json 을 덮어쓴다.
import tempfile                                     # noqa: E402
_SANDBOX = tempfile.mkdtemp(prefix="namuh-safety-")

from services import botstore                       # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
botstore.arb_store.path = os.path.join(_SANDBOX, "arb_bots.json")

from services import tradelog                       # noqa: E402
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True        # 운영 일지를 읽지 않는다

from services import market_schedule as _ms_mod      # noqa: E402
from services.trader import TradingBot              # noqa: E402
from services.strategy import StrategyParams        # noqa: E402


class _Refusing:
    configured = True

    def market_buy(self, *a, **k):
        raise NamuhError("주문 거부 (테스트)")


class _ZeroFill:
    """접수는 됐는데 한 주도 안 붙은 응답."""
    configured = True

    def market_buy(self, *a, **k):
        return {"orderId": "T", "units": 0}


def _bot(acct, held_turn=0):
    p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4",
                       splitCount=10, targetProfitPct=10.0, locMode="half_half")
    b = TradingBot(bot_id="SAFETY", coin="TQQQ", interval="1h", mode="LIVE",
                   capital_krw=1000.0, params=p, broker="namuh")
    b.namuh_account = acct
    if held_turn:
        b.pos.units, b.pos.entryPrice, b.pos.turn = 4.0, 50.0, held_turn
    return b


for _label, _acct, _turn in (("주문 거부", _Refusing, 0), ("체결 0주", _ZeroFill, 0)):
    for _path, _held, _price in (("1회차/단일", 0, 50.0),
                                 ("전반전 반반LOC", 1, 49.0),
                                 ("후반전 평단LOC", 6, 49.0)):
        _b = _bot(_acct(), _held)
        _c0, _u0 = _b.cash, _b.pos.units
        _b._enter_chunk(price=_price, invest_krw=300.0, reason="검증")
        check(f"{_label} 시 현금이 줄지 않는다 ({_path})",
              abs(_b.cash - _c0) < 1e-9 and _b.pos.units == _u0,
              f"현금 ${_c0:,.2f} 유지 · 보유 {_u0}주 유지")

# 부분 체결은 **체결분만** 정산해야 한다 (요청 수량으로 깎으면 안 된다)
class _PartialFill:
    configured = True

    def market_buy(self, *a, **k):
        return {"orderId": "T", "units": 2}


_b = _bot(_PartialFill())
_c0 = _b.cash
_b._enter_chunk(price=50.0, invest_krw=300.0, reason="검증")
_cps = 50.0 * (1 + _b.params.feePct / 100.0)
check("부분 체결은 체결 수량만큼만 현금을 깎는다",
      abs((_c0 - _b.cash) - 2 * _cps) < 0.01 and _b.pos.units == 2.0,
      f"5주 요청 → 2주 체결 · 차감 ${_c0 - _b.cash:,.2f}")

# ── 못 산 봉이 회차를 까먹지 않는가 ──
#
# 라오어에서 회차는 '몇 번 샀는가' 다. 평단·분할 소진·쿼터매도가 전부 이
# 값에 걸려 있다. 예전에는 LOC 미체결에도 turn 을 올려서, 평단 +5~12%
# 구간에 머물면 한 주도 안 사고 40회를 다 태운 뒤 **수익 중인 포지션을
# 쿼터매도** 했다 (실측: 9회 시도 0주 매수, turn 10/10).
_b = _bot(None)
_b.mode = "PAPER"
_b.pos.units, _b.pos.entryPrice, _b.pos.turn = 10.0, 100.0, 1
_c0 = _b.cash
for _ in range(9):
    _b._enter_chunk(price=108.0, invest_krw=200.0, reason="검증")   # 평단 +8%
check("LOC 미체결은 회차를 소진하지 않는다",
      _b.pos.turn == 1, f"9회 시도 후 turn={_b.pos.turn}/{_b.params.splitCount}")
check("미체결된 배정액을 이월금으로 쌓지 않는다",
      _b.budget_carryover == 0.0 and abs(_b.cash - _c0) < 1e-9,
      f"이월 ${_b.budget_carryover:,.2f} · 현금 ${_b.cash:,.2f} 그대로")

_b._enter_chunk(price=99.0, invest_krw=200.0, reason="검증")        # 평단 아래
check("체결되면 회차가 정상적으로 오른다",
      _b.pos.turn == 2 and _b.pos.units > 10.0,
      f"turn={_b.pos.turn} · 보유 {_b.pos.units}주")

# 1주 값에 못 미치는 잔돈은 진짜 이월이다 — 이건 계속 모아야 한다
_c = _bot(None)
_c.mode = "PAPER"
_c.pos.units, _c.pos.entryPrice, _c.pos.turn = 1.0, 250.0, 1
for _ in range(3):
    _c._enter_chunk(price=240.0, invest_krw=30.0, reason="검증")
check("1주 미만 잔돈은 회차 유지하며 계속 누적된다",
      _c.pos.turn == 1 and _c.budget_carryover > 60.0,
      f"turn={_c.pos.turn} · 이월 ${_c.budget_carryover:,.2f}")

# ── 반반 매수의 두 다리가 각자의 상한으로 따로 나가는가 ──
#
# 예전에는 두 다리를 한 건으로 합쳐 보내면서 지정가도 안 실었다. 그러면
# 시장가가 되어 '평단 위로는 안 산다' 는 보장이 사라지는데, 로그에는
# "평단LOC 3주 + 평단+5%LOC 3주" 라고 두 다리로 적혔다.
#
# 주문 종류도 바꿨다. LOC(12, 장마감 지정가)는 마감 동시호가에만 붙어서
# 6초 체결 확인을 통과하지 못한다. 그 사이 주문은 거래소에 살아 있어
# 봉마다 쌓인다. 현재가가 이미 상한 아래일 때만 주문하므로 지정가(00)면
# 즉시 체결되고 상한 보장도 그대로다.
_SENT = []


class _RecordingAccount:
    configured = True

    def market_buy(self, ticker, amount_usd=0.0, order_type="", units=0.0,
                   limit_price=0.0):
        _SENT.append({"units": int(units), "orderType": order_type,
                      "limitPrice": round(limit_price, 2)})
        return {"orderId": f"T{len(_SENT)}", "units": int(units)}


_b = _bot(_RecordingAccount())
_b.pos.units, _b.pos.entryPrice, _b.pos.turn = 4.0, 50.0, 1
_SENT.clear()
_b._enter_chunk(price=49.0, invest_krw=400.0, reason="검증")   # 평단 이하 → 두 다리 다 체결

check("반반 매수는 두 다리를 따로 낸다", len(_SENT) == 2,
      f"주문 {len(_SENT)}건: " + " · ".join(f"{o['units']}주@${o['limitPrice']}" for o in _SENT))
check("두 다리의 상한이 서로 다르다 (평단 / 평단+5%)",
      len(_SENT) == 2 and _SENT[0]["limitPrice"] == 50.00 and _SENT[1]["limitPrice"] == 52.50,
      f"{[o['limitPrice'] for o in _SENT]} (평단 $50.00 · 평단+5% $52.50)")
check("시장가가 아니라 지정가로 나간다",
      all(o["orderType"] == namuh.ORD_LIMIT for o in _SENT),
      f"주문종류 {[o['orderType'] for o in _SENT]} (00=지정가)")
check("장마감 지정가(LOC)는 쓰지 않는다",
      all(o["orderType"] != namuh.ORD_LOC for o in _SENT),
      "6초 체결 확인을 통과 못 해 주문이 거래소에 쌓인다")

# 평단 초과 ~ 평단+5% 이내면 B 다리 하나만 나가야 한다
_b2 = _bot(_RecordingAccount())
_b2.pos.units, _b2.pos.entryPrice, _b2.pos.turn = 4.0, 50.0, 1
_SENT.clear()
_b2._enter_chunk(price=51.0, invest_krw=400.0, reason="검증")
check("평단 초과 구간에서는 평단+5% 다리만 나간다",
      len(_SENT) == 1 and _SENT[0]["limitPrice"] == 52.50,
      f"주문 {len(_SENT)}건 · 상한 ${_SENT[0]['limitPrice'] if _SENT else '-'}")

# 한 다리가 실패해도 다른 다리 체결분은 살아야 한다
class _OneLegFails:
    configured = True
    calls = 0

    def market_buy(self, ticker, amount_usd=0.0, order_type="", units=0.0,
                   limit_price=0.0):
        type(self).calls += 1
        if type(self).calls == 1:
            raise NamuhError("첫 다리 거부 (테스트)")
        return {"orderId": "T", "units": int(units)}


_b3 = _bot(_OneLegFails())
_b3.pos.units, _b3.pos.entryPrice, _b3.pos.turn = 4.0, 50.0, 1
_c0 = _b3.cash
_b3._enter_chunk(price=49.0, invest_krw=400.0, reason="검증")
check("한 다리가 실패해도 나머지 체결분은 장부에 남는다",
      _b3.pos.units > 4.0 and _b3.cash < _c0,
      f"보유 {_b3.pos.units}주 · 현금 ${_b3.cash:,.2f}")

# ── 매크로 기어: 지표를 못 받으면 '모른다' 로 가야 한다 ──
#
# 예전 폴백은 evaluate_regime(500, 480, 18.5) 였다. 주석은 '중립 2단' 인데
# 실제 판정은 3단 고속 질주(1.2배 · 목표 12%) 였다. 야후가 막히면 봇이
# 가장 공격적으로 사들이는 구조였다.
from services import macro_regime as _mr            # noqa: E402

_mr._REGIME_CACHE = None
_orig_qqq = _mr._fetch_qqq_sma200
_mr._fetch_qqq_sma200 = lambda: (_ for _ in ()).throw(RuntimeError("HTTP 429"))
_reg = _mr.get_macro_regime(force_refresh=True)
_mr._fetch_qqq_sma200 = _orig_qqq
_mr._REGIME_CACHE = None

check("지표 수신 실패 시 공격 기어로 가지 않는다",
      _reg["gear"] == _mr.GEAR_NEUTRAL, _reg["gearName"])
check("지표 수신 실패 시 매수 배수를 건드리지 않는다",
      _reg["sizingMultiplier"] == 1.0, f"{_reg['sizingMultiplier']}x")
check("지표 수신 실패 시 목표 익절률을 덮어쓰지 않는다",
      _reg["recommendedTargetProfitPct"] is None,
      "None → 봇이 제 설정을 그대로 쓴다")
check("지표 없이 시세를 지어내지 않는다",
      _reg["qqqPrice"] is None and _reg["vix"] is None and _reg.get("degraded") is True,
      "qqqPrice · vix 모두 None · degraded=True")

# 실패가 이어져도 야후를 계속 두드리면 안 된다 (차단 유발)
_mr._REGIME_CACHE = None
_mr._FAIL_COUNT = 0
_mr._NEXT_RETRY_AT = 0.0
_hits = {"n": 0}


def _boom():
    _hits["n"] += 1
    raise RuntimeError("HTTP 429")


_orig_qqq2 = _mr._fetch_qqq_sma200
_mr._fetch_qqq_sma200 = _boom
for _ in range(100):
    _mr.get_macro_regime()
_first_gap = _mr._NEXT_RETRY_AT - time.time()
check("지표 수신이 실패해도 매 틱마다 재요청하지 않는다",
      _hits["n"] == 1, f"요청 100회 → 실제 호출 {_hits['n']}회")
check("실패가 이어지면 재시도 간격이 벌어진다",
      55 < _first_gap <= 60, f"첫 재시도 대기 {int(_first_gap)}초 (1분 → 2분 → … 최대 15분)")

_mr._fetch_qqq_sma200 = _orig_qqq2
_mr._REGIME_CACHE = None
_mr._FAIL_COUNT = 0
_mr._NEXT_RETRY_AT = 0.0

# ── 라오어 원전 LOC: 주문과 체결이 분리된다 ──
#
# LOC 는 마감 동시호가에서만 붙는다. 그래서 접수 시점에는 장부를 건드리면
# 안 되고, 체결은 그 세션이 끝난 뒤 잔고 변화로 정산해야 한다. 봇이 꺼져도
# 주문은 거래소에 살아 있으므로 미체결 목록은 디스크에 남아야 한다.
_LOC = {"qty": 4.0, "avg": 50.0, "orders": [], "cancelled": []}


class _LocBroker:
    configured = True

    def get_balance(self):
        return {"qtyByTicker": {"TQQQ": _LOC["qty"]},
                "holdings": {"TQQQ": {"qty": _LOC["qty"], "avgPrice": _LOC["avg"]}}}

    def market_buy(self, ticker, amount_usd=0.0, order_type="", units=0.0,
                   limit_price=0.0, await_fill=True):
        if order_type != namuh.ORD_LOC or await_fill is not False:
            raise AssertionError(f"LOC 접수가 아니다: {order_type} / await_fill={await_fill}")
        oid = 1000 + len(_LOC["orders"])
        _LOC["orders"].append({"id": oid, "units": int(units), "limit": limit_price})
        return {"orderId": oid, "units": 0.0, "status": "ACCEPTED"}

    def cancel_order(self, order_id, ticker, qty=0.0):
        _LOC["cancelled"].append(int(order_id))
        return {"cancelOrderId": "9", "originalOrderId": str(order_id)}

    def market_sell(self, ticker, units, order_type=""):
        _LOC["qty"] = 0.0
        return {"orderId": "S", "units": units, "price": 60.0}


def _loc_bot():
    p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4",
                       splitCount=10, targetProfitPct=10.0,
                       locMode="half_half", feePct=0.0)
    b = TradingBot(bot_id="LOC", coin="TQQQ", interval="1h", mode="LIVE",
                   capital_krw=1000.0, params=p, broker="namuh")
    b.namuh_account = _LocBroker()
    b.pos.units, b.pos.entryPrice, b.pos.turn = 4.0, 50.0, 1
    return b


_LOC["qty"], _LOC["avg"] = 4.0, 50.0
_LOC["orders"].clear()
_lb = _loc_bot()
_c0, _u0, _t0 = _lb.cash, _lb.pos.units, _lb.pos.turn
_lb._place_loc_orders(price=49.0, chunk_budget=400.0, session="2026-09-21", reason="2회차")
check("LOC 접수만으로는 장부가 움직이지 않는다",
      _lb.cash == _c0 and _lb.pos.units == _u0 and _lb.pos.turn == _t0,
      f"현금 ${_lb.cash:,.2f} · 보유 {_lb.pos.units}주 · T={_lb.pos.turn} 그대로")
check("두 다리를 각자의 상한으로 접수한다",
      len(_lb.pending_orders) == 2
      and {o["limit"] for o in _lb.pending_orders} == {50.0, 52.5},
      " · ".join(f"{o['leg']} {o['units']}주@${o['limit']}" for o in _lb.pending_orders))

_restored = TradingBot.restore(_lb.snapshot(), None, _LocBroker())
check("미체결 주문은 재시작을 넘어 살아남는다",
      len(_restored.pending_orders) == 2 and _restored.loc_session == "2026-09-21",
      f"{len(_restored.pending_orders)}건 · 세션 {_restored.loc_session}")

# 마감 뒤 8주 체결 → 계좌 12주, 평단 (4*50 + 8*48)/12
_LOC["qty"], _LOC["avg"] = 12.0, (4 * 50.0 + 8 * 48.0) / 12
_orig_sd, _orig_lw = _ms_mod.session_date, _ms_mod.loc_window
_ms_mod.session_date = lambda *a, **k: "2026-09-22"
_ms_mod.loc_window = lambda *a, **k: {"past": True, "pastClose": True, "sessionDate": "2026-09-22"}

# 접수 창이 닫혔다고 해서 체결된 게 아니다. 마감 전에는 정산하면 안 된다.
_same = TradingBot.restore(_lb.snapshot(), None, _LocBroker())
_ms_mod.session_date = lambda *a, **k: "2026-09-21"          # 아직 같은 세션
_ms_mod.loc_window = lambda *a, **k: {"past": True, "pastClose": False,
                                      "sessionDate": "2026-09-21"}
_same._settle_pending_loc()
check("접수 마감만으로는 정산하지 않는다 (체결은 장 마감에 일어난다)",
      len(_same.pending_orders) == 2 and _same.pos.turn == 1,
      "미체결로 오판하면 마감에 들어온 물량을 장부가 놓친다")
_ms_mod.session_date = lambda *a, **k: "2026-09-22"
_ms_mod.loc_window = lambda *a, **k: {"past": True, "pastClose": True, "sessionDate": "2026-09-22"}

_restored._settle_pending_loc()
check("체결가를 잔고 변화로 정확히 역산한다",
      _restored.pos.units == 12.0 and abs(_restored.pos.entryPrice - 48.6667) < 0.01,
      f"보유 12주 · 평단 ${_restored.pos.entryPrice:,.4f} (체결가 $48.00 역산)")
check("체결되면 회차가 오르고 미체결 목록이 비워진다",
      _restored.pos.turn == 2 and not _restored.pending_orders,
      f"T={_restored.pos.turn} · 미체결 {len(_restored.pending_orders)}건")

# 종가가 상한 위 → 한 주도 안 붙음
_LOC["qty"], _LOC["avg"] = 4.0, 50.0
_LOC["orders"].clear()
_lb2 = _loc_bot()
_lb2._place_loc_orders(price=49.0, chunk_budget=400.0, session="2026-09-21", reason="2회차")
_c2 = _lb2.cash
_lb2._settle_pending_loc()
check("LOC 미체결은 회차를 소진하지 않는다",
      _lb2.pos.turn == 1 and _lb2.cash == _c2 and not _lb2.pending_orders,
      f"T={_lb2.pos.turn} 유지 · 현금 ${_lb2.cash:,.2f} 불변")
_ms_mod.session_date, _ms_mod.loc_window = _orig_sd, _orig_lw

# 익절 시 살아 있는 매수 LOC 를 거둬들이지 않으면 포지션이 되살아난다
_LOC["qty"], _LOC["avg"] = 4.0, 50.0
_LOC["orders"].clear()
_LOC["cancelled"].clear()
_lb3 = _loc_bot()
_lb3._place_loc_orders(price=49.0, chunk_budget=400.0, session="2026-09-21", reason="2회차")
_ids = [o["orderId"] for o in _lb3.pending_orders]
_lb3._exit(price=60.0, reason="목표 익절")
check("익절하면 미체결 매수 LOC 를 전부 취소한다",
      sorted(_LOC["cancelled"]) == sorted(_ids) and not _lb3.pending_orders
      and _lb3.pos.units == 0.0,
      f"취소 {_LOC['cancelled']} — 안 하면 마감 체결로 포지션이 되살아난다")

# ── NH 호출 통로: 조회만 재시도, 주문은 한 번만 ──
#
# 실제로 LOC 접수 직전 잔고 조회가 HTTP 429(IGW42903)로 튕겨 그날 주문을
# 놓쳤다. 조회는 잠깐 쉬었다 다시 하면 풀린다. 주문은 다르다 — 응답을 못
# 받았다고 안 나간 게 아니라서, 다시 내면 같은 주문이 두 번 나갈 수 있다.
class _RL:
    def __init__(self, code, rsp=""):
        self.status_code, self._rsp = code, rsp
        self.text = rsp

    def json(self):
        return {"rsp_cd": self._rsp}


_CALLS = []


def _flaky_post(seq):
    it = iter(seq)

    def f(url, **k):
        _CALLS.append(url)
        return next(it)
    return f


_real_post2 = requests.post

_CALLS.clear()
requests.post = _flaky_post([_RL(429, "IGW42903"), _RL(429, "IGW42903"), _RL(200, "00166")])
_r = namuh._nh_post("https://x/gbstock/inquiry/v1/balance", read=True, timeout=1)
check("조회는 호출 한도에 걸리면 쉬었다가 다시 한다",
      _r.status_code == 200 and len(_CALLS) == 3,
      f"429 → 429 → 200 · 호출 {len(_CALLS)}회")

_CALLS.clear()
requests.post = _flaky_post([_RL(429, "IGW42903"), _RL(200, "00171")])
_r2 = namuh._nh_post("https://x/gbstock/order/v1/buy", read=False, timeout=1)
check("주문은 한도에 걸려도 다시 보내지 않는다 (중복 주문 방지)",
      _r2.status_code == 429 and len(_CALLS) == 1,
      f"호출 {len(_CALLS)}회 — 두 번 보내면 같은 주문이 두 번 체결될 수 있다")

_CALLS.clear()
requests.post = _flaky_post([_RL(200, "00166")] * 3)
_saved_int = namuh._NH_MIN_INTERVAL
namuh._NH_MIN_INTERVAL = 0.15
_t0 = time.monotonic()
for _ in range(3):
    namuh._nh_post("https://x/q", read=True, timeout=1)
_el = time.monotonic() - _t0
namuh._NH_MIN_INTERVAL = _saved_int
check("NH 호출 사이를 최소 간격만큼 띄운다",
      _el >= 0.29, f"3회 호출 {_el:.2f}초 (간격 0.15초 × 2)")

requests.post = _real_post2
_src_nh = open("services/namuh.py", encoding="utf-8").read()
check("NH 로 가는 호출이 모두 통로를 거친다",
      _src_nh.count("requests.post(") == 1,
      "_nh_post 안의 1곳만 남는다 — 새 호출이 통로를 우회하지 못하게")

# ── 매수 증거금 통화: 모의는 달러, 실전은 원화도 ──
#
# 예전에는 늘 1(달러)이었다. 실계좌에 원화만 넣어 두면 달러 증거금 부족으로
# 주문이 거부되고, 그보다 앞서 배포 가드가 달러 예수금만 보고 봇 생성을
# 막았다.
_saved_mock, _saved_pref = os.environ.get("NAMUH_MOCK"), namuh.MARGIN_PREF

os.environ["NAMUH_MOCK"] = "1"
namuh.MARGIN_PREF = "auto"
check("모의계좌는 늘 달러 증거금이다",
      namuh.margin_code(500.0, {"usdAvailable": 0.0}) == namuh.MARGIN_USD,
      "모의 서버는 원화 증거금을 받지 않는다")

os.environ["NAMUH_MOCK"] = "0"
check("실계좌 auto: 달러가 충분하면 달러",
      namuh.margin_code(500.0, {"usdAvailable": 1000.0}) == namuh.MARGIN_USD, "")
check("실계좌 auto: 달러가 모자라면 원화",
      namuh.margin_code(500.0, {"usdAvailable": 100.0}) == namuh.MARGIN_KRW,
      "원화만 넣어 둔 실계좌도 살 수 있다")
namuh.MARGIN_PREF = "usd"
check("NAMUH_MARGIN=usd 면 늘 달러",
      namuh.margin_code(500.0, {"usdAvailable": 0.0}) == namuh.MARGIN_USD, "")

# 매수 여력: 원화는 공시환율로 환산해 센다. 환율을 못 받으면 세지 않는다.
_bal = {"usdAvailable": 100.0, "krwDeposit": 1_380_000.0}
namuh.MARGIN_PREF = "auto"
_bp = namuh.buying_power_usd(_bal, 1380.0)
check("실계좌 매수 여력에 원화를 환산해 더한다",
      _bp["total"] == 1100.0 and _bp["krwCounted"],
      f"$100 + 1,380,000원÷1380 = ${_bp['total']:,.2f}")
_bp2 = namuh.buying_power_usd(_bal, None)
check("환율을 못 받으면 원화를 세지 않는다 (여력을 지어내지 않는다)",
      _bp2["total"] == 100.0 and not _bp2["krwCounted"],
      f"${_bp2['total']:,.2f}")
os.environ["NAMUH_MOCK"] = "1"
_bp3 = namuh.buying_power_usd(_bal, 1380.0)
check("모의계좌 매수 여력은 달러만 센다",
      _bp3["total"] == 100.0, f"${_bp3['total']:,.2f}")

if _saved_mock is None:
    os.environ.pop("NAMUH_MOCK", None)
else:
    os.environ["NAMUH_MOCK"] = _saved_mock
namuh.MARGIN_PREF = _saved_pref

def _nh_acct():
    """NamuhAccount 공장. 위쪽 루프가 _acct 라는 이름을 덮어써서 따로 둔다."""
    a = NamuhAccount("APPKEY", "SECRET", "12345678901")
    a._token = "FAKE"
    a._token_expires_at = time.time() + 9999
    return a


# ── 잔고의 티커는 tck_iem_cd 를 먼저 본다 ──
#
# 계좌에 따라 iem_cd 에 티커가 아닌 식별코드가 들어올 수 있다. 그러면 티커로
# 찾는 거래소 대조가 통째로 어긋난다. 모의계좌는 iem_cd 에 티커가 온다.
def _bal_payload(rows):
    return {"rsp_cd": "00166", "Output_0": {"fc_dca": "1000", "fc_aet_amt": "1000"},
            "Output_1": rows}


_real_post3 = requests.post
requests.post = lambda *a_, **k: _Resp(200, _bal_payload([
    {"tck_iem_cd": "TQQQ", "iem_cd": "US74347X8314", "cns_bse_bnc_qty": "3",
     "sll_pbl_qty1": "3", "fc_phs_uit_pr": "75.05"}]))
_hb = _nh_acct().get_balance(fresh=True)
check("tck_iem_cd 가 있으면 그것을 티커로 쓴다",
      _hb["qtyByTicker"] == {"TQQQ": 3.0},
      f"iem_cd 가 식별코드(US74347X8314)여도 TQQQ 로 잡는다 · {_hb['qtyByTicker']}")

requests.post = lambda *a_, **k: _Resp(200, _bal_payload([
    {"iem_cd": "TQQQ", "cns_bse_bnc_qty": "1", "sll_pbl_qty1": "1", "fc_phs_uit_pr": "75.05"}]))
_hb2 = _nh_acct().get_balance(fresh=True)
check("tck_iem_cd 가 없으면 iem_cd 를 쓴다 (모의계좌 형식)",
      _hb2["qtyByTicker"] == {"TQQQ": 1.0}, f"{_hb2['qtyByTicker']}")
requests.post = _real_post3

# ── 계좌 종류가 모드와 맞는가 (/n2/acctinfo) ──
#
# 응답 모양은 모의계좌에서 실측했다. 모의 03 · 실전(CMA 포함) 01.
def _acct_rows(*pairs):
    return {"rsp_cd": "00000", "rsp_msg": "조회가 완료되었습니다.",
            "Output_0": [{"acct_no": n, "acct_type": t} for n, t in pairs]}


_saved_mock2 = os.environ.get("NAMUH_MOCK")
_a5 = _nh_acct()
_mine = _a5.account_no
_other = "20101794704"

os.environ["NAMUH_MOCK"] = "1"
requests.post = lambda *a_, **k: _Resp(200, _acct_rows((_mine, "03"), (_other, "01")))
_c1 = _a5.check_account_type()
check("모의 모드 + 모의계좌(03) 는 통과한다",
      _c1["verified"] and _c1["ok"] and _c1["type"] == "03", _c1["message"])

os.environ["NAMUH_MOCK"] = "0"
_c2 = _a5.check_account_type()
check("실전 모드에 모의계좌 번호를 넣으면 막는다",
      _c2["verified"] and not _c2["ok"] and "모의투자 계좌" in _c2["message"],
      _c2["message"][:60])

requests.post = lambda *a_, **k: _Resp(200, _acct_rows((_other, "01")))
_c3 = _a5.check_account_type()
check("이 키의 계좌 목록에 없는 번호(오타)는 막는다",
      _c3["verified"] and not _c3["ok"] and "목록에 없습니다" in _c3["message"],
      _c3["message"][:60])

requests.post = lambda *a_, **k: _Resp(500, {"rsp_cd": "IGW9999"})
_c4 = _a5.check_account_type()
check("확인 자체가 실패하면 막지 않는다 (틀렸다고 단정하지 않는다)",
      not _c4["verified"] and _c4["ok"], _c4["message"][:60])

requests.post = lambda *a_, **k: _Resp(200, _acct_rows((_mine, "03")))
_masked = _a5.check_account_type().get("accounts", [])
check("계좌 목록은 가려서 돌려준다",
      all("*" in x for x in _masked) and not any(_mine in x for x in _masked),
      f"{_masked}")

requests.post = _real_post3
if _saved_mock2 is None:
    os.environ.pop("NAMUH_MOCK", None)
else:
    os.environ["NAMUH_MOCK"] = _saved_mock2

# ── 연결 테스트가 토큰을 함부로 재발급하지 않는다 ──
#
# 공식 문서는 '만료 전 재발급 금지' 다. 예전 test_connection 은 매번
# force_refresh=True 로 불러, 실전 봇을 만들 때마다·키를 저장할 때마다
# 새 토큰을 받았다. 이제 유효한 토큰이 있으면 그걸 쓰고, 서버가 그 토큰을
# 거부했을 때만 한 번 새로 받는다.
_TOK = {"issued": 0, "bal": []}


def _route_tc(balance_codes):
    it = iter(balance_codes)

    def f(url, **k):
        if url.endswith("/oauth2/token"):
            _TOK["issued"] += 1
            return _Resp(200, {"access_token": f"NEW{_TOK['issued']}", "expires_in": 86400})
        if url.endswith("/n2/acctinfo"):
            return _Resp(200, {"rsp_cd": "00000", "Output_0": [
                {"acct_no": "12345678901", "acct_type": "03"}]})
        code = next(it)
        _TOK["bal"].append(code)
        if code == 200:
            return _Resp(200, _bal_payload([]))
        if code in (401, 403):
            return _Resp(code, {"rsp_cd": "IGW40011", "rsp_msg": "유효하지 않은 토큰"},
                         text="unauthorized")
        # 토큰과 무관한 서버 장애 (본문에도 토큰 얘기가 없다)
        return _Resp(code, {"rsp_cd": "IGW50000", "rsp_msg": "일시적인 서버 오류"},
                     text="server error")
    return f


_saved_mock3 = os.environ.get("NAMUH_MOCK")
os.environ["NAMUH_MOCK"] = "1"

_TOK.update(issued=0, bal=[])
requests.post = _route_tc([200])
_t1 = _nh_acct().test_connection()                # 유효한 토큰을 이미 들고 있다
check("유효한 토큰이 있으면 연결 테스트가 재발급하지 않는다",
      _t1["success"] and _TOK["issued"] == 0,
      f"토큰 발급 {_TOK['issued']}회 — 만료 전 재발급 금지")

_TOK.update(issued=0, bal=[])
requests.post = _route_tc([401, 200])
_t2 = _nh_acct().test_connection()                # 들고 있던 토큰이 서버에서 거부됨
check("토큰이 거부되면 한 번만 새로 받아 다시 확인한다",
      _t2["success"] and _TOK["issued"] == 1 and _TOK["bal"] == [401, 200],
      f"잔고 {_TOK['bal']} · 발급 {_TOK['issued']}회")

_TOK.update(issued=0, bal=[])
requests.post = _route_tc([500])
_t3 = _nh_acct().test_connection()                # 토큰과 무관한 장애
check("토큰 문제가 아닌 실패에는 재발급하지 않는다",
      not _t3["success"] and _TOK["issued"] == 0,
      f"HTTP 500 · 발급 {_TOK['issued']}회")

requests.post = _real_post3
if _saved_mock3 is None:
    os.environ.pop("NAMUH_MOCK", None)
else:
    os.environ["NAMUH_MOCK"] = _saved_mock3

# ── 일시적 실패가 하루치 접수 기회를 태우지 않는다 ──
#
# LOC 는 하루 한 번, 8분짜리 창에서만 낸다. 그런데 '오늘은 시도했다' 표시를
# 함수 진입 즉시 찍고 있었다. 실제로 접수 직전 잔고 조회가 HTTP 429
# (IGW42903 호출 건수 초과)로 튕기자 창이 8분이나 남았는데도 그날 주문을
# 영영 못 냈다.
_FLAKY = {"n": 0}


class _FlakyBalance:
    configured = True

    def get_balance(self, fresh=False):
        _FLAKY["n"] += 1
        if _FLAKY["n"] == 1:
            raise NamuhError("나무증권 잔고 조회 실패 (HTTP 429)")
        return {"qtyByTicker": {"TQQQ": 0.0}, "holdings": {}}

    def market_buy(self, *a, **k):
        return {"orderId": 970, "units": 0.0, "status": "ACCEPTED"}


_p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4",
                    splitCount=40, locMode="half_half", feePct=0.0)
_lb4 = TradingBot(bot_id="RETRY", coin="TQQQ", interval="1h", mode="LIVE",
                  capital_krw=4000.0, params=_p, broker="namuh")
_lb4.namuh_account = _FlakyBalance()

_lb4._place_loc_orders(price=75.0, chunk_budget=120.0, session="2026-09-23", reason="1회차")
check("잔고 조회가 튕기면 세션을 소진하지 않는다",
      _lb4.loc_session is None and not _lb4.pending_orders,
      "접수 창이 남아 있으면 다시 시도해야 한다")

_lb4._place_loc_orders(price=75.0, chunk_budget=120.0, session="2026-09-23", reason="1회차")
check("재시도에서 접수되고 그때 세션을 소진한다",
      _lb4.loc_session == "2026-09-23" and len(_lb4.pending_orders) == 1,
      f"접수 {len(_lb4.pending_orders)}건 · 잔고 호출 {_FLAKY['n']}회")

# 체결 확인은 캐시를 쓰면 안 된다 (변화를 봐야 한다)
_src = open("services/namuh.py", encoding="utf-8").read()
check("체결 확인용 보유수량 조회는 캐시를 건너뛴다",
      "self.get_balance(fresh=True)" in _src,
      "held_qty 가 캐시를 보면 체결을 영영 못 잡는다")

# ── 청산하지 못한 봇은 지우지 않는다 ──
#
# 미국장이 닫힌 시각에 삭제했더니 매도가 거부됐는데 봇만 사라졌다. 계좌에는
# TQQQ 1주가 남았는데 그걸 아는 봇이 없다 — 익절·손절 감시도, 거래소 대조도
# 걸리지 않는 '고아 물량' 이다.
from services.trader import BotManager, LiquidationFailed    # noqa: E402


class _SellRefuses:
    configured = True

    def market_sell(self, *a, **k):
        raise NamuhError("매도 거부 (테스트)")


def _mgr_with_bot(market_open):
    p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4",
                       splitCount=40, feePct=0.0)
    b = TradingBot(bot_id="DEL", coin="TQQQ", interval="1h", mode="LIVE",
                   capital_krw=4000.0, params=p, broker="namuh")
    b.namuh_account = _SellRefuses()
    b.pos.units, b.pos.entryPrice = 1.0, 75.05
    namuh.market_session = lambda *a_, **k_: {"open": market_open,
                                              "reason": "애프터마켓 진행 중"}
    m = BotManager()
    m.bots = {"DEL": b}
    return m, b


_orig_session = namuh.market_session

_m, _b = _mgr_with_bot(False)
try:
    _m.delete("DEL")
    _refused = False
except LiquidationFailed:
    _refused = True
check("장이 닫혀 있으면 포지션 있는 봇을 지우지 않는다",
      _refused and "DEL" in _m.bots and _b.pos.units == 1.0,
      "지웠다면 계좌에 주인 없는 1주가 남는다")

_m2, _b2 = _mgr_with_bot(True)
try:
    _m2.delete("DEL")
    _refused2 = False
except LiquidationFailed:
    _refused2 = True
check("매도 주문이 거부되면 봇을 지우지 않고 남긴다",
      _refused2 and "DEL" in _m2.bots and _b2.pos.units == 1.0,
      "봇이 남아야 장부와 계좌가 계속 맞는다")

_m3, _b3 = _mgr_with_bot(False)
_b3.pos.units = 0.0
check("팔 물량이 없으면 장이 닫혀 있어도 지워진다",
      _m3.delete("DEL") and not _m3.bots,
      "팔 게 없는데 막으면 봇을 영영 못 지운다")

namuh.market_session = _orig_session

# ── 미국 증시 스케줄: 조기 마감일과 신정 토요일 ──
from services import market_schedule as _ms                  # noqa: E402
from datetime import datetime as _dt, date as _date          # noqa: E402
from zoneinfo import ZoneInfo as _ZI                         # noqa: E402

_ET = _ZI("America/New_York")
_half = _ms.get_nyse_half_days(2026)
check("조기 마감일(13:00 ET)을 안다",
      _date(2026, 11, 27) in _half and _date(2026, 12, 24) in _half,
      "추수감사절 다음 날 · 크리스마스 이브")

_st = _ms.get_us_market_status(_dt(2026, 11, 27, 14, 0, tzinfo=_ET))
check("조기 마감일 14:00 ET 는 장이 닫힌 것으로 본다",
      not _st["isOpen"], f"{_st['status']} — 예전에는 OPEN 이라 주문을 냈다")

# 접수 창은 개장과 함께 열린다. 마감 직전 8분만 열어두면 한 번의 통신
# 오류가 그날 주문을 통째로 날린다 (실제로 HTTP 429 로 그렇게 됐다).
_w_open = _ms.loc_window(_dt(2026, 9, 23, 9, 35, tzinfo=_ET))
_w_mid = _ms.loc_window(_dt(2026, 9, 23, 12, 0, tzinfo=_ET))
_w_late = _ms.loc_window(_dt(2026, 9, 23, 15, 47, tzinfo=_ET))
_w_shut = _ms.loc_window(_dt(2026, 9, 23, 15, 49, tzinfo=_ET))
check("LOC 접수 창이 개장 직후부터 열린다",
      _w_open["in"] and _w_mid["in"] and _w_late["in"],
      f"{_w_open['opensAtEt']}~{_w_open['shutsAtEt']} ET (6시간 이상)")
check("거래소 접수 마감 전에 창을 닫는다",
      not _w_shut["in"] and _w_shut["cutoffEt"] == "15:50",
      "15:48 에 닫는다 — 거래소는 15:50 이후 접수도 취소도 안 받는다")

_st2 = _ms.get_us_market_status(_dt(2026, 11, 27, 11, 0, tzinfo=_ET))
check("조기 마감일 11:00 ET 는 정상 개장이다",
      _st2["isOpen"] and _st2["closeTimeEt"] == "13:00", _st2["reason"])

_st3 = _ms.get_us_market_status(_dt(2026, 9, 22, 14, 0, tzinfo=_ET))
check("평일 14:00 ET 는 그대로 개장이다 (16:00 마감)",
      _st3["isOpen"] and _st3["closeTimeEt"] == "16:00", _st3["reason"])

# 신정이 토요일인 해에는 앞 금요일도 다음 월요일도 쉬지 않는다 (2022년 실제)
check("신정이 토요일이면 대체휴일을 만들지 않는다",
      _date(2027, 12, 31) not in _ms.get_nyse_holidays(2027)
      and _date(2028, 1, 3) not in _ms.get_nyse_holidays(2028),
      "2021-12-31 금 개장 · 2022-01-03 월 개장 (실제 NYSE)")

# 2026 정규 휴장일 10개는 그대로여야 한다
_h26 = _ms.get_nyse_holidays(2026)
_official = {_date(2026, 1, 1), _date(2026, 1, 19), _date(2026, 2, 16), _date(2026, 4, 3),
             _date(2026, 5, 25), _date(2026, 6, 19), _date(2026, 7, 3), _date(2026, 9, 7),
             _date(2026, 11, 26), _date(2026, 12, 25)}
check("2026 NYSE 휴장일 10개가 공식과 일치한다",
      set(_h26) == _official, f"{len(_h26)}개 · 차이 {sorted(set(_h26) ^ _official)}")

print(f"\n{'=' * 58}")
if FAIL:
    print(f"통과 {len(PASS)}개 · 실패 {len(FAIL)}개")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print(f"전체 {len(PASS)}개 항목 통과")
