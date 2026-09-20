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
check("기본 주문이 시장가(03)다",
      inp.get("ahi_nmn_pr_tp_cd") == "03" and "fc_orr_uit_pr" not in inp,
      "시장가라 단가를 보내지 않는다")
check("매수에는 증거금통화종류코드를 보낸다", inp.get("wtm_cur_knd_cd") == "1", "1.해당통화")

_ORDERS.clear()
a9 = _acct(); a9._await_fill = lambda *a_, **k: 5.0
namuh._price_cache["TQQQ"] = (time.time(), 75.50)
a9.market_buy("TQQQ", 400.0, order_type=namuh.ORD_LOC)
inp = (_ORDERS[-1][1] or {}).get("Input_0", {})
check("LOC(12) 등 지정가 계열은 단가를 함께 보낸다",
      inp.get("ahi_nmn_pr_tp_cd") == "12" and inp.get("fc_orr_uit_pr") == 75.50,
      f"유형 12 · 단가 ${inp.get('fc_orr_uit_pr')}")

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
