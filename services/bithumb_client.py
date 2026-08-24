import os
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

    # 빗썸은 API 키에 IP 등록을 요구한다. Render 같은 PaaS 는 아웃바운드 IP 가
    # 공용 대역이라 등록이 불가능할 수 있다. 그 경우 고정 IP 를 가진 프록시를
    # 경유하게 하고, 그 프록시의 IP 하나만 빗썸에 등록한다.
    #   예) BITHUMB_PROXY_URL=http://user:pass@203.0.113.10:3128
    # 공개(Public) 시세 조회는 IP 등록이 필요 없으므로 프록시를 타지 않는다.
    PROXY_ENV = "BITHUMB_PROXY_URL"

    def __init__(self, connect_key: str = "", secret_key: str = ""):
        self.connect_key = str(connect_key).strip()
        self.secret_key = str(secret_key).strip()

    @classmethod
    def proxy_url(cls) -> str:
        return (os.getenv(cls.PROXY_ENV) or "").strip()

    @classmethod
    def _proxies(cls):
        """인증 요청에만 적용할 프록시 설정. 미설정이면 None."""
        url = cls.proxy_url()
        return {"http": url, "https": url} if url else None

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
        return requests.get(url, headers=headers, params=params, timeout=8,
                            proxies=self._proxies()).json()

    def _post_v2(self, endpoint: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.BASE_URL_V2}{endpoint}"
        headers = self._get_v2_headers(body)
        return requests.post(url, headers=headers, json=body, timeout=8,
                             proxies=self._proxies()).json()

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
        return requests.post(url, headers=headers, data=request_params, timeout=8,
                             proxies=self._proxies()).json()

    # ================= 하이브리드 자동 감지 API =================
    def get_balance(self, currency: str = "ALL") -> Dict[str, Any]:
        """빗썸 잔고 조회 (API 2.0 우선 시도 -> 1.0 자동 폴백).
        두 엔진이 모두 실패하면 각 엔진이 돌려준 실제 사유를 모두 담아 반환한다.
        (사유를 삼켜버리면 '키가 틀렸는지 / IP 미등록인지'를 사용자가 구분할 수 없다)"""
        if not self.connect_key or not self.secret_key:
            return {"status": "error", "message": "Connect Key와 Secret Key를 먼저 입력해주세요."}

        diag = {}

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
            else:
                # 리스트가 아니면 2.0 이 에러를 돌려준 것 -> 사유 보관
                err = res_v2.get("error", res_v2) if isinstance(res_v2, dict) else res_v2
                diag["v2"] = err
        except Exception as e:
            diag["v2"] = f"통신/서명 오류: {e}"

        # 2. API 1.0 (HMAC) 잔고 조회 시도
        try:
            res_v1 = self._post_v1("/info/balance", {"currency": currency.upper()})
            if res_v1.get("status") == "0000":
                res_v1["apiVersion"] = "1.0 (HMAC)"
                return res_v1
            diag["v1"] = res_v1.get("message") or res_v1
        except Exception as e:
            diag["v1"] = f"통신 오류: {e}"

        return {
            "status": "error",
            "message": self._explain(diag),
            "diagnostics": diag,
        }

    @staticmethod
    def _explain(diag: Dict[str, Any]) -> str:
        """두 엔진의 실패 사유를 사용자가 조치 가능한 문장으로 번역"""
        blob = f"{diag.get('v2', '')} {diag.get('v1', '')}".lower()
        if "ip" in blob:
            hint = ("프록시(BITHUMB_PROXY_URL)를 경유하고 있습니다. 프록시 서버의 IP 를 "
                    "빗썸에 등록했는지 확인하세요."
                    if BithumbClient.proxy_url() else
                    "모달에 표시된 '서버 공인 IP'를 빗썸 [API 관리 > IP 주소 등록]에 등록하세요. "
                    "PaaS 의 아웃바운드 IP 가 공용 대역이면 등록이 불가능하므로, "
                    "고정 IP 프록시(BITHUMB_PROXY_URL)를 쓰는 방법이 있습니다.")
            return (f"빗썸이 요청 IP를 거부했습니다. {hint} "
                    f"(원문: v2={diag.get('v2')}, v1={diag.get('v1')})")
        if "access key" in blob or "invalid" in blob or "auth data" in blob or "5300" in blob:
            return ("API 키 인증 실패. Connect Key/Secret Key를 다시 확인하고, "
                    "키에 '자산조회' 권한이 있는지 확인하세요. "
                    f"(원문: v2={diag.get('v2')}, v1={diag.get('v1')})")
        return f"빗썸 인증 실패. API 2.0 응답={diag.get('v2')} / API 1.0 응답={diag.get('v1')}"

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

    @classmethod
    def egress_ip(cls) -> dict:
        """인증 요청이 실제로 나가는 공인 IP. 이 값이 빗썸에 등록해야 하는 IP 다.
        프록시가 설정돼 있으면 프록시를 경유한 IP 를 돌려준다."""
        proxies = cls._proxies()
        info = {
            "proxyConfigured": bool(proxies),
            "proxyHost": None,
            "ip": None,
            "error": None,
        }
        if proxies:
            # 자격증명이 로그·화면에 새지 않도록 호스트만 남긴다
            try:
                from urllib.parse import urlparse
                pr = urlparse(cls.proxy_url())
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
