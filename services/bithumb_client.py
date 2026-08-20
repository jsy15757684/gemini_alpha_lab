import time
import base64
import hmac
import hashlib
import urllib.parse
import json
import requests
from typing import Dict, Any, Optional

class BithumbClient:
    """
    빗썸(Bithumb) 공식 REST API v1/v2 클라이언트
    - Public API: 시세, 호가, 캔들
    - Private API: 잔고 조회, 시장가/지정가 매수/매도 주문 (HMAC-SHA512 서명)
    """
    BASE_URL = "https://api.bithumb.com"

    def __init__(self, connect_key: str = "", secret_key: str = ""):
        self.connect_key = connect_key.strip()
        self.secret_key = secret_key.strip()

    def _generate_signature(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, str]:
        """빗썸 API 인증용 HMAC-SHA512 헤더 생성"""
        # microtime 문자열 생성 (현재 시간 * 1000)
        nonce = str(int(time.time() * 1000))
        
        # 쿼리 스트링 인코딩
        query_str = urllib.parse.urlencode(params)
        
        # chr(0) 널바이트로 구분된 서명 원문 생성
        data_to_sign = f"{endpoint}\x00{query_str}\x00{nonce}"
        
        # HMAC-SHA512 해싱 후 hex/base64 인코딩
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            data_to_sign.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        
        api_sign = base64.b64encode(signature.encode('utf-8')).decode('utf-8')
        
        return {
            "Api-Key": self.connect_key,
            "Api-Sign": api_sign,
            "Api-Nonce": nonce,
            "api-client-type": "2",
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def _post(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Private POST 요청 실행"""
        if not self.connect_key or not self.secret_key:
            return {"status": "error", "message": "빗썸 API Connect Key와 Secret Key가 설정되지 않았습니다."}

        if params is None:
            params = {}
        
        # 빗썸 API 표준 파라미터 (order_currency, payment_currency)
        if "endpoint" not in params:
            params["endpoint"] = endpoint

        headers = self._generate_signature(endpoint, params)
        url = f"{self.BASE_URL}{endpoint}"

        try:
            res = requests.post(url, headers=headers, data=params, timeout=5)
            data = res.json()
            return data
        except Exception as e:
            return {"status": "error", "message": f"빗썸 API 통신 오류: {str(e)}"}

    def _get_public(self, endpoint: str) -> Dict[str, Any]:
        """Public GET 요청 실행 (인증 불필요)"""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            res = requests.get(url, timeout=5)
            return res.json()
        except Exception as e:
            return {"status": "error", "message": f"시세 조회 오류: {str(e)}"}

    # ================= Public APIs =================
    def get_ticker(self, order_currency: str = "BTC", payment_currency: str = "KRW") -> Dict[str, Any]:
        """빗썸 현재가 시세 조회"""
        sym = order_currency.upper().replace("-USD", "").replace("KRW-", "")
        endpoint = f"/public/ticker/{sym}_{payment_currency}"
        return self._get_public(endpoint)

    # ================= Private APIs =================
    def get_balance(self, currency: str = "ALL") -> Dict[str, Any]:
        """빗썸 보유 자산 및 원화(KRW) 잔고 조회"""
        endpoint = "/info/balance"
        params = {
            "currency": currency.upper()
        }
        return self._post(endpoint, params)

    def place_market_buy(self, order_currency: str, units: float) -> Dict[str, Any]:
        """빗썸 시장가 매수 주문"""
        endpoint = "/trade/market_buy"
        params = {
            "order_currency": order_currency.upper().replace("-USD", "").replace("KRW-", ""),
            "payment_currency": "KRW",
            "units": str(units)
        }
        return self._post(endpoint, params)

    def place_market_sell(self, order_currency: str, units: float) -> Dict[str, Any]:
        """빗썸 시장가 매도 주문"""
        endpoint = "/trade/market_sell"
        params = {
            "order_currency": order_currency.upper().replace("-USD", "").replace("KRW-", ""),
            "payment_currency": "KRW",
            "units": str(units)
        }
        return self._post(endpoint, params)

    def test_connection(self) -> Dict[str, Any]:
        """API Key 유효성 및 잔고 연동 테스트"""
        if not self.connect_key or not self.secret_key:
            return {
                "success": False,
                "message": "Connect Key와 Secret Key를 모두 입력해주세요."
            }
        
        res = self.get_balance("BTC")
        if res.get("status") == "0000": # 빗썸 성공 코드: "0000"
            data = res.get("data", {})
            total_krw = float(data.get("total_krw", 0))
            in_use_krw = float(data.get("in_use_krw", 0))
            avail_krw = total_krw - in_use_krw
            return {
                "success": True,
                "message": "빗썸 API 연결 성공! 실계좌 잔고 연동 완료.",
                "totalKrw": total_krw,
                "availableKrw": avail_krw,
                "btcBalance": float(data.get("total_btc", 0))
            }
        else:
            err_msg = res.get("message", "API 키 서명 또는 권한 오류")
            return {
                "success": False,
                "message": f"빗썸 인증 실패: {err_msg}"
            }
