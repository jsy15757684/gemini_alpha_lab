import time
import base64
import hmac
import hashlib
import urllib.parse
import json
import uuid
import jwt
import requests
from typing import Dict, Any, Optional

class BithumbClient:
    """
    빗썸(Bithumb) 공식 하이브리드 듀얼 엔진 클라이언트
    - API 2.0: JWT Bearer Token 인증 (최신 표준 규격)
    - API 1.0: HMAC-SHA512 Api-Sign 인증 (기존 표준 규격)
    - 사용자가 1.0/2.0 어떤 키를 발급받았든 100% 자동 감지 및 연결 지원!
    """
    BASE_URL_V1 = "https://api.bithumb.com"
    BASE_URL_V2 = "https://api.bithumb.com/v1"

    def __init__(self, connect_key: str = "", secret_key: str = ""):
        self.connect_key = str(connect_key).strip()
        self.secret_key = str(secret_key).strip()

    # ================= Public API (공통) =================
    def get_ticker(self, order_currency: str = "BTC", payment_currency: str = "KRW") -> Dict[str, Any]:
        """빗썸 현재가 실시간 시세 조회"""
        sym = order_currency.upper().replace("-USD", "").replace("KRW-", "")
        url = f"{self.BASE_URL_V1}/public/ticker/{sym}_{payment_currency}"
        try:
            res = requests.get(url, timeout=3.5)
            return res.json()
        except Exception as e:
            return {"status": "error", "message": f"시세 조회 오류: {str(e)}"}

    # ================= API 2.0 (JWT 방식) =================
    def _get_v2_headers(self, query_params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """빗썸 API 2.0 JWT 헤더 생성"""
        payload = {
            "access_key": self.connect_key,
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000)
        }
        if query_params:
            query_str = urllib.parse.urlencode(query_params)
            query_hash = hashlib.sha512(query_str.encode("utf-8")).hexdigest()
            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        jwt_token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        if isinstance(jwt_token, bytes):
            jwt_token = jwt_token.decode("utf-8")

        return {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json"
        }

    def _get_v2(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.BASE_URL_V2}{endpoint}"
        headers = self._get_v2_headers(params)
        return requests.get(url, headers=headers, params=params, timeout=4).json()

    def _post_v2(self, endpoint: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.BASE_URL_V2}{endpoint}"
        headers = self._get_v2_headers(body)
        return requests.post(url, headers=headers, json=body, timeout=4).json()

    # ================= API 1.0 (HMAC 방식) =================
    def _post_v1(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_params = dict(params) if params else {}
        request_params["endpoint"] = endpoint

        str_data = urllib.parse.urlencode(request_params)
        nonce = str(int(time.time() * 1000))
        data_to_sign = f"{endpoint}\x00{str_data}\x00{nonce}"
        utf8_data = data_to_sign.encode("utf-8")

        key = self.secret_key.encode("utf-8")
        h = hmac.new(key, utf8_data, hashlib.sha512)
        hex_output = h.hexdigest().encode("utf-8")
        api_sign = base64.b64encode(hex_output).decode("utf-8")

        headers = {
            "Api-Key": self.connect_key,
            "Api-Sign": api_sign,
            "Api-Nonce": nonce,
            "api-client-type": "2"
        }

        url = f"{self.BASE_URL_V1}{endpoint}"
        return requests.post(url, headers=headers, data=request_params, timeout=4).json()

    # ================= 하이브리드 자동 감지 API =================
    def get_balance(self, currency: str = "ALL") -> Dict[str, Any]:
        """빗썸 잔고 조회 (API 2.0 우선 시도 -> 1.0 자동 폴백)"""
        if not self.connect_key or not self.secret_key:
            return {"status": "error", "message": "Connect Key와 Secret Key를 먼저 입력해주세요."}

        # 1. API 2.0 (JWT) 잔고 조회 시도
        try:
            res_v2 = self._get_v2("/accounts")
            if isinstance(res_v2, list):
                total_krw = 0.0
                avail_krw = 0.0
                btc_bal = 0.0
                for item in res_v2:
                    curr = item.get("currency", "").upper()
                    bal = float(item.get("balance", 0))
                    locked = float(item.get("locked", 0))
                    if curr == "KRW":
                        avail_krw = bal
                        total_krw = bal + locked
                    elif curr == "BTC":
                        btc_bal = bal + locked

                return {
                    "status": "0000",
                    "apiVersion": "2.0 (JWT)",
                    "data": {
                        "total_krw": str(total_krw),
                        "in_use_krw": str(total_krw - avail_krw),
                        "available_krw": str(avail_krw),
                        "total_btc": str(btc_bal)
                    }
                }
        except Exception:
            pass

        # 2. API 1.0 (HMAC) 잔고 조회 시도
        try:
            res_v1 = self._post_v1("/info/balance", {"currency": currency.upper()})
            if res_v1.get("status") == "0000":
                res_v1["apiVersion"] = "1.0 (HMAC)"
                return res_v1
            return res_v1
        except Exception as e:
            return {"status": "error", "message": f"빗썸 통신 오류: {str(e)}"}

    def place_market_buy(self, order_currency: str, units: float) -> Dict[str, Any]:
        """빗썸 시장가 매수 (API 2.0 / 1.0 자동 실행)"""
        clean_sym = order_currency.upper().replace("-USD", "").replace("KRW-", "")
        
        # API 2.0 시도
        try:
            v2_market = f"KRW-{clean_sym}"
            ticker = self.get_ticker(clean_sym, "KRW")
            cur_p = float(ticker.get("data", {}).get("closing_price", 0))
            price_krw = str(int(units * cur_p)) if cur_p > 0 else "10000"
            
            body = {
                "market": v2_market,
                "side": "bid",
                "price": price_krw,
                "ord_type": "price"
            }
            res_v2 = self._post_v2("/orders", body)
            if res_v2 and "uuid" in res_v2:
                return {"status": "0000", "order_id": res_v2["uuid"], "message": "정상 체결"}
        except Exception:
            pass

        # API 1.0 시도
        return self._post_v1("/trade/market_buy", {
            "order_currency": clean_sym,
            "payment_currency": "KRW",
            "units": str(units)
        })

    def place_market_sell(self, order_currency: str, units: float) -> Dict[str, Any]:
        """빗썸 시장가 매도 (API 2.0 / 1.0 자동 실행)"""
        clean_sym = order_currency.upper().replace("-USD", "").replace("KRW-", "")
        
        # API 2.0 시도
        try:
            v2_market = f"KRW-{clean_sym}"
            body = {
                "market": v2_market,
                "side": "ask",
                "volume": str(units),
                "ord_type": "market"
            }
            res_v2 = self._post_v2("/orders", body)
            if res_v2 and "uuid" in res_v2:
                return {"status": "0000", "order_id": res_v2["uuid"], "message": "정상 체결"}
        except Exception:
            pass

        # API 1.0 시도
        return self._post_v1("/trade/market_sell", {
            "order_currency": clean_sym,
            "payment_currency": "KRW",
            "units": str(units)
        })

    def test_connection(self) -> Dict[str, Any]:
        """API Key 유효성 및 잔고 연동 테스트 (2.0 & 1.0 통합 검증)"""
        if not self.connect_key or not self.secret_key:
            return {
                "success": False,
                "message": "Connect Key와 Secret Key를 모두 입력해주세요."
            }
        
        res = self.get_balance("BTC")
        if res.get("status") == "0000":
            data = res.get("data", {})
            total_krw = float(data.get("total_krw", 0))
            in_use_krw = float(data.get("in_use_krw", 0))
            avail_krw = float(data.get("available_krw", total_krw - in_use_krw))
            ver = res.get("apiVersion", "Live")
            return {
                "success": True,
                "message": f"빗썸 API {ver} 연결 성공! 실계좌 잔고 연동 완료.",
                "totalKrw": total_krw,
                "availableKrw": avail_krw,
                "btcBalance": float(data.get("total_btc", 0))
            }
        else:
            err_msg = res.get("message", "API 키 인증 또는 IP 권한 오류")
            return {
                "success": False,
                "message": f"빗썸 인증 실패: {err_msg}"
            }
