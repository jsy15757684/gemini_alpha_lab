"""바이낸스 현물 API 클라이언트.

무전송 양방향 차익거래의 '해외 다리' 를 담당한다. 빗썸 쪽은 services/bithumb.py.

설계 원칙 (빗썸 클라이언트와 같다)
  · 받지 못한 값을 지어내지 않는다. 실패는 예외로 올린다.
  · 키는 로그에 남기지 않는다.
  · 주문 수량은 거래소 규격(stepSize)에 맞춰 내림한다. 규격을 어긴 주문은
    거부되고, 그 거부가 '한쪽 다리만 체결' 사고의 가장 흔한 원인이다.

테스트넷: BINANCE_TESTNET=1 이면 testnet.binance.vision 을 쓴다.
실계좌를 건드리기 전 검증용이며, 키도 테스트넷 전용 키가 따로 필요하다.
"""

import os
import time
import hmac
import math
import hashlib
import logging
import threading
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

LIVE_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"

# 이 프로그램이 다루는 종목 (빗썸 원화마켓과 겹치는 것만)
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}

_HTTP = requests.Session()
_FILTER_CACHE: Dict[str, Dict[str, float]] = {}
_FILTER_LOCK = threading.Lock()


class BinanceError(Exception):
    def __init__(self, message: str, payload: Any = None, code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.payload = payload
        self.code = code


def is_testnet() -> bool:
    return (os.getenv("BINANCE_TESTNET") or "").strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    return TESTNET_BASE if is_testnet() else LIVE_BASE


def symbol_for(coin: str) -> str:
    c = (coin or "").upper()
    if c not in SYMBOLS:
        raise BinanceError(f"바이낸스에서 다루지 않는 종목입니다: {coin}")
    return SYMBOLS[c]


# ── 공개 조회 ────────────────────────────────────────────────────

def _public(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        r = _HTTP.get(f"{base_url()}{path}", params=params or {}, timeout=10)
    except Exception as e:
        raise BinanceError(f"바이낸스 통신 오류: {e}")
    if r.status_code != 200:
        try:
            body = r.json()
            raise BinanceError(f"바이낸스 조회 실패: {body.get('msg', body)}",
                               body, body.get("code"))
        except BinanceError:
            raise
        except Exception:
            raise BinanceError(f"바이낸스 조회 실패 (HTTP {r.status_code})")
    return r.json()


def price(coin: str) -> float:
    """현재가(USDT)."""
    d = _public("/api/v3/ticker/price", {"symbol": symbol_for(coin)})
    try:
        return float(d["price"])
    except (KeyError, TypeError, ValueError):
        raise BinanceError(f"가격 응답을 해석할 수 없습니다: {d}")


def filters(coin: str, force: bool = False) -> Dict[str, float]:
    """종목의 주문 규격. stepSize / minQty / minNotional.

    주문 수량이 stepSize 의 배수가 아니면 거부된다(-1013). 이걸 맞추지 않으면
    한쪽 다리만 체결되는 사고가 난다.
    """
    sym = symbol_for(coin)
    with _FILTER_LOCK:
        if not force and sym in _FILTER_CACHE:
            return _FILTER_CACHE[sym]

    d = _public("/api/v3/exchangeInfo", {"symbol": sym})
    try:
        info = d["symbols"][0]
    except (KeyError, IndexError, TypeError):
        raise BinanceError(f"거래 규격을 받지 못했습니다: {sym}")
    if info.get("status") != "TRADING":
        raise BinanceError(f"{sym} 는 현재 거래 가능 상태가 아닙니다 ({info.get('status')})")

    by_type = {f["filterType"]: f for f in info.get("filters", [])}
    lot = by_type.get("LOT_SIZE", {})
    notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL") or {}
    out = {
        "stepSize": float(lot.get("stepSize") or 0.0),
        "minQty": float(lot.get("minQty") or 0.0),
        "minNotional": float(notional.get("minNotional") or 0.0),
        "baseAssetPrecision": float(info.get("baseAssetPrecision") or 8),
    }
    with _FILTER_LOCK:
        _FILTER_CACHE[sym] = out
    return out


def round_qty(coin: str, qty: float) -> float:
    """stepSize 배수로 내림한다. 올림하면 잔고를 넘겨 거부될 수 있다."""
    f = filters(coin)
    step = f["stepSize"]
    if step <= 0:
        return qty
    n = math.floor(qty / step)
    # 부동소수 오차로 step 배수가 미세하게 어긋나는 것을 막는다.
    decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))
    return round(n * step, decimals)


# ── 인증 ─────────────────────────────────────────────────────────

class BinanceAccount:
    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.api_key = (api_key or "").strip()
        self.secret_key = (secret_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def masked_key(self) -> str:
        k = self.api_key
        return (k[:3] + "******" + k[-3:]) if len(k) > 6 else ("******" if k else "")

    def _signed(self, method: str, path: str, params: Dict[str, Any]) -> Any:
        if not self.configured:
            raise BinanceError("바이낸스 API 키가 등록되지 않았습니다.")
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p.setdefault("recvWindow", 5000)
        query = urllib.parse.urlencode(p, doseq=True)
        sig = hmac.new(self.secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{base_url()}{path}?{query}&signature={sig}"
        try:
            r = _HTTP.request(method, url, headers={"X-MBX-APIKEY": self.api_key}, timeout=12)
        except Exception as e:
            raise BinanceError(f"바이낸스 통신 오류: {e}")
        try:
            body = r.json()
        except Exception:
            raise BinanceError(f"바이낸스 응답을 해석할 수 없습니다 (HTTP {r.status_code})")
        if r.status_code != 200:
            code = body.get("code") if isinstance(body, dict) else None
            msg = body.get("msg") if isinstance(body, dict) else str(body)
            raise BinanceError(self._explain(code, msg), body, code)
        return body

    @staticmethod
    def _explain(code: Optional[int], msg: str) -> str:
        hints = {
            -1021: "서버 시각이 바이낸스와 어긋났습니다 (timestamp 오류). 서버 시각을 동기화하세요.",
            -1022: "서명이 올바르지 않습니다. Secret Key 를 확인하세요.",
            -2014: "API Key 형식이 올바르지 않습니다.",
            -2015: "API Key 가 거부됐습니다 — 키가 틀렸거나, 권한이 없거나, IP 화이트리스트에 "
                   "이 서버 IP 가 없습니다.",
            -1013: "주문 수량/금액이 거래소 규격에 맞지 않습니다 (stepSize·최소주문액).",
            -2010: "주문이 거부됐습니다 — 잔고 부족이거나 규격 위반입니다.",
        }
        extra = hints.get(code)
        return f"{msg} ({code})" + (f" — {extra}" if extra else "")

    # ── 계좌 ──
    def balances(self) -> Dict[str, float]:
        """자산별 사용가능 수량. 0 인 자산은 제외한다."""
        d = self._signed("GET", "/api/v3/account", {})
        out: Dict[str, float] = {}
        for b in d.get("balances", []):
            try:
                free = float(b.get("free") or 0)
                locked = float(b.get("locked") or 0)
            except (TypeError, ValueError):
                continue
            if free + locked > 0:
                out[str(b.get("asset", "")).upper()] = free
        return out

    # ── 주문 ──
    def market_buy_quote(self, coin: str, usdt_amount: float) -> Dict[str, Any]:
        """USDT 금액만큼 시장가 매수 (quoteOrderQty).

        수량이 아니라 '쓸 금액' 을 지정하므로 stepSize 반올림 문제가 없다.
        """
        f = filters(coin)
        if usdt_amount < f["minNotional"]:
            raise BinanceError(
                f"주문 금액이 최소 기준보다 작습니다: ${usdt_amount:.2f} < ${f['minNotional']:.2f}")
        res = self._signed("POST", "/api/v3/order", {
            "symbol": symbol_for(coin), "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{usdt_amount:.8f}".rstrip("0").rstrip("."),
        })
        return self._fill_summary(res)

    def market_sell(self, coin: str, qty: float) -> Dict[str, Any]:
        """수량만큼 시장가 매도. stepSize 배수로 내림해서 보낸다."""
        q = round_qty(coin, qty)
        f = filters(coin)
        if q <= 0 or q < f["minQty"]:
            raise BinanceError(
                f"주문 수량이 최소 기준보다 작습니다: {q} < {f['minQty']}")
        res = self._signed("POST", "/api/v3/order", {
            "symbol": symbol_for(coin), "side": "SELL", "type": "MARKET",
            "quantity": f"{q:.8f}".rstrip("0").rstrip("."),
        })
        return self._fill_summary(res)

    @staticmethod
    def _fill_summary(res: Dict[str, Any]) -> Dict[str, Any]:
        """체결 결과를 정규화한다. 이론 수량이 아니라 '실제로 체결된 값' 을 쓴다."""
        try:
            filled = float(res.get("executedQty") or 0)
            quote = float(res.get("cummulativeQuoteQty") or 0)
        except (TypeError, ValueError):
            filled = quote = 0.0
        return {
            "orderId": res.get("orderId"),
            "status": res.get("status"),
            "filledQty": filled,
            "quoteQty": quote,
            "avgPrice": (quote / filled) if filled > 0 else 0.0,
            "raw": res,
        }


def account_from_env() -> BinanceAccount:
    return BinanceAccount(os.getenv("BINANCE_API_KEY", ""),
                          os.getenv("BINANCE_SECRET_KEY", ""))
