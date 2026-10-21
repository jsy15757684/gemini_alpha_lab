"""나무증권 국내주식 (KRX) 시세 — 국내 스캘퍼(모의)용. **주문 기능은 없다.**

2026-10-06 에 지운 ORB 봇의 krx.py 에서 시세 부분만 되살렸다 (주문 · 잔고 · 실시간은 뺐다).
  시세   POST {실서버}/krstock/quote/v1/currentPrice   (2026-10-21 실계좌 키로 확인)

토큰 · 호출 간격(1.1초) · 재시도 규칙은 해외와 같은 통로(namuh._nh_post)를 쓴다 —
그래서 해외 봇과 같은 프로세스(나무증권 워커)에서 돈다.
"""

import logging
import time
from typing import Any, Dict

from services import namuh
from services.namuh import NamuhError, _nh_post

logger = logging.getLogger(__name__)

MAX_WATCH = 28          # 선정기(kr_selector)가 한 번에 고르는 상한

# 모의 장부에서 매도 손익에 빼는 비용(%) — 증권거래세 + 수수료, 높은 쪽으로 잡는다.
SELL_COST_PCT = 0.20


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


def quote(acc: namuh.NamuhAccount, code: str) -> Dict[str, Any]:
    """국내 현재가 한 번에 — 시가 · 고가 · 최우선 호가 · 동시호가 예상체결까지."""
    if not acc.configured:
        raise NamuhError("나무증권 계정 키가 설정되지 않았습니다.")
    code = str(code).strip()
    try:
        res = _nh_post(f"{namuh.BASE_URL}/krstock/quote/v1/currentPrice", read=True, headers=acc._headers(),
                       json={"Input_0": {"iem_cd": code, "market_cd": "KRX"}}, timeout=10)
        b = res.json()
    except NamuhError:
        raise
    except Exception as e:
        raise NamuhError(f"나무증권 국내 시세 통신 오류: {e}")
    o = b.get("Output_0") or {}
    price = _i(o.get("stck_prpr"))
    if res.status_code != 200 or price <= 0:
        raise NamuhError(f"국내 시세 조회 실패 ({code} · {b.get('rsp_cd')}): "
                         f"{b.get('rsp_msg') or (res.text or '')[:120]}")
    prev_close = _i(o.get("stck_prdy_clpr"))
    # 동시호가 예상체결 (Output_2) — 장 전 08:30~09:00 에만 온다. 없으면 0.
    o2 = b.get("Output_2") or {}
    exp_price = _i(o2.get("antc_cnpr"))
    # 등락률 칸은 부호 없이 오는 일이 있어(ORB 실측) 전일 종가로 직접 낸다.
    exp_pct = (exp_price / prev_close - 1) * 100 if exp_price > 0 and prev_close > 0 else _f(o2.get("antc_prdy_ctrt"))
    chg = (price / prev_close - 1) * 100 if prev_close > 0 else _f(o.get("prdy_ctrt"))
    return {
        "code": code,
        "name": str(o.get("iem_nm") or code).strip().lstrip("*#").strip(),
        "price": price,
        "open": _i(o.get("stck_oprc")),
        "high": _i(o.get("stck_hgpr")),
        "low": _i(o.get("stck_lwpr")),
        "prevClose": prev_close,
        "changePct": round(chg, 2),
        "bid": _i(o.get("bidp1") or o.get("bidp")),
        "ask": _i(o.get("askp1") or o.get("askp")),
        # 10단계 호가 [(가격, 잔량)] — 모의 체결가를 잔량을 먹어 내려가며 낸다
        "asks": [(_i(o.get(f"askp{i}")), _i(o.get(f"askp_rsqn{i}"))) for i in range(1, 11) if _i(o.get(f"askp{i}")) > 0],
        "bids": [(_i(o.get(f"bidp{i}")), _i(o.get(f"bidp_rsqn{i}"))) for i in range(1, 11) if _i(o.get(f"bidp{i}")) > 0],
        # 정적 VI 발동 예정 가격 (상단 · 하단 · 기준). 동적 VI 는 오지 않는다. 0 이면 모름
        "viUp": _i(o.get("vi_antc_mxpr")),
        "totalAsk": _i(o.get("total_askp_rsqn")),     # 총 매도잔량 (10단계 합)
        "totalBid": _i(o.get("total_bidp_rsqn")),     # 총 매수잔량
        "viDown": _i(o.get("vi_antc_llam")),
        "volume": _i(o.get("acml_vol")),
        "prevVolume": _i(o.get("prdy_vol")),
        "expPrice": exp_price,
        "expChangePct": round(exp_pct, 2),
        "expVolume": _i(o2.get("antc_vol")),
        "hogaTime": str(o.get("hoga_bsop_hour") or "").strip(),   # "HH:MM:SS" (한국시간)
        "fetchedAt": time.time(),
    }
