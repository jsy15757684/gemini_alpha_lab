"""나무증권 국내주식 (KRX) — 시세 · 잔고 · 지정가 주문.

해외(gbstock)와 API 가 완전히 다르다 (krstock). 필드 이름도 다르다.
호출 형태는 nhplug-auto-trader-main 의 broker.py 와 서버에서 직접 받아 본
응답(2026-09-28, 187개 필드)으로 맞췄다.

  시세   POST {실서버}/krstock/quote/v1/currentPrice   (모의도 시세는 실서버)
  잔고   POST {주문서버}/krstock/inquiry/v1/balance
  주문   POST {주문서버}/krstock/order/v1/cashBuy · cashSell   (지정가 01 만 쓴다)

토큰 · 호출 간격(1.1초) · 재시도 규칙은 해외와 같은 통로(namuh._nh_post)를
쓴다. 그래서 국내와 해외는 반드시 한 프로세스에 있어야 한다.

시장가는 쓰지 않는다. 모의투자가 지정가만 받고(해외에서 실측), 지정가는
화면에서 본 값보다 불리하게 체결되지 않는다. 즉시 체결이 필요할 때는
최우선 호가에서 몇 호가 더 불리한 '시장성 지정가' 를 건다.
"""

import logging
import math
import threading
import time
from typing import Any, Dict, Optional

from services import namuh
from services.namuh import NamuhError, _nh_post

logger = logging.getLogger(__name__)

# ORB 는 장 초반 유동성이 두터운 종목에서만 의미가 있다. 호가가 얇으면
# 돌파 신호 자체가 한두 건의 주문으로 만들어진다. 목록을 좁게 둔다.
KRX_STOCKS: Dict[str, Dict[str, Any]] = {
    "069500": {"name": "KODEX 200", "etf": True, "index": "kospi"},
    "122630": {"name": "KODEX 레버리지", "etf": True, "index": "kospi"},
    "233740": {"name": "KODEX 코스닥150레버리지", "etf": True, "index": "kosdaq"},
    "005930": {"name": "삼성전자", "etf": False, "index": "kospi"},
    "000660": {"name": "SK하이닉스", "etf": False, "index": "kospi"},
}

# 지수 필터에 쓰는 지수 추종 ETF. 나무증권에서 지수 시세 경로를 찾지 못했다.
INDEX_PROXY = {"kospi": ("069500", "코스피(KODEX 200)"), "kosdaq": ("229200", "코스닥(KODEX 코스닥150)")}

# 모의투자 장부와 매도 손익 추정에만 쓰는 값이다. 실제 세율은 해마다 바뀌어
# 높은 쪽(0.20%)으로 잡는다. ETF 는 증권거래세가 없다.
SELL_TAX_PCT = 0.20
FEE_PCT = 0.015         # 나무증권 온라인 수수료 (대략값)


def is_krx(code: str) -> bool:
    return str(code or "").strip() in KRX_STOCKS


def tick_size(price: float, etf: bool) -> int:
    """KRX 호가 단위 (2023-01 개편). ETF·ETN 은 2,000원 미만 1원, 이상 5원."""
    if etf:
        return 1 if price < 2000 else 5
    for bound, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                        (200000, 100), (500000, 500)):
        if price < bound:
            return tick
    return 1000


def round_tick(price: float, etf: bool, up: bool) -> int:
    """호가 단위에 맞춘다. 매수는 올림(up), 매도는 내림 — 둘 다 체결 쪽으로."""
    t = tick_size(price, etf)
    n = price / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return int(n * t)


def shift_ticks(price: float, etf: bool, ticks: int) -> int:
    """price 에서 호가 ticks 칸 이동한 값. 경계(예: 2,000원)를 넘으면 단위가 바뀐다."""
    p = int(price)
    step = 1 if ticks > 0 else -1
    for _ in range(abs(ticks)):
        p += step * tick_size(p if step > 0 else p - 1, etf)
    return p


def _i(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _post(acc: namuh.NamuhAccount, base: str, path: str, inp: Dict[str, Any], *, read: bool):
    if not acc.configured:
        raise NamuhError("나무증권 계정 키가 설정되지 않았습니다.")
    try:
        res = _nh_post(f"{base}{path}", read=read, headers=acc._headers(),
                       json={"Input_0": inp}, timeout=10)
        body = res.json()
    except NamuhError:
        raise
    except Exception as e:
        raise NamuhError(f"나무증권 국내 {path.rsplit('/', 1)[-1]} 통신 오류: {e}")
    return res, body


def quote(acc: namuh.NamuhAccount, code: str) -> Dict[str, Any]:
    """국내 현재가 한 번에. 누적 거래량 · 가중평균가(당일 VWAP) · 최우선 호가까지."""
    code = str(code).strip()
    res, b = _post(acc, namuh.BASE_URL, "/krstock/quote/v1/currentPrice",
                   {"iem_cd": code, "market_cd": "KRX"}, read=True)
    o = b.get("Output_0") or {}
    price = _i(o.get("stck_prpr"))
    if res.status_code != 200 or price <= 0:
        raise NamuhError(f"국내 시세 조회 실패 ({code} · {b.get('rsp_cd')}): "
                         f"{b.get('rsp_msg') or (res.text or '')[:120]}")
    vol = _i(o.get("acml_vol"))
    # 가중평균가 필드가 비면 누적 거래대금(백만원)/누적 거래량으로 낸다.
    vwap = _f(o.get("wghn_avrg_prc")) or (_f(o.get("acml_tr_pbmn")) * 1_000_000 / vol if vol else 0.0)
    grp = str(o.get("scrt_grp_isnm") or "").strip()
    return {
        "code": code,
        "name": str(o.get("iem_nm") or code).strip().lstrip("*#").strip(),
        "price": price,
        "open": _i(o.get("stck_oprc")),
        "high": _i(o.get("stck_hgpr")),
        "low": _i(o.get("stck_lwpr")),
        "prevClose": _i(o.get("stck_prdy_clpr")),
        "bid": _i(o.get("bidp1") or o.get("bidp")),
        "ask": _i(o.get("askp1") or o.get("askp")),
        "upperLimit": _i(o.get("stck_mxpr")),
        "lowerLimit": _i(o.get("stck_llam")),
        "volume": vol,
        "vwap": round(vwap, 2),
        "hogaTime": str(o.get("hoga_bsop_hour") or "").strip(),   # "HH:MM:SS" (한국시간)
        "etf": grp in ("ETF", "ETN") or KRX_STOCKS.get(code, {}).get("etf", False),
        "changePct": _f(o.get("prdy_ctrt")),
        "fetchedAt": time.time(),
    }


_BAL_CACHE: Dict[str, tuple] = {}
_BAL_LOCK = threading.Lock()
_BAL_TTL = 5.0


def balance(acc: namuh.NamuhAccount, fresh: bool = False) -> Dict[str, Any]:
    """국내 예수금과 보유. {cash, holdings: {code: {qty, avg, price, name}}}"""
    if not acc.account_no:
        raise NamuhError("나무증권 계좌번호가 설정되지 않았습니다.")
    ck = acc.account_no
    if not fresh:
        with _BAL_LOCK:
            hit = _BAL_CACHE.get(ck)
            if hit and time.time() - hit[0] < _BAL_TTL:
                return dict(hit[1])
    res, b = _post(acc, namuh.trade_base_url(), "/krstock/inquiry/v1/balance",
                   {"act_no": acc.account_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
                    "aet_bse": "2", "qut_dit_cd": "UNT"}, read=True)
    if res.status_code != 200 or "Output_0" not in b:
        raise NamuhError(f"국내 잔고 조회 실패 ({b.get('rsp_cd')}): "
                         f"{b.get('rsp_msg') or (res.text or '')[:120]}")
    holdings = {}
    for row in b.get("Output_1") or []:
        code = str(row.get("iem_cd") or "").strip()
        # 수량 칸이 셋이다 (모의계좌 실측 2026-09-28, 1주 사고 바로 판 뒤):
        #   rsdl_qty     지금 보유 — 사면 1, 팔면 0 (이것이 체결 확인의 기준)
        #   ny_stl_qty   미결제 — 산 1주가 이틀 뒤 결제까지 남는다. 매도분은
        #                종목코드 없는 -1 행으로 따로 온다
        #   itg_bnc_qty  결제 잔고 — 당일 산 것은 0
        # broker.py 는 '셋 중 가장 큰 값' 을 썼는데, 그러면 판 뒤에도 1주가
        # 있는 것으로 보인다(미결제 1). 봇이 판 주식을 계속 들고 있다고 믿게 된다.
        if "rsdl_qty" in row:
            qty = _i(row.get("rsdl_qty"))
        else:
            qty = max(_i(row.get("itg_bnc_qty")), _i(row.get("ny_stl_qty")))
        if not code:
            continue
        # sll_amt · sll_pls_amt 는 '지금 팔면 받을 평가 금액 · 손익' 이다. 체결된
        # 매도 금액이 아니다 — 두 번째 매도 뒤 오히려 58원 줄었다(2026-09-28).
        # 그래서 매도 체결가는 이것으로 내지 않는다.
        if qty > 0:
            holdings[code] = {"qty": qty, "avg": _f(row.get("phs_pr")), "price": _i(row.get("now_pr")),
                              "name": str(row.get("iem_nm") or code).strip().lstrip("*#").strip()}
    o0 = b.get("Output_0") or {}
    # dca(예수금)는 당일 매수를 빼지 않는다(결제 기준). 주문가능금액이 맞다.
    cash = _i(o0.get("orr_pbl_amt1")) or _i(o0.get("dca"))
    out = {"cash": cash, "deposit": _i(o0.get("dca")), "holdings": holdings}
    with _BAL_LOCK:
        _BAL_CACHE[ck] = (time.time(), dict(out))
    return out


def sellable(acc: namuh.NamuhAccount, code: str) -> int:
    res, b = _post(acc, namuh.trade_base_url(), "/krstock/inquiry/v1/sellableQuantity",
                   {"act_no": acc.account_no, "iem_cd": code, "cfd_lon_cd": "00"}, read=True)
    return _i((b.get("Output_0") or {}).get("sll_pbl_qty"))


def order(acc: namuh.NamuhAccount, side: str, code: str, qty: int, limit: int) -> str:
    """지정가 주문 한 건. 접수 번호를 돌려준다. **재시도하지 않는다.**"""
    if qty < 1 or limit <= 0:
        raise NamuhError(f"주문 수량·가격이 올바르지 않습니다 ({qty}주 @ {limit}원)")
    path = "/krstock/order/v1/cashBuy" if side == "buy" else "/krstock/order/v1/cashSell"
    res, b = _post(acc, namuh.trade_base_url(), path, {
        "act_no": acc.account_no, "iem_cd": code, "orr_qty": int(qty),
        "nmn_pr_tp_cd": "01",               # 지정가
        "orr_pr": int(limit),
        "orr_cnd_dit_cd": "00", "ssl_nmn_pr_dit_cd": "00",
        "rmt_mkt_cd": "KRX", "sor_mkt_sli_yn": "N",
    }, read=False)
    out = b.get("Output_0") or {}
    order_no = str(out.get("mkt_orr_no") or out.get("orr_no") or "").strip()
    if res.status_code != 200 or not order_no:
        raise NamuhError(f"국내 {'매수' if side == 'buy' else '매도'} 주문 거부 ({b.get('rsp_cd')}): "
                         f"{b.get('rsp_msg') or (res.text or '')[:160]}", b)
    with _BAL_LOCK:
        _BAL_CACHE.pop(acc.account_no, None)
    return order_no


def await_fill(acc: namuh.NamuhAccount, code: str, qty_before: int, want: int,
               side: str, wait_sec: float = 8.0) -> Dict[str, Any]:
    """접수 뒤 잔고 변화로 체결을 확인한다. {filled, avgAfter, qtyAfter}"""
    t0 = time.time()
    last = None
    while time.time() - t0 < wait_sec:
        time.sleep(1.5)
        try:
            bal = balance(acc, fresh=True)
        except NamuhError as e:
            last = e
            continue
        h = bal["holdings"].get(code) or {}
        now = int(h.get("qty") or 0)
        filled = (now - qty_before) if side == "buy" else (qty_before - now)
        if filled >= want:
            return {"filled": want, "qtyAfter": now, "avgAfter": float(h.get("avg") or 0)}
        last = {"filled": max(0, filled), "qtyAfter": now, "avgAfter": float(h.get("avg") or 0)}
    if isinstance(last, dict):
        return last
    raise NamuhError(f"체결 확인 중 잔고를 받지 못했습니다: {last}")
