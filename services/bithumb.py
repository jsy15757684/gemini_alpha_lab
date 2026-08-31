"""빗썸 원화마켓 API 클라이언트 (공개 시세 + 인증 주문).

이 프로젝트는 빗썸 원화 자동매매 하나만 한다. 다른 거래소·자산은 다루지 않는다.

주의 — 캔들 배열 필드 순서
    빗썸은 [시각, 시가, 종가, 고가, 저가, 거래량] 순으로 준다.
    흔한 OHLC 순서가 아니라 '종가가 세 번째' 다. 이걸 틀리면 지표가 전부 어긋난다.
    normalize_candles() 가 이 매핑을 한 곳에서만 처리한다.
"""

import os
import time
import json
import uuid
import base64
import hmac
import hashlib
import logging
import threading
import urllib.parse
from typing import Any, Dict, List, Optional

import jwt
import requests

logger = logging.getLogger(__name__)

BASE_V1 = "https://api.bithumb.com"
BASE_V2 = "https://api.bithumb.com/v1"

# 이 앱이 다루는 빗썸 원화마켓 코인
COINS: Dict[str, str] = {
    "BTC": "비트코인",
    "ETH": "이더리움",
    "SOL": "솔라나",
    "XRP": "리플",
    "DOGE": "도지코인",
}

# 빗썸이 지원하는 캔들 간격
INTERVALS = ["1m", "3m", "5m", "10m", "30m", "1h", "6h", "12h", "24h"]

PROXY_ENV = "BITHUMB_PROXY_URL"


class BithumbError(Exception):
    """빗썸이 명시적으로 거부했을 때. message 는 사용자에게 보여줄 수 있는 문장."""

    def __init__(self, message: str, raw: Any = None):
        super().__init__(message)
        self.message = message
        self.raw = raw


def normalize_coin(symbol: str) -> Optional[str]:
    """'btc', 'BTC-USD', 'KRW-BTC' -> 'BTC'. 지원하지 않으면 None."""
    if not symbol:
        return None
    s = symbol.upper().replace("KRW-", "").replace("-KRW", "").replace("-USD", "").strip()
    return s if s in COINS else None


def proxy_url() -> str:
    """빗썸은 API 키에 IP 등록을 요구한다. 아웃바운드 IP 가 고정되지 않는 환경에서는
    고정 IP 프록시를 경유하고 그 IP 하나만 등록한다. 공개 시세는 경유하지 않는다."""
    return (os.getenv(PROXY_ENV) or "").strip()


def _proxies() -> Optional[Dict[str, str]]:
    u = proxy_url()
    return {"http": u, "https": u} if u else None


# ───────────────────────── 공개 API ─────────────────────────

# 봇 여러 개가 같은 코인을 동시에 볼 때 중복 호출을 줄인다.
# 손절 판단에 쓰이므로 TTL 은 짧게 둔다 (기본 3초).
_PRICE_TTL = float(os.getenv("APP_PRICE_CACHE_SEC", "3"))
_price_cache: Dict[str, tuple] = {}
_price_lock = threading.Lock()


def get_price(coin: str) -> float:
    """현재가(원). 실패하면 BithumbError."""
    c = normalize_coin(coin)
    if not c:
        raise BithumbError(f"빗썸 원화마켓에 없는 코인입니다: {coin}")
    with _price_lock:
        hit = _price_cache.get(c)
        if hit and (time.time() - hit[0]) < _PRICE_TTL:
            return hit[1]
    try:
        r = requests.get(f"{BASE_V1}/public/ticker/{c}_KRW", timeout=6)
        body = r.json()
    except Exception as e:
        raise BithumbError(f"시세 조회 통신 오류: {e}")
    if body.get("status") != "0000":
        raise BithumbError(f"시세 조회 실패: {body.get('message', body.get('status'))}", body)
    price = float(body["data"]["closing_price"])
    if price <= 0:
        raise BithumbError("시세가 0 으로 조회되었습니다")
    with _price_lock:
        _price_cache[c] = (time.time(), price)
    return price


def get_ticker(coin: str) -> Dict[str, Any]:
    """현재가 + 24시간 변동 정보."""
    c = normalize_coin(coin)
    if not c:
        raise BithumbError(f"빗썸 원화마켓에 없는 코인입니다: {coin}")
    try:
        body = requests.get(f"{BASE_V1}/public/ticker/{c}_KRW", timeout=6).json()
    except Exception as e:
        raise BithumbError(f"시세 조회 통신 오류: {e}")
    if body.get("status") != "0000":
        raise BithumbError(f"시세 조회 실패: {body.get('message')}", body)
    d = body["data"]
    price = float(d["closing_price"])
    prev = float(d.get("prev_closing_price") or price)
    return {
        "coin": c,
        "name": COINS[c],
        "price": price,
        "prevClose": prev,
        "change": round(price - prev, 0),
        "changePercent": round(float(d.get("fluctate_rate_24H") or 0.0), 2),
        "high24h": float(d.get("max_price") or price),
        "low24h": float(d.get("min_price") or price),
        "volume24h": float(d.get("units_traded_24H") or 0.0),
        "currency": "KRW",
        "source": "bithumb-public",
    }


def normalize_candles(rows: List[List[Any]]) -> List[Dict[str, Any]]:
    """빗썸 캔들 배열을 dict 로 바꾼다.

    빗썸 순서: [시각(ms), 시가, 종가, 고가, 저가, 거래량]
    종가가 index 2, 고가가 3, 저가가 4 다. 순서를 헷갈리기 쉬워 여기 한 곳에 가둔다.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            out.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return out


def get_candles(coin: str, interval: str = "1h", limit: int = 200) -> List[Dict[str, Any]]:
    """과거 캔들. 빗썸은 한 번에 최대 200개를 준다."""
    c = normalize_coin(coin)
    if not c:
        raise BithumbError(f"빗썸 원화마켓에 없는 코인입니다: {coin}")
    if interval not in INTERVALS:
        raise BithumbError(f"지원하지 않는 캔들 간격입니다: {interval} (가능: {', '.join(INTERVALS)})")
    try:
        body = requests.get(f"{BASE_V1}/public/candlestick/{c}_KRW/{interval}", timeout=10).json()
    except Exception as e:
        raise BithumbError(f"캔들 조회 통신 오류: {e}")
    if body.get("status") != "0000":
        raise BithumbError(f"캔들 조회 실패: {body.get('message')}", body)
    candles = normalize_candles(body.get("data") or [])
    if not candles:
        raise BithumbError("캔들 데이터가 비어 있습니다")
    return candles[-limit:] if limit else candles


# ───────────────────────── 인증 API ─────────────────────────

class BithumbAccount:
    """API 키가 필요한 호출. 키는 생성자에서만 받고 밖으로 내보내지 않는다."""

    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.api_key = str(api_key or "").strip()
        self.secret_key = str(secret_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def masked_key(self) -> str:
        k = self.api_key
        return (k[:3] + "******" + k[-3:]) if len(k) > 6 else ("******" if k else "")

    # --- API 2.0 (JWT) ---
    def _v2_headers(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        payload = {
            "access_key": self.api_key,
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
        }
        if params:
            qs = urllib.parse.urlencode(params)
            payload["query_hash"] = hashlib.sha512(qs.encode()).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # --- API 1.0 (HMAC-SHA512) ---
    def _v1_post(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = {"endpoint": endpoint}
        body.update(params or {})
        str_data = urllib.parse.urlencode(body)
        nonce = str(int(time.time() * 1000))
        raw = f"{endpoint}\x00{str_data}\x00{nonce}".encode()
        sign = base64.b64encode(
            hmac.new(self.secret_key.encode(), raw, hashlib.sha512).hexdigest().encode()
        ).decode()
        headers = {
            "Api-Key": self.api_key,
            "Api-Sign": sign,
            "Api-Nonce": nonce,
            "api-client-type": "2",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return requests.post(BASE_V1 + endpoint, headers=headers, data=str_data,
                             timeout=10, proxies=_proxies()).json()

    def get_balance(self) -> Dict[str, Any]:
        """원화 잔고와 코인 보유량. 두 엔진을 모두 시도하고 실패 사유를 보존한다."""
        if not self.configured:
            raise BithumbError("빗썸 API 키가 등록되지 않았습니다.")

        diag: Dict[str, Any] = {}

        # API 2.0
        try:
            res = requests.get(f"{BASE_V2}/accounts", headers=self._v2_headers(),
                               timeout=10, proxies=_proxies()).json()
            if isinstance(res, list):
                krw_free = krw_locked = 0.0
                coins: Dict[str, float] = {}
                for item in res:
                    cur = str(item.get("currency", "")).upper()
                    bal = float(item.get("balance") or 0)
                    lock = float(item.get("locked") or 0)
                    if cur == "KRW":
                        krw_free, krw_locked = bal, lock
                    elif cur in COINS:
                        coins[cur] = bal + lock
                return {"apiVersion": "2.0 (JWT)", "krwAvailable": krw_free,
                        "krwTotal": krw_free + krw_locked, "coins": coins}
            diag["v2"] = res.get("error", res) if isinstance(res, dict) else res
        except Exception as e:
            diag["v2"] = f"통신/서명 오류: {e}"

        # API 1.0
        try:
            res = self._v1_post("/info/balance", {"currency": "ALL"})
            if res.get("status") == "0000":
                d = res.get("data", {})
                coins = {}
                for c in COINS:
                    v = d.get(f"total_{c.lower()}")
                    if v is not None:
                        coins[c] = float(v)
                return {"apiVersion": "1.0 (HMAC)",
                        "krwAvailable": float(d.get("available_krw") or 0),
                        "krwTotal": float(d.get("total_krw") or 0),
                        "coins": coins}
            diag["v1"] = res.get("message") or res
        except Exception as e:
            diag["v1"] = f"통신 오류: {e}"

        raise BithumbError(explain_auth_failure(diag), diag)

    def market_buy(self, coin: str, krw_amount: float) -> Dict[str, Any]:
        """원화 금액만큼 시장가 매수."""
        c = normalize_coin(coin)
        if not c:
            raise BithumbError(f"빗썸 원화마켓에 없는 코인입니다: {coin}")
        if not self.configured:
            raise BithumbError("빗썸 API 키가 등록되지 않았습니다.")
        amount = int(krw_amount)
        if amount < 1000:
            raise BithumbError(f"주문 금액이 너무 작습니다: {amount:,}원")

        try:
            res = requests.post(f"{BASE_V2}/orders",
                                headers=self._v2_headers(),
                                json={"market": f"KRW-{c}", "side": "bid",
                                      "price": str(amount), "ord_type": "price"},
                                timeout=10, proxies=_proxies()).json()
            if isinstance(res, dict) and res.get("uuid"):
                return {"orderId": res["uuid"], "apiVersion": "2.0"}
        except Exception as e:
            logger.warning(f"빗썸 2.0 매수 실패, 1.0 시도: {e}")

        price = get_price(c)
        units = round(amount / price, 8)
        res = self._v1_post("/trade/market_buy",
                            {"order_currency": c, "payment_currency": "KRW", "units": str(units)})
        if res.get("status") != "0000":
            raise BithumbError(f"매수 거부: {res.get('message') or res.get('status')}", res)
        return {"orderId": res.get("order_id"), "apiVersion": "1.0"}

    def market_sell(self, coin: str, units: float) -> Dict[str, Any]:
        """보유 수량만큼 시장가 매도."""
        c = normalize_coin(coin)
        if not c:
            raise BithumbError(f"빗썸 원화마켓에 없는 코인입니다: {coin}")
        if not self.configured:
            raise BithumbError("빗썸 API 키가 등록되지 않았습니다.")
        if units <= 0:
            raise BithumbError("매도 수량이 0 입니다.")

        try:
            res = requests.post(f"{BASE_V2}/orders",
                                headers=self._v2_headers(),
                                json={"market": f"KRW-{c}", "side": "ask",
                                      "volume": str(units), "ord_type": "market"},
                                timeout=10, proxies=_proxies()).json()
            if isinstance(res, dict) and res.get("uuid"):
                return {"orderId": res["uuid"], "apiVersion": "2.0"}
        except Exception as e:
            logger.warning(f"빗썸 2.0 매도 실패, 1.0 시도: {e}")

        res = self._v1_post("/trade/market_sell",
                            {"order_currency": c, "payment_currency": "KRW", "units": str(units)})
        if res.get("status") != "0000":
            raise BithumbError(f"매도 거부: {res.get('message') or res.get('status')}", res)
        return {"orderId": res.get("order_id"), "apiVersion": "1.0"}

    def test_connection(self) -> Dict[str, Any]:
        if not self.configured:
            return {"success": False, "message": "Connect Key 와 Secret Key 를 모두 입력하세요."}
        try:
            bal = self.get_balance()
        except BithumbError as e:
            return {"success": False, "message": e.message, "diagnostics": e.raw}
        return {
            "success": True,
            "message": f"빗썸 API {bal['apiVersion']} 연결 성공",
            "krwAvailable": bal["krwAvailable"],
            "krwTotal": bal["krwTotal"],
            "coins": bal["coins"],
        }


def explain_auth_failure(diag: Dict[str, Any]) -> str:
    """두 엔진의 실패 사유를 조치 가능한 문장으로 옮긴다. 원문도 함께 남긴다."""
    blob = f"{diag.get('v2', '')} {diag.get('v1', '')}".lower()
    raw = f"(원문: 2.0={diag.get('v2')} / 1.0={diag.get('v1')})"

    # IP 거부를 가장 먼저 본다.
    # API 2.0 이 NotAllowIP 를 돌려줘도 1.0 폴백은 "Invalid Apikey" 를 돌려주는데,
    # 키 오류를 먼저 검사하면 IP 문제를 '키가 틀렸다' 고 잘못 안내하게 된다.
    # (실제로 그렇게 잘못 안내했다 — IP 가 바뀐 상황에서 키를 의심하게 만들었다)
    if "notallowip" in blob or "not allowed client ip" in blob or "allowed ip" in blob:
        current = egress_ip().get("ip")
        where = ("프록시 서버" if proxy_url() else "이 서버")
        return (f"빗썸이 요청 IP 를 거부했습니다 (등록되지 않은 IP). "
                f"{where}의 현재 공인 IP 는 {current or '확인 불가'} 입니다. "
                f"빗썸 [API 관리 > IP 주소 등록]에 이 IP 를 추가하세요. "
                f"회선 IP 가 바뀌면 이 오류가 다시 납니다. {raw}")
    if "auth data" in blob:
        # 키는 인식되는데 서명이 검증되지 않는 상태 = Secret Key 불일치
        return ("Connect Key 는 빗썸이 인식했지만 서명이 검증되지 않았습니다. "
                "Secret Key 가 이 Connect Key 의 짝이 아닙니다. 키를 재발급해 다시 등록하세요. " + raw)
    if "invalid apikey" in blob or "invalid_access_key" in blob:
        return ("빗썸이 Connect Key 를 인식하지 못했습니다. 키 값과 Connect/Secret 자리가 "
                "바뀌지 않았는지 확인하세요. " + raw)
    if " ip " in f" {blob} ":
        hint = ("프록시를 경유 중입니다. 프록시 서버 IP 를 빗썸에 등록했는지 확인하세요."
                if proxy_url() else
                "화면에 표시된 서버 IP 를 빗썸 [API 관리 > IP 주소 등록]에 등록하세요.")
        return f"빗썸이 요청 IP 를 거부했습니다. {hint} {raw}"
    if "permission" in blob or "권한" in blob:
        return f"API 키에 필요한 권한이 없습니다. '자산 조회'와 '주문' 권한을 확인하세요. {raw}"
    return f"빗썸 인증에 실패했습니다. {raw}"


def egress_ip() -> Dict[str, Any]:
    """인증 요청이 실제로 나가는 공인 IP. 빗썸에 등록해야 하는 값이다."""
    proxies = _proxies()
    info: Dict[str, Any] = {"proxyConfigured": bool(proxies), "proxyHost": None,
                            "ip": None, "error": None}
    if proxies:
        try:
            pr = urllib.parse.urlparse(proxy_url())
            info["proxyHost"] = f"{pr.hostname}:{pr.port}" if pr.port else pr.hostname
        except Exception:
            info["proxyHost"] = "(파싱 불가)"
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=8, proxies=proxies)
        if r.status_code == 200:
            info["ip"] = r.json().get("ip")
        else:
            info["error"] = f"IP 조회 실패 (HTTP {r.status_code})"
    except Exception as e:
        info["error"] = f"{'프록시 경유 ' if proxies else ''}IP 조회 실패: {e}"
    return info
