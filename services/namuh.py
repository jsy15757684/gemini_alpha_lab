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

# 운영 / 모의투자 도메인.
#
# 실측으로 확인한 규칙이다 (2026-09-20):
#   토큰  운영에서만 발급된다 (문서에도 모의 '미제공'). 발급받은 토큰은
#         모의 도메인에서도 그대로 통한다
#   시세  운영에서만 제공된다 (문서에 모의 '미제공')
#   잔고·주문  계좌 종류를 따라간다. 모의계좌를 운영 도메인으로 조회하면
#         11512 '데이터가 존재하지 않습니다', 반대도 실패한다
#
# 그래서 토큰·시세는 항상 운영으로, 잔고·주문만 갈라 보낸다.
BASE_URL = (os.getenv("NAMUH_BASE_URL") or "https://api.nhplug.com:8443").strip()
MOCK_BASE_URL = (os.getenv("NAMUH_MOCK_BASE_URL") or "https://moapi.nhplug.com:8443").strip()


def use_mock() -> bool:
    """모의투자 계좌를 쓰는가. NAMUH_MOCK=1 로 켠다."""
    return (os.getenv("NAMUH_MOCK") or "").strip().lower() in ("1", "true", "yes", "on")


def trade_base_url() -> str:
    """잔고·주문이 갈 도메인. 매번 읽어 시험에서 바꿔 끼울 수 있게 한다."""
    return MOCK_BASE_URL if use_mock() else BASE_URL

# 발급받은 토큰을 프로세스 밖에 보관한다.
#
# 공식 문서: "발급된 토큰정보는 24시간 유효합니다. 토큰정보 만료 전 재발급
# 받지 않도록 유의해주세요." 그런데 토큰을 메모리에만 두면 서비스를 재시작할
# 때마다 새로 발급받는다. 배포가 잦은 날에는 하루에도 여러 번이 된다
# (2026-09-19 에는 12번 재시작했다). 발급 횟수 제한에 걸리면 그때부터
# 인증이 통째로 막힌다.
#
# 그래서 디스크에 저장해 재시작 후에도 남은 유효기간을 쓴다.
TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "namuh_token.json")

# 국가코드 (fc_sec_trd_nat_cd). 지금은 미국만 쓴다.
NAT_US = "200"

# 현물호가유형코드 (ahi_nmn_pr_tp_cd). 공식 문서 기준.
ORD_LIMIT = "00"    # 지정가 — fc_orr_uit_pr 필수. 미체결 가능
ORD_MARKET = "03"   # 시장가 — 단가 불필요. 즉시 체결
ORD_LOC = "12"      # LOC(장마감 지정가) — 라오어 원전이 쓰는 방식. 단가 필수
ORDER_TYPE_NAMES = {"00": "지정가", "03": "시장가", "12": "LOC(장마감 지정가)"}

# 기본 주문 유형. 시장가를 쓴다 — 함수 이름(market_buy/market_sell)과 맞고,
# 지정가로 내면 미체결로 끝나 회차만 소비될 수 있다. 3배 레버리지 ETF 는
# 호가 스프레드가 있으니 슬리피지는 감수하는 선택이다.
# NAMUH_ORDER_TYPE 으로 바꿀 수 있다 (00/03/12).
DEFAULT_ORDER_TYPE = (os.getenv("NAMUH_ORDER_TYPE") or ORD_MARKET).strip()

# 라오어 무한매수법 대표 지원 미국 주식 ETF 및 메이저 종목
# 이 프로그램은 **미국 3배 레버리지 ETF 만** 다룬다.
#
# 개별주(NVDA·AAPL·TSLA)는 뺐다. 무한매수법은 자금을 쪼개 계속 담는 전략이라
# 대상이 늘어날수록 자본이 분산되고, 1배 종목은 이 전략을 쓸 이유가 약하다.
# 종목을 늘리려면 여기에 추가하면 된다 — 서버가 이 목록으로 배포를 검증한다.
#
# 거래소 코드는 나무증권 종목 마스터(m_gtsstock.mst)와 대조해 확인했다
# (NQQ=나스닥, NYY=뉴욕). 5종 모두 일치한다.
NAMUH_STOCKS: Dict[str, Dict[str, str]] = {
    "TQQQ": {"name": "ProShares UltraPro QQQ (나스닥 3배)", "market": "NASDAQ", "leverage": "3x", "currency": "USD"},
    "SOXL": {"name": "Direxion Daily Semiconductor Bull 3X (반도체 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "UPRO": {"name": "ProShares UltraPro S&P500 (S&P500 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "TECL": {"name": "Direxion Daily Technology Bull 3X (기술주 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
    "FNGU": {"name": "MicroSectors FANG+ 3X (빅테크 3배)", "market": "NYSE", "leverage": "3x", "currency": "USD"},
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
    
    services.market_schedule의 서머타임 및 NYSE 10대 공휴일 스케줄러를 기반으로 정확히 판정한다.
    """
    from services.market_schedule import get_us_market_status
    st = get_us_market_status(now_utc)
    return {
        "open": st["isOpen"],
        "etTime": st.get("easternTime", ""),
        "tz": "EDT" if st.get("isDst") else "EST",
        "reason": "" if st["isOpen"] else f"{st['statusText']} (개장 예정: {st.get('nextOpenKst', '-')})",
        "details": st,
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

    def _token_cache_key(self) -> str:
        """앱키가 바뀌면 저장된 토큰을 쓰지 않도록 구분자를 둔다 (키 값은 저장하지 않는다)."""
        import hashlib
        return hashlib.sha256(self.app_key.encode()).hexdigest()[:16]

    def _load_token_from_disk(self) -> None:
        try:
            if not os.path.exists(TOKEN_FILE):
                return
            with open(TOKEN_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("keyId") != self._token_cache_key():
                return
            exp = float(d.get("expiresAt") or 0)
            if time.time() < exp - 300 and d.get("token"):
                self._token = d["token"]
                self._token_expires_at = exp
                left = (exp - time.time()) / 3600.0
                logger.info(f"저장된 나무증권 토큰을 재사용합니다 (남은 유효 {left:.1f}시간).")
        except Exception as e:
            logger.warning(f"저장된 나무증권 토큰을 읽지 못했습니다: {e}")

    def _save_token_to_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            tmp = TOKEN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"keyId": self._token_cache_key(), "token": self._token,
                           "expiresAt": self._token_expires_at}, f)
            os.replace(tmp, TOKEN_FILE)
            os.chmod(TOKEN_FILE, 0o600)
        except Exception as e:
            logger.warning(f"나무증권 토큰을 저장하지 못했습니다: {e}")

    def get_token(self, force_refresh: bool = False) -> str:
        """24시간 유효한 OAuth2 Access Token. 만료 전에는 재발급하지 않는다.

        공식 문서가 '만료 전 재발급 금지' 를 명시한다. 메모리에만 두면
        재시작마다 새로 발급받게 되므로 디스크에도 보관한다.
        """
        with self._token_lock:
            now = time.time()
            if not force_refresh and self._token and now < (self._token_expires_at - 300):
                return self._token

            if not force_refresh and not self._token:
                self._load_token_from_disk()
                if self._token and time.time() < (self._token_expires_at - 300):
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
            self._save_token_to_disk()
            logger.info(f"나무증권 OAuth2 토큰 발급/갱신 완료 (유효 {expires_in / 3600:.0f}시간)")
            return self._token

    def _headers(self, tr_id: str = "") -> Dict[str, str]:
        """나무증권은 TR ID 헤더를 쓰지 않는다. Bearer 토큰과 content-type 뿐이다.

        tr_id 인자는 옛 호출부 호환을 위해 남겨두고 무시한다 —
        한국투자증권 규격(tr_id, appkey, appsecret 헤더)을 쓰고 있었는데
        실제 응답은 IGW40401 '제공하지 않는 API URI' 였다.
        """
        return {
            "content-type": "application/json; charset=UTF-8",
            "Authorization": f"Bearer {self.get_token()}",
        }

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
        """해외주식 잔고 조회 (POST /gbstock/inquiry/v1/balance).

        공식 문서 기준이다. 예전에는 한국투자증권 규격(GET
        /uapi/overseas-stock/..., TR ID TTTS3012R, 계좌번호 8+2 분할)을
        쓰고 있었고 실제 응답은 IGW40401 '제공하지 않는 API URI' 였다.
        """
        if not self.configured:
            raise NamuhError("나무증권 계정 키가 설정되지 않았습니다.")
        if not self.account_no:
            raise NamuhError("나무증권 계좌번호가 설정되지 않았습니다 (NAMUH_ACCOUNT_NO).")

        endpoint = f"{trade_base_url()}/gbstock/inquiry/v1/balance"
        body = {
            "Input_0": {
                "act_no": self.account_no,      # 11자리 그대로 (쪼개지 않는다)
                "qut_iqr_dit_cd": "9",          # 9.전체
                "fc_sec_trd_nat_cd": NAT_US,    # 200.미국
                "cur_cd": "USD",
                "xns_dit_cd": "0",              # 비용 미포함
            }
        }
        try:
            res = requests.post(endpoint, headers=self._headers(),
                                json=body, timeout=10)
        except Exception as e:
            raise NamuhError(f"나무증권 잔고 조회 통신 오류: {e}")

        if res.status_code != 200:
            raise NamuhError(
                f"나무증권 잔고 조회 실패 (HTTP {res.status_code}): {(res.text or '')[:200]}")
        try:
            b = res.json()
        except Exception:
            raise NamuhError(f"나무증권 잔고 응답을 해석하지 못했습니다: {(res.text or '')[:200]}")

        # 성공 코드는 rt_cd 가 아니라 rsp_cd 다. 조회 성공은 00166.
        rsp_cd = str(b.get("rsp_cd", ""))
        if "Output_0" not in b and rsp_cd not in ("00166", "0"):
            raise NamuhError(
                f"나무증권 잔고 조회 거부 ({rsp_cd}): {b.get('rsp_msg') or str(b)[:160]}")

        o0 = b.get("Output_0") or {}
        usd_avail = float(o0.get("fc_dca") or 0.0)          # 외화예수금
        usd_total = float(o0.get("fc_aet_amt") or usd_avail)  # 외화자산금액

        holdings: Dict[str, Dict[str, Any]] = {}
        for it in (b.get("Output_1") or []):
            tkr = str(it.get("iem_cd", "")).strip().upper()
            qty = float(it.get("cns_bse_bnc_qty") or 0.0)   # 체결기준잔고수량
            if not tkr or qty <= 0:
                continue
            holdings[tkr] = {
                "qty": qty,
                "sellableQty": float(it.get("sll_pbl_qty1") or qty),
                "avgPrice": float(it.get("fc_phs_uit_pr") or 0.0),   # 외화매입단가
                "lastPrice": float(it.get("fc_sec_end_pr") or 0.0),  # 외화증권종가
                "evalAmountUsd": float(it.get("fc_eal_amt") or 0.0),
                "pnlUsd": float(it.get("fc_eal_pls_amt") or 0.0),
                "name": str(it.get("iem_nm", "")).strip(),
                "currency": str(it.get("cur_cd", "USD")).strip() or "USD",
            }

        return {
            "success": True,
            "usdAvailable": usd_avail,
            "usdTotal": usd_total,
            "krwDeposit": float(o0.get("krw_dca") or 0.0),
            "totalAssetKrw": float(o0.get("tot_aet_amt") or 0.0),
            "holdings": holdings,
            # 대조에 쓰는 표준 형식. 매도가능 수량 기준이 안전하다.
            "qtyByTicker": {t: v["qty"] for t, v in holdings.items()},
            "sellableByTicker": {t: v["sellableQty"] for t, v in holdings.items()},
            "accountNo": self.masked_account(),
            "rspCd": rsp_cd,
        }

    def quote(self, ticker: str) -> Dict[str, Any]:
        """해외주식 현재가 상세 (POST /gbstock/quote/v1/current).

        현재가뿐 아니라 최우선 호가까지 준다. 실제로 체결될 가격은 호가이므로
        지정가·LOC 주문을 낼 때 이 값을 쓰는 것이 맞다.
        """
        sym = ticker.upper().strip()
        if not self.configured:
            raise NamuhError("나무증권 계정 키가 설정되지 않았습니다.")
        try:
            res = requests.post(f"{BASE_URL}/gbstock/quote/v1/current",
                                headers=self._headers(),
                                json={"Input_0": {"iem_cd": sym}}, timeout=8)
            b = res.json()
        except Exception as e:
            raise NamuhError(f"나무증권 시세 조회 통신 오류 ({sym}): {e}")

        rsp_cd = str(b.get("rsp_cd", ""))
        o = b.get("Output_0") or {}
        if res.status_code != 200 or not o:
            raise NamuhError(
                f"나무증권 시세 조회 거부 ({rsp_cd}): "
                f"{b.get('rsp_msg') or (res.text or '')[:160]}")

        price = float(o.get("trdprc") or 0.0)
        if price <= 0:
            raise NamuhError(f"{sym} 현재가가 0 입니다 (응답 {rsp_cd}).")
        return {
            "ticker": sym,
            "price": price,
            "bid": float(o.get("best_bid1") or 0.0),
            "ask": float(o.get("best_ask1") or 0.0),
            "prevClose": float(o.get("hst_trdprc") or 0.0),
            "changePct": float(o.get("pctchng") or 0.0),
            "open": float(o.get("open_prc") or 0.0),
            "high": float(o.get("high") or 0.0),
            "low": float(o.get("low") or 0.0),
            "volume": float(o.get("acvol") or 0.0),
            "exchange": str(o.get("exch_id") or "").strip(),
            "name": str(o.get("iem_nm") or "").strip(),
            "currency": str(o.get("currency_unit") or "USD").strip(),
            "marketPeriod": str(o.get("marketperiod_cls") or "").strip(),
            "source": "namuh",
        }

    def get_price(self, ticker: str) -> float:
        """미국 주식 현재가(USD)."""
        sym = ticker.upper().strip()
        with _price_lock:
            now = time.time()
            if sym in _price_cache:
                t, p = _price_cache[sym]
                if now - t < _PRICE_TTL and p > 0:
                    return p

        # 1) 나무증권 현재가 (공식 규격). 이것이 실제 매매하는 시세다.
        if self.configured:
            try:
                p = self.quote(sym)["price"]
                with _price_lock:
                    _price_cache[sym] = (time.time(), p)
                return p
            except NamuhError as e:
                logger.warning(f"나무증권 시세 실패, 공개 시세로 넘어갑니다 ({sym}): {e}")

        # 2) 공개 시세 (Yahoo Finance). 출처가 다르다는 것을 숨기지 않는다.
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            if res.status_code == 200:
                price = float(res.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
                if price > 0:
                    with _price_lock:
                        _price_cache[sym] = (time.time(), price)
                    return price
        except Exception as e:
            logger.warning(f"공개 시세 수신 실패 ({sym}): {e}")

        # 받지 못하면 값을 지어내지 않는다.
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

    def market_buy(self, ticker: str, amount_usd: float = 0.0,
                   order_type: str = "", units: float = 0.0,
                   limit_price: float = 0.0,
                   await_fill: bool = True) -> Dict[str, Any]:
        """미국 주식 매수 (라오어 무한매수 금액 또는 정수 주수 기준 주문).

        await_fill=False 는 LOC(장마감 지정가) 전용이다. LOC 는 마감
        동시호가에서만 붙으므로 접수 직후 몇 초를 기다려봐야 늘 '미체결'
        이다. 그때 예외를 던지면 호출부는 '안 샀다' 로 처리하는데 주문은
        거래소에 살아 있어, 봉마다 주문이 쌓인다. 그래서 LOC 는 접수만
        확인하고 (status=ACCEPTED, units=0) 체결은 다음 세션에 잔고
        변화로 정산한다.
        """
        sym = ticker.upper().strip()
        order_type = (order_type or DEFAULT_ORDER_TYPE).strip()
        price = limit_price if limit_price > 0 else self.get_price(sym)
        if price <= 0:
            raise NamuhError(f"현재가를 조회할 수 없습니다: {sym}")

        if units > 0:
            qty = int(units)
        else:
            # 배정액을 넘지 않게 내림한다.
            qty = int(amount_usd // price)
        if qty < 1:
            raise NamuhError(
                f"1회 배정액 ${amount_usd:,.2f} 이 {sym} 1주 값 ${price:,.2f} 보다 "
                f"작아 매수하지 않습니다. 분할수를 줄이거나 운용자본을 늘리세요.")
        actual_amt = qty * price

        if not self.configured:
            logger.info(f"[모의 주문] 나무증권 매수 접수: {sym} {qty}주 @ ${price:,.2f} (${actual_amt:,.2f}) | {order_type or 'LOC'}")
            return {
                "orderId": f"MOCK-BUY-{int(time.time()*1000)}",
                "ticker": sym,
                "units": float(qty),
                "price": price,
                "amountUsd": actual_amt,
                "status": "FILLED",
                "orderType": order_type or "LOC",
            }

        # 실주문 전에 장이 열려 있는지 본다.
        self._require_market_open("매수")
        qty_before = self.held_qty(sym)

        # 공식 문서: POST /gbstock/order/v1/buy
        # 예전에는 한국투자증권 규격(/uapi/overseas-stock/v1/trading/order,
        # TR TTTT1002U, CANO 8+2 분할)을 쓰고 있었다.
        inp = {
            "act_no": self.account_no,          # 11자리 그대로
            "fc_sec_trd_nat_cd": NAT_US,        # 200.미국
            "iem_cd": sym,                      # 예: AAPL (순수 티커)
            "orr_qty": int(qty),
            "ahi_nmn_pr_tp_cd": order_type,
            "wtm_cur_knd_cd": "1",              # 1.해당통화(USD)
        }
        # 단가는 지정가 계열에서만 필수다 (00/11/12/61/62/63). 소수점 2자리.
        if order_type != ORD_MARKET:
            inp["fc_orr_uit_pr"] = round(price, 2)

        try:
            res = requests.post(f"{trade_base_url()}/gbstock/order/v1/buy",
                                headers=self._headers(), json={"Input_0": inp}, timeout=10)
            res_data = res.json()
        except NamuhError:
            raise
        except Exception as e:
            raise NamuhError(f"나무증권 매수 주문 통신 오류: {e}")

        # 주문 완료 코드는 00171 이다 (rt_cd 가 아니다).
        rsp_cd = str(res_data.get("rsp_cd", ""))
        order_id = str((res_data.get("Output_0") or {}).get("orr_no") or "")
        if res.status_code != 200 or (not order_id and rsp_cd != "00171"):
            raise NamuhError(
                f"나무증권 매수 주문 실패 ({rsp_cd}): "
                f"{res_data.get('rsp_msg') or (res.text or '')[:160]}", res_data)

        if not await_fill:
            # LOC: 접수만 확인한다. 체결은 마감 뒤 잔고로 정산한다.
            return {
                "orderId": order_id,
                "ticker": sym,
                "units": 0.0,                 # 아직 아무것도 안 샀다
                "requestedUnits": float(qty),
                "price": price,
                "amountUsd": 0.0,
                "status": "ACCEPTED",
                "orderType": order_type,
                "qtyBefore": qty_before,
            }

        # 접수됐다고 체결된 것이 아니다. 실제 보유 수량 변화로 확인한다.
        filled = self._await_fill(sym, qty_before, qty, "매수")
        if filled <= 0:
            raise NamuhError(
                f"매수 주문({order_id})이 체결되지 않았습니다 — {ORDER_TYPE_NAMES.get(order_type, order_type)} "
                f"${price:,.2f}. 장부를 바꾸지 않습니다.")
        return {
            "orderId": order_id,
            "ticker": sym,
            "units": float(filled),
            "price": price,
            "amountUsd": filled * price,
            "status": "FILLED" if filled >= qty else "PARTIAL",
            "requestedUnits": float(qty),
            "orderType": order_type,
        }

    def cancel_order(self, order_id: Any, ticker: str,
                     qty: float = 0.0) -> Dict[str, Any]:
        """주문 취소 (POST /gbstock/order/v1/cancel).

        LOC 는 마감까지 거래소에 살아 있다. 익절로 포지션을 닫았는데 매수
        LOC 가 남아 있으면 마감 동시호가에 체결돼 포지션이 되살아난다.
        그래서 장부를 비울 때는 미체결 주문도 같이 거둬들여야 한다.

        NYSE 는 15:50 ET 이후 LOC 취소를 받지 않는다. 그 뒤의 취소는
        거부되므로, 호출부는 실패를 정상 경로로 다뤄야 한다.
        """
        sym = ticker.upper().strip()
        inp = {
            "act_no": self.account_no,
            "org_orr_no": int(order_id),
            "fc_sec_trd_nat_cd": NAT_US,
            "iem_cd": sym,
            "all_pat_dit_cd": "2" if qty and qty > 0 else "1",   # 2:일부, 1:전체
        }
        if qty and qty > 0:
            inp["can_qty"] = int(qty)

        try:
            res = requests.post(f"{trade_base_url()}/gbstock/order/v1/cancel",
                                headers=self._headers(), json={"Input_0": inp}, timeout=10)
            data = res.json()
        except Exception as e:
            raise NamuhError(f"나무증권 주문 취소 통신 오류: {e}")

        rsp_cd = str(data.get("rsp_cd", ""))
        new_id = str((data.get("Output_0") or {}).get("orr_no") or "")
        # 취소 완료 코드는 00192 다.
        if res.status_code != 200 or (not new_id and rsp_cd != "00192"):
            raise NamuhError(
                f"나무증권 주문 취소 실패 ({rsp_cd}): "
                f"{data.get('rsp_msg') or (res.text or '')[:160]}", data)
        return {"cancelOrderId": new_id, "originalOrderId": str(order_id),
                "ticker": sym, "rspCd": rsp_cd}

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

    def market_sell(self, ticker: str, units: float,
                    order_type: str = "") -> Dict[str, Any]:
        """미국 주식 매도 (전량 또는 쿼터 매도)."""
        sym = ticker.upper().strip()
        order_type = (order_type or DEFAULT_ORDER_TYPE).strip()
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

        # 공식 문서: POST /gbstock/order/v1/sell
        # 매도에는 증거금통화종류코드(wtm_cur_knd_cd)가 없다.
        inp = {
            "act_no": self.account_no,
            "fc_sec_trd_nat_cd": NAT_US,
            "iem_cd": sym,
            "orr_qty": int(qty),
            "ahi_nmn_pr_tp_cd": order_type,
        }
        if order_type != ORD_MARKET:
            inp["fc_orr_uit_pr"] = round(price, 2)

        try:
            res = requests.post(f"{trade_base_url()}/gbstock/order/v1/sell",
                                headers=self._headers(), json={"Input_0": inp}, timeout=10)
            res_data = res.json()
        except NamuhError:
            raise
        except Exception as e:
            raise NamuhError(f"나무증권 매도 주문 통신 오류: {e}")

        rsp_cd = str(res_data.get("rsp_cd", ""))
        order_id = str((res_data.get("Output_0") or {}).get("orr_no") or "")
        if res.status_code != 200 or (not order_id and rsp_cd != "00171"):
            raise NamuhError(
                f"나무증권 매도 주문 실패 ({rsp_cd}): "
                f"{res_data.get('rsp_msg') or (res.text or '')[:160]}", res_data)

        filled = self._await_fill(sym, qty_before, qty, "매도")
        if filled <= 0:
            raise NamuhError(
                f"매도 주문({order_id})이 체결되지 않았습니다 — "
                f"{ORDER_TYPE_NAMES.get(order_type, order_type)} ${price:,.2f}. "
                f"장부를 바꾸지 않습니다.")
        return {
            "orderId": order_id,
            "ticker": sym,
            "units": float(filled),
            "price": price,
            "proceedsUsd": filled * price,
            "status": "FILLED" if filled >= qty else "PARTIAL",
            "requestedUnits": float(qty),
            "orderType": order_type,
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
