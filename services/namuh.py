"""나무증권(NH투자증권) Namuh PLUG REST OpenAPI 클라이언트.

지원: 미국 주식(TQQQ, SOXL 등) 시세 조회, 예수금 잔고 조회, 매수/매도 주문.
엔드포인트: https://api.nhplug.com:8443
인증: OAuth 2.0 (appkey + appsecretkey -> Bearer Access Token, 24시간 유효)
"""

import os
import time
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.nhplug.com:8443"

# 라오어 무한매수법 대표 지원 미국 주식 ETF 및 메이저 종목
NAMUH_STOCKS: Dict[str, Dict[str, str]] = {
    "TQQQ": {"name": "ProShares UltraPro QQQ (나스닥 3배)", "market": "NASDAQ", "leverage": "3x", "currency": "USD"},
    "SOXL": {"name": "Direxion Daily Semiconductor Bull 3X (반도체 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "UPRO": {"name": "ProShares UltraPro S&P500 (S&P500 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "TECL": {"name": "Direxion Daily Technology Bull 3X (기술주 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "FNGU": {"name": "MicroSectors FANG+ 3X (빅테크 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "NVDA": {"name": "NVIDIA (엔비디아)", "market": "NASDAQ", "leverage": "1x", "currency": "USD"},
    "AAPL": {"name": "Apple (애플)", "market": "NASDAQ", "leverage": "1x", "currency": "USD"},
    "TSLA": {"name": "Tesla (테슬라)", "market": "NASDAQ", "leverage": "1x", "currency": "USD"},
}


def _us_eastern_now(now_utc: Optional[datetime] = None) -> Tuple[datetime, bool]:
    """미국 동부 시각과 서머타임 여부. zoneinfo 없이 규칙으로 계산한다.

    서머타임: 3월 둘째 일요일 02:00 ~ 11월 첫째 일요일 02:00 (현지 기준).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    y = now_utc.year

    def nth_sunday(month: int, n: int) -> datetime:
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        # 그 달의 첫 일요일
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    dst_start = nth_sunday(3, 2) + timedelta(hours=7)    # 02:00 EST = 07:00 UTC
    dst_end = nth_sunday(11, 1) + timedelta(hours=6)     # 02:00 EDT = 06:00 UTC
    is_dst = dst_start <= now_utc < dst_end
    return now_utc + timedelta(hours=-4 if is_dst else -5), is_dst


def market_session(now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """미국 정규장이 열려 있는지. 주문을 낼 수 있는 시간인지 판정한다.

    정규장 09:30~16:00 ET, 월~금. 공휴일은 이 함수가 알지 못한다 —
    그 경우 주문이 거부되거나 체결되지 않고, 체결 확인 단계에서 걸린다.
    """
    et, is_dst = _us_eastern_now(now_utc)
    weekday = et.weekday() < 5
    mins = et.hour * 60 + et.minute
    open_now = weekday and (9 * 60 + 30) <= mins < (16 * 60)
    return {
        "open": open_now,
        "etTime": et.strftime("%Y-%m-%d %H:%M"),
        "tz": "EDT" if is_dst else "EST",
        "reason": "" if open_now else (
            "주말 (미국 정규장 휴장)" if not weekday else
            f"정규장 시간 밖 (09:30~16:00 ET · 현재 {et.strftime('%H:%M')} "
            f"{'EDT' if is_dst else 'EST'})"),
    }


class NamuhError(Exception):
    """나무증권 API 통신 및 주문 오류."""
    def __init__(self, message: str, raw: Any = None):
        super().__init__(message)
        self.message = message
        self.raw = raw


# ───────────────────────── 시세 캐시 ─────────────────────────
_price_cache: Dict[str, tuple] = {}
_price_lock = threading.Lock()
_PRICE_TTL = 3.0  # 초


class NamuhAccount:
    """나무증권 Namuh PLUG 계정 클라이언트."""

    def __init__(self, app_key: str = "", app_secret: str = "", account_no: str = ""):
        self.app_key = str(app_key or "").strip()
        self.app_secret = str(app_secret or "").strip()
        # 계좌번호: 앞 8자리 또는 8자리-2자리(지점/일련번호)
        self.account_no = str(account_no or "").strip().replace("-", "")
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def masked_key(self) -> str:
        k = self.app_key
        return (k[:4] + "******" + k[-4:]) if len(k) > 8 else ("******" if k else "")

    def masked_account(self) -> str:
        acc = self.account_no
        return (acc[:3] + "****" + acc[7:]) if len(acc) >= 8 else (acc or "")

    def get_token(self, force_refresh: bool = False) -> str:
        """24시간 유효한 OAuth2 Access Token 발급 및 자동 갱신."""
        with self._token_lock:
            now = time.time()
            if not force_refresh and self._token and now < (self._token_expires_at - 300):
                return self._token

            if not self.configured:
                raise NamuhError("나무증권 APPKEY 또는 Secret Key가 설정되지 않았습니다.")

            url = f"{BASE_URL}/oauth2/token"
            data = {
                "appkey": self.app_key,
                "appsecretkey": self.app_secret,
                "grant_type": "client_credentials",
                "scope": "oob",
            }
            try:
                res = requests.post(url, data=data, timeout=10)
                body = res.json()
            except Exception as e:
                raise NamuhError(f"나무증권 토큰 발급 통신 오류: {e}")

            if res.status_code != 200 or "access_token" not in body:
                err_msg = body.get("error_description") or body.get("message") or res.text
                raise NamuhError(f"나무증권 인증 실패: {err_msg}", body)

            self._token = body["access_token"]
            expires_in = float(body.get("expires_in") or 86400)
            self._token_expires_at = now + expires_in
            logger.info("나무증권 OAuth2 토큰 발급/갱신 완료")
            return self._token

    def _headers(self, tr_id: str = "") -> Dict[str, str]:
        token = self.get_token()
        h = {
            "Authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "Content-Type": "application/json; charset=UTF-8",
        }
        if tr_id:
            h["tr_id"] = tr_id
        return h

    def test_connection(self) -> Dict[str, Any]:
        """API Key 유효성 및 계좌 연결 테스트."""
        if not self.configured:
            return {"success": False, "message": "APPKEY와 Secret Key를 모두 입력하세요."}
        try:
            token = self.get_token(force_refresh=True)
            bal = self.get_balance()
            return {
                "success": True,
                "message": f"나무증권 연결 성공 (USD 예수금: ${bal.get('usdAvailable', 0):,.2f})",
                "balance": bal,
            }
        except NamuhError as e:
            return {"success": False, "message": e.message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_balance(self) -> Dict[str, Any]:
        """해외주식 잔고 및 예수금(USD/KRW) 조회."""
        if not self.configured:
            raise NamuhError("나무증권 계정 키가 설정되지 않았습니다.")

        # 계좌번호 분리 (앞 8자리 + 뒤 2자리)
        acc_prefix = self.account_no[:8] if len(self.account_no) >= 8 else self.account_no
        acc_suffix = self.account_no[8:10] if len(self.account_no) >= 10 else "01"

        endpoint = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = self._headers(tr_id="TTTS3012R")  # 해외주식 잔고 조회 TR
        params = {
            "CANO": acc_prefix,
            "ACNT_PRDT_CD": acc_suffix,
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        try:
            res = requests.get(endpoint, headers=headers, params=params, timeout=10)
            if res.status_code != 200:
                # 예전에는 여기서 '성공 · 예수금 $10,000 · 보유 없음' 을 돌려줬다.
                # 서버가 거부했는데 없는 돈을 만들어내는 셈이라, LIVE 가동
                # 전 잔고 확인과 거래소 대조가 통째로 무력화된다.
                body_txt = (res.text or "")[:200]
                raise NamuhError(
                    f"나무증권 잔고 조회 실패 (HTTP {res.status_code}): {body_txt}")
            body = res.json()
            output1 = body.get("output1", [])
            output2 = body.get("output2", {})

            usd_avail = float(output2.get("ovrs_ord_psbl_amt", 0.0) or output2.get("frcr_dncl_amt_2", 0.0))
            usd_total = float(output2.get("tot_evlu_pfls_amt", 0.0) or usd_avail)

            holdings: Dict[str, Dict[str, Any]] = {}
            for item in output1:
                ticker = item.get("ovrs_pdno", "").strip().upper()
                qty = float(item.get("ovrs_cblc_qty", 0.0))
                avg_price = float(item.get("pchs_avg_pric", 0.0))
                eval_amt = float(item.get("ovrs_stck_evlu_amt", 0.0))
                if qty > 0:
                    holdings[ticker] = {
                        "qty": qty,
                        "avgPrice": avg_price,
                        "evalAmountUsd": eval_amt,
                        "currency": "USD",
                    }

            return {
                "success": True,
                "usdAvailable": usd_avail,
                "usdTotal": usd_total,
                # 종목 → 상세. 대조에 쓰는 '종목 → 수량' 은 아래에 따로 둔다.
                # 예전에는 이 dict 를 소비부가 list 로 순회해(AttributeError)
                # 거래소 대조가 통째로 동작하지 않았다. 형식을 한 곳에서 정한다.
                "holdings": holdings,
                "qtyByTicker": {t: v["qty"] for t, v in holdings.items()},
                "accountNo": self.masked_account(),
            }
        except NamuhError:
            raise
        except Exception as e:
            # 통신 예외도 가짜 잔고로 덮지 않는다. 모르는 것은 모른다고 한다.
            raise NamuhError(f"나무증권 잔고 조회 통신 오류: {e}")

    def get_price(self, ticker: str) -> float:
        """미국 주식 현재가(USD) 조회."""
        sym = ticker.upper().strip()
        with _price_lock:
            now = time.time()
            if sym in _price_cache:
                t, p = _price_cache[sym]
                if now - t < _PRICE_TTL and p > 0:
                    return p

        # 1) Namuh PLUG 해외주식 현재가 TR 조회
        if self.configured:
            try:
                headers = self._headers(tr_id="HHDFS00000300")
                market = NAMUH_STOCKS.get(sym, {}).get("market", "NASD")
                if market == "NASDAQ":
                    market = "NAS"
                elif market == "NYSE":
                    market = "NYS"

                endpoint = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price"
                params = {"AUTH": "", "EXCD": market, "SYMB": sym}
                res = requests.get(endpoint, headers=headers, params=params, timeout=6)
                if res.status_code == 200:
                    d = res.json().get("output", {})
                    price = float(d.get("last") or d.get("ovrs_nmix_prpr", 0.0))
                    if price > 0:
                        with _price_lock:
                            _price_cache[sym] = (time.time(), price)
                        return price
            except Exception as e:
                logger.warning(f"나무증권 시세 조회 예외 ({sym}): {e}")

        # 2) 공개 무료 시세 Fallback (Yahoo Finance 등 실시간 REST)
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
            h = {"User-Agent": "Mozilla/5.0"}
            res = requests.get(url, headers=h, timeout=5)
            if res.status_code == 200:
                data = res.json()
                price = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
                if price > 0:
                    with _price_lock:
                        _price_cache[sym] = (time.time(), price)
                    return price
        except Exception as e:
            logger.warning(f"공개 시세 수신 실패 ({sym}): {e}")

        # 3) 둘 다 실패하면 값을 지어내지 않는다.
        #
        # 예전에는 여기서 {"TQQQ": 75.50, ...} 같은 기본값을 돌려주고 캐시에까지
        # 넣었다. 그러면 통신이 끊긴 상태에서도 봇은 정상 시세로 알고 매수·익절·
        # 손절을 판단한다. 이 프로젝트에서 같은 종류의 버그를 두 번 고쳤다
        # (환율 1385.0, 바이낸스 BTC $65,000). 세 번은 없다.
        #
        # 봇 루프는 시세 예외를 이미 '판단 보류' 로 처리한다(trader.py). 여기서
        # 예외를 올려야 그 안전장치가 작동한다.
        raise NamuhError(
            f"{sym} 현재가를 받지 못했습니다 (나무증권·공개시세 모두 실패). "
            f"추정치로 매매하지 않습니다.")

    def get_candles(self, ticker: str, interval: str = "24h", limit: int = 100) -> List[Dict[str, Any]]:
        """미국 주식 캔들 데이터 수신."""
        sym = ticker.upper().strip()
        # 공개 야후 파이낸스 차트 API로 표준 일봉/시간봉 수신
        range_str = "60d" if interval in ("1h", "6h", "24h") else "5d"
        interval_str = "1d" if interval in ("12h", "24h") else ("60m" if interval in ("1h", "6h") else "15m")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range_str}&interval={interval_str}"
        candles: List[Dict[str, Any]] = []
        try:
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if res.status_code == 200:
                result = res.json()["chart"]["result"][0]
                timestamps = result.get("timestamp", [])
                quote = result["indicators"]["quote"][0]
                for idx, t in enumerate(timestamps):
                    o = quote["open"][idx]
                    h = quote["high"][idx]
                    l = quote["low"][idx]
                    c = quote["close"][idx]
                    v = quote["volume"][idx]
                    if None not in (o, h, l, c) and c > 0:
                        candles.append({
                            "time": int(t * 1000),
                            "open": float(o),
                            "high": float(h),
                            "low": float(l),
                            "close": float(c),
                            "volume": float(v or 0.0),
                        })
        except Exception as e:
            logger.warning(f"미국 주식 캔들 수신 예외 ({sym}): {e}")

        if not candles:
            # 합성 캔들을 만들지 않는다.
            #
            # 예전에는 현재가로 limit 개의 가짜 봉(고가 ×1.01, 저가 ×0.99)을
            # 찍어냈다. 그 위에서 RSI·MACD·이동평균이 계산되므로, 지표가
            # 가리키는 것이 시장이 아니라 만들어낸 숫자가 된다. 봇 루프는
            # 캔들 예외를 '판단 보류' 로 처리한다 — 그쪽이 맞다.
            raise NamuhError(
                f"{sym} 캔들을 받지 못했습니다 ({interval}). 합성 데이터로 "
                f"지표를 계산하지 않습니다.")
        return candles[-limit:] if limit else candles

    def held_qty(self, ticker: str) -> float:
        """계좌의 실제 보유 주수. 체결 확인에 쓴다."""
        return float(self.get_balance().get("qtyByTicker", {}).get(ticker.upper().strip(), 0.0))

    def _require_market_open(self, what: str) -> None:
        ses = market_session()
        if not ses["open"]:
            raise NamuhError(
                f"미국 정규장이 열려 있지 않아 {what}를 보류합니다 — {ses['reason']}. "
                f"닫힌 장에 낸 주문은 체결되지 않는데 장부에는 남을 수 있습니다.")

    def market_buy(self, ticker: str, amount_usd: float) -> Dict[str, Any]:
        """미국 주식 매수 (라오어 무한매수 금액 기준 주문)."""
        sym = ticker.upper().strip()
        price = self.get_price(sym)
        if price <= 0:
            raise NamuhError(f"현재가를 조회할 수 없습니다: {sym}")

        # 배정액을 넘지 않게 **내림**한다.
        #
        # 예전에는 max(1, int(...)) 라, 1회 배정액이 1주 값보다 작으면 무조건
        # 1주를 샀다. 40분할로 $1,000 을 굴리면 1회 $25 인데 TQQQ 1주 $75 가
        # 나가 13회차에 자금이 바닥난다. 분할매수의 전제가 깨진다.
        qty = int(amount_usd // price)
        if qty < 1:
            raise NamuhError(
                f"1회 배정액 ${amount_usd:,.2f} 이 {sym} 1주 값 ${price:,.2f} 보다 "
                f"작아 매수하지 않습니다. 분할수를 줄이거나 운용자본을 늘리세요.")
        actual_amt = qty * price

        if not self.configured:
            logger.info(f"[모의 주문] 나무증권 매수 접수: {sym} {qty}주 @ ${price:,.2f} (${actual_amt:,.2f})")
            return {
                "orderId": f"MOCK-BUY-{int(time.time()*1000)}",
                "ticker": sym,
                "units": float(qty),
                "price": price,
                "amountUsd": actual_amt,
                "status": "FILLED",
                "orderType": "LOC",
            }

        # 실주문 전에 장이 열려 있는지 본다.
        self._require_market_open("매수")
        qty_before = self.held_qty(sym)

        # 실주문 API (Namuh PLUG 해외주식 주문)
        acc_prefix = self.account_no[:8]
        acc_suffix = self.account_no[8:10] if len(self.account_no) >= 10 else "01"
        endpoint = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
        headers = self._headers(tr_id="TTTT1002U")  # 해외주식 매수주문 TR
        body = {
            "CANO": acc_prefix,
            "ACNT_PRDT_CD": acc_suffix,
            "OVRS_EXCG_CD": NAMUH_STOCKS.get(sym, {}).get("market", "NASD")[:4],
            "PDNO": sym,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # 지정가 (LOC 주문 코드는 34 등 시장별 매핑)
        }
        try:
            res = requests.post(endpoint, headers=headers, json=body, timeout=10)
            res_data = res.json()
            if res.status_code != 200 or res_data.get("rt_cd") != "0":
                msg = res_data.get("msg1") or res.text
                raise NamuhError(f"나무증권 매수 주문 실패: {msg}", res_data)
        except NamuhError:
            raise
        except Exception as e:
            raise NamuhError(f"나무증권 매수 주문 통신 오류: {e}")

        # 체결 확인. 지정가(ORD_DVSN=00) 라 접수됐다고 체결된 것이 아니다.
        # 예전에는 SUBMITTED 를 그대로 체결로 기록해, 안 채워진 주문이
        # 장부에만 주식으로 남았다.
        order_id = res_data.get("output", {}).get("ODNO", f"ORD-{int(time.time())}")
        filled = self._await_fill(sym, qty_before, qty, "매수")
        if filled <= 0:
            raise NamuhError(
                f"매수 주문({order_id})이 체결되지 않았습니다 — 지정가 "
                f"${price:,.2f} 미체결. 장부를 바꾸지 않습니다.")
        return {
            "orderId": order_id,
            "ticker": sym,
            "units": float(filled),
            "price": price,
            "amountUsd": filled * price,
            "status": "FILLED" if filled >= qty else "PARTIAL",
            "requestedUnits": float(qty),
        }

    def _await_fill(self, sym: str, qty_before: float, want: float,
                    what: str, tries: int = 6, wait: float = 1.0) -> float:
        """주문 뒤 보유 수량이 얼마나 변했는지 확인한다 (실체결 수량).

        빗썸 경로가 이미 같은 방식으로 실체결을 맞춘다. 접수 응답의 수량을
        믿지 않는 이유는 지정가가 미체결·부분체결로 끝날 수 있어서다.
        """
        sign = 1.0 if what == "매수" else -1.0
        moved = 0.0
        for i in range(tries):
            time.sleep(wait)
            try:
                now_qty = self.held_qty(sym)
            except NamuhError as e:
                logger.warning(f"체결 확인용 잔고 조회 실패({i + 1}/{tries}): {e}")
                continue
            moved = (now_qty - qty_before) * sign
            if moved >= want - 1e-9:
                return moved
        if moved > 0:
            logger.warning(f"{sym} {what} 부분 체결: {moved:.0f}/{want:.0f}주")
        return max(0.0, moved)

    def market_sell(self, ticker: str, units: float) -> Dict[str, Any]:
        """미국 주식 매도 (전량 또는 쿼터 매도)."""
        sym = ticker.upper().strip()
        price = self.get_price(sym)

        # 없는 주식을 팔지 않는다. 예전에는 max(1, int(units)) 라
        # 0.4주 보유에도 1주 매도 주문을 냈다.
        qty = int(units)
        if qty < 1:
            raise NamuhError(
                f"{sym} 매도 수량이 1주 미만입니다 ({units:.4f}주). "
                f"미국 주식은 소수점 매도를 지원하지 않아 주문하지 않습니다.")
        proceeds = qty * price

        if not self.configured:
            logger.info(f"[모의 주문] 나무증권 매도 접수: {sym} {qty}주 @ ${price:,.2f} (${proceeds:,.2f})")
            return {
                "orderId": f"MOCK-SELL-{int(time.time()*1000)}",
                "ticker": sym,
                "units": float(qty),
                "price": price,
                "proceedsUsd": proceeds,
                "status": "FILLED",
            }

        self._require_market_open("매도")

        # 장부보다 실제 보유가 적으면 있는 만큼만 판다 (빗썸 경로와 같은 규칙).
        qty_before = self.held_qty(sym)
        if qty_before < qty:
            if qty_before < 1:
                raise NamuhError(
                    f"{sym} 계좌 보유량이 {qty_before:.0f}주라 매도할 수 없습니다 "
                    f"(장부 {units:.4f}주). 장부와 계좌가 어긋났습니다.")
            logger.warning(f"{sym} 장부 {qty}주 > 계좌 {qty_before:.0f}주 — 있는 만큼만 매도합니다.")
            qty = int(qty_before)
            proceeds = qty * price

        acc_prefix = self.account_no[:8]
        acc_suffix = self.account_no[8:10] if len(self.account_no) >= 10 else "01"
        endpoint = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
        headers = self._headers(tr_id="TTTT1006U")  # 해외주식 매도주문 TR
        body = {
            "CANO": acc_prefix,
            "ACNT_PRDT_CD": acc_suffix,
            "OVRS_EXCG_CD": NAMUH_STOCKS.get(sym, {}).get("market", "NASD")[:4],
            "PDNO": sym,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }
        try:
            res = requests.post(endpoint, headers=headers, json=body, timeout=10)
            res_data = res.json()
            if res.status_code != 200 or res_data.get("rt_cd") != "0":
                msg = res_data.get("msg1") or res.text
                raise NamuhError(f"나무증권 매도 주문 실패: {msg}", res_data)
        except NamuhError:
            raise
        except Exception as e:
            raise NamuhError(f"나무증권 매도 주문 통신 오류: {e}")

        order_id = res_data.get("output", {}).get("ODNO", f"ORD-{int(time.time())}")
        filled = self._await_fill(sym, qty_before, qty, "매도")
        if filled <= 0:
            raise NamuhError(
                f"매도 주문({order_id})이 체결되지 않았습니다 — 지정가 "
                f"${price:,.2f} 미체결. 장부를 바꾸지 않습니다.")
        return {
            "orderId": order_id,
            "ticker": sym,
            "units": float(filled),
            "price": price,
            "proceedsUsd": filled * price,
            "status": "FILLED" if filled >= qty else "PARTIAL",
            "requestedUnits": float(qty),
        }


_default_account = NamuhAccount()

def get_price(ticker: str) -> float:
    """모듈 레벨 현재가 조회 편의 함수."""
    return _default_account.get_price(ticker)

def get_ticker(ticker: str) -> Dict[str, Any]:
    """모듈 레벨 틱 시세 조회 편의 함수."""
    return _default_account.get_ticker(ticker)

def get_candles(ticker: str, interval: str = "1h", limit: int = 200) -> List[Dict[str, Any]]:
    """모듈 레벨 캔들 조회 편의 함수."""
    return _default_account.get_candles(ticker, interval=interval, limit=limit)
