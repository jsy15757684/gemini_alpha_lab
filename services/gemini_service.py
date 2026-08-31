"""Gemini AI 분석 및 자동매매 서비스.

Google Gemini API 를 사용하여 빗썸 원화마켓 코인의 시세, 캔들, 기술적 지표를
종합 분석하고 실시간 매매 신호(BUY / SELL / HOLD)와 판단 근거를 생성한다.

원칙:
  1) API 키가 없거나 호출 실패 시 임의 판단을 내리지 않고 HOLD(관망)로 안전하게 처리한다.
  2) 모든 AI 판단에는 신뢰도(Confidence), 위험도(Risk Level), 상세 근거(Reasons)가 포함된다.
  3) API 응답은 엄격한 JSON 구조로 파싱된다.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import requests

from services import bithumb
from services.strategy import StrategyParams, compute_indicators

logger = logging.getLogger(__name__)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(CURRENT_DIR), "data")
GEMINI_KEY_FILE = os.path.join(DATA_DIR, "gemini_key.json")

DEFAULT_MODEL = "gemini-2.5-flash"
SUPPORTED_MODELS = [
    "gemini-2.5-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]


class GeminiKeyStore:
    """Gemini API 키 보관 및 관리."""

    def __init__(self):
        self._env_key = os.getenv("GEMINI_API_KEY", "").strip()
        self._model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip()
        self._disk_key = ""
        self._load_disk()

    def _load_disk(self):
        try:
            if os.path.exists(GEMINI_KEY_FILE):
                with open(GEMINI_KEY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._disk_key = data.get("apiKey", "").strip()
                    if data.get("model"):
                        self._model = data.get("model").strip()
        except Exception as e:
            logger.warning(f"Gemini 키 파일 로드 실패: {e}")

    @property
    def api_key(self) -> str:
        return self._env_key or self._disk_key

    @property
    def model(self) -> str:
        return self._model or DEFAULT_MODEL

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def source(self) -> str:
        if self._env_key:
            return "env"
        if self._disk_key:
            return "disk"
        return "none"

    def status(self) -> Dict[str, Any]:
        key = self.api_key
        masked = f"{key[:6]}...{key[-4:]}" if len(key) >= 10 else ("***" if key else "")
        return {
            "configured": self.configured,
            "source": self.source,
            "maskedKey": masked,
            "model": self.model,
            "supportedModels": SUPPORTED_MODELS,
            "readOnly": bool(self._env_key),
        }

    def save(self, api_key: str, model: Optional[str] = None):
        if self._env_key:
            raise PermissionError("GEMINI_API_KEY 환경변수가 설정되어 있어 변경할 수 없습니다.")
        os.makedirs(DATA_DIR, exist_ok=True)
        self._disk_key = api_key.strip()
        if model and model in SUPPORTED_MODELS:
            self._model = model.strip()
        with open(GEMINI_KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({"apiKey": self._disk_key, "model": self._model}, f, ensure_ascii=False, indent=2)

    def clear(self):
        if self._env_key:
            raise PermissionError("GEMINI_API_KEY 환경변수가 설정되어 있어 삭제할 수 없습니다.")
        self._disk_key = ""
        if os.path.exists(GEMINI_KEY_FILE):
            os.remove(GEMINI_KEY_FILE)

    def test_connection(self, api_key: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
        target_key = api_key or self.api_key
        target_model = model or self.model or DEFAULT_MODEL
        if not target_key:
            return {"success": False, "message": "Gemini API 키가 설정되지 않았습니다."}

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={target_key}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": "빗썸 암호화폐 자동매매 연결 테스트입니다. JSON 형식으로 {\"status\": \"ok\", \"message\": \"connected\"} 만 응답하세요."}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                return {"success": True, "message": f"Gemini API 연결 성공 ({target_model})", "raw": text}
            else:
                err_msg = resp.text
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("error", {}).get("message", resp.text)
                except Exception:
                    pass
                return {"success": False, "message": f"Gemini API 오류 ({resp.status_code}): {err_msg}"}
        except Exception as e:
            return {"success": False, "message": f"Gemini 연결 시도 중 네트워크 오류: {str(e)}"}


gemini_keystore = GeminiKeyStore()

# ── AI 분석 캐시 (중복 호출 방지) ──
_ANALYSIS_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SEC = 25.0


def _build_analysis_prompt(coin: str, interval: str, current_price: float,
                           bars: List[Dict[str, Any]], pos_open: bool,
                           entry_price: Optional[float] = None) -> str:
    """Gemini AI에게 전달할 종합 기술적 지표 및 시황 데이터 프롬프트 구성."""
    coin_name = bithumb.COINS.get(coin, coin)

    # 최근 15개 봉의 데이터 요약
    recent_bars = bars[-15:] if len(bars) >= 15 else bars
    bars_summary = []
    for b in recent_bars:
        bars_summary.append({
            "time": b.get("time"),
            "open": b.get("open"),
            "high": b.get("high"),
            "low": b.get("low"),
            "close": b.get("close"),
            "volume": round(b.get("volume", 0), 4),
            "rsi": round(b.get("rsi", 0), 2) if b.get("rsi") is not None else None,
            "smaFast": round(b.get("smaFast", 0), 2) if b.get("smaFast") is not None else None,
            "smaSlow": round(b.get("smaSlow", 0), 2) if b.get("smaSlow") is not None else None,
            "macd": round(b.get("macd", 0), 2) if b.get("macd") is not None else None,
            "macdSignal": round(b.get("macdSignal", 0), 2) if b.get("macdSignal") is not None else None,
            "bbUpper": round(b.get("bbUpper", 0), 2) if b.get("bbUpper") is not None else None,
            "bbLower": round(b.get("bbLower", 0), 2) if b.get("bbLower") is not None else None,
        })

    last_bar = bars[-1] if bars else {}
    rsi_cur = last_bar.get("rsi")
    rsi_str = f"{rsi_cur:.1f}" if rsi_cur is not None else "계산불가"

    pos_info = "현재 포지션 없음 (현금 100% 보유 중)"
    if pos_open and entry_price:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        pos_info = f"현재 포지션 보유 중 (진입가: {entry_price:,.0f}원, 현재 수익률: {pnl_pct:+.2f}%)"

    prompt = f"""당신은 가상자산 퀀트 트레이딩 및 시장 분석 최고 전문가인 'Gemini Alpha AI' 입니다.
대한민국 원화 거래소(빗썸)의 {coin_name}({coin}/KRW)에 대한 실시간 시세 및 기술적 지표 데이터를 제공합니다.

[현재 시장 상황]
- 대상 종목: {coin_name} ({coin}/KRW)
- 캔들 간격: {interval}
- 현재 실시간 가격: {current_price:,.0f} 원
- 현재 RSI(14): {rsi_str}
- 보유 상태: {pos_info}

[최근 캔들 및 지표 추이 (과거 -> 최신순)]
{json.dumps(bars_summary, ensure_ascii=False, indent=2)}

[요청 사항]
위 기술적 데이터(RSI, 이동평균선 정배열/역배열, MACD 오실레이터, 볼린저밴드 위치, 거래량 추이, 지지/저항선)를 정밀 분석하여 매매 판단을 내려주세요.
반드시 아래 JSON 스키마를 엄격히 준수하여 응답하세요. 다른 설명이나 마크다운 백틱(```) 없이 오직 유효한 JSON 문자열만 출력해야 합니다.

[응답 JSON 스키마]
{{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0~100 사이의 정수 (신뢰도 점수, 75 이상이면 강한 확신),
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "target_profit_pct": 권장 익절 목표 수익률(%),
  "stop_loss_pct": 권장 손절 비율(%),
  "summary": "한 줄 요약 (예: 단기 바닥 확인 후 거래량 동반 반등 추세 진입)",
  "reasons": [
    "핵심 근거 1 (예: RSI 30 부근에서 반등 시작)",
    "핵심 근거 2 (예: MACD 히스토그램 양전환)",
    "핵심 근거 3 (예: 볼린저 하단 지지 확인)"
  ],
  "market_sentiment": "BULLISH" | "BEARISH" | "NEUTRAL"
}}
"""
    return prompt


def analyze_coin(coin: str, interval: str = "1h",
                 custom_bars: Optional[List[Dict[str, Any]]] = None,
                 current_price: Optional[float] = None,
                 pos_open: bool = False,
                 entry_price: Optional[float] = None,
                 force_refresh: bool = False) -> Dict[str, Any]:
    """단일 코인에 대해 Gemini AI 실시간 분석 및 매매 신호 생성."""
    norm_coin = bithumb.normalize_coin(coin)
    if not norm_coin:
        return {
            "success": False,
            "action": "HOLD",
            "confidence": 0,
            "summary": f"유효하지 않은 코인 심볼: {coin}",
            "reasons": ["지원되지 않는 코인입니다."],
            "risk_level": "HIGH",
            "market_sentiment": "NEUTRAL",
        }

    cache_key = f"{norm_coin}_{interval}_{pos_open}"
    now = time.time()
    if not force_refresh and cache_key in _ANALYSIS_CACHE:
        cached = _ANALYSIS_CACHE[cache_key]
        if now - cached.get("_cached_at", 0) < CACHE_TTL_SEC:
            return cached

    if not gemini_keystore.configured:
        return {
            "success": False,
            "action": "HOLD",
            "confidence": 0,
            "coin": norm_coin,
            "name": bithumb.COINS.get(norm_coin, norm_coin),
            "summary": "Gemini API 키가 설정되지 않았습니다.",
            "reasons": ["'빗썸 계정' 탭 또는 .env 에서 GEMINI_API_KEY 를 등록해주세요."],
            "risk_level": "LOW",
            "market_sentiment": "NEUTRAL",
            "target_profit_pct": 3.0,
            "stop_loss_pct": 2.0,
        }

    try:
        # 시세 및 캔들 지표 준비
        if current_price is None:
            current_price = bithumb.get_price(norm_coin)

        if custom_bars is not None:
            bars = custom_bars
        else:
            raw_candles = bithumb.get_candles(norm_coin, interval, limit=100)
            bars = compute_indicators(raw_candles, StrategyParams())

        if not bars:
            return {
                "success": False,
                "action": "HOLD",
                "confidence": 0,
                "coin": norm_coin,
                "summary": "캔들 데이터를 수신하지 못했습니다.",
                "reasons": ["거래소 데이터 수신 실패"],
            }

        prompt = _build_analysis_prompt(norm_coin, interval, current_price, bars, pos_open, entry_price)
        target_model = gemini_keystore.model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={gemini_keystore.api_key}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json"
            }
        }

        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            err_msg = f"Gemini API 오류 ({resp.status_code})"
            try:
                err_msg = resp.json().get("error", {}).get("message", err_msg)
            except Exception:
                pass
            return {
                "success": False,
                "action": "HOLD",
                "confidence": 0,
                "coin": norm_coin,
                "name": bithumb.COINS.get(norm_coin, norm_coin),
                "summary": f"AI 분석 실패: {err_msg}",
                "reasons": [err_msg],
                "risk_level": "MEDIUM",
                "market_sentiment": "NEUTRAL",
            }

        data = resp.json()
        raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
        parsed = json.loads(raw_text)

        action = str(parsed.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"

        confidence = int(parsed.get("confidence", 50))
        confidence = max(0, min(100, confidence))

        result = {
            "success": True,
            "coin": norm_coin,
            "name": bithumb.COINS.get(norm_coin, norm_coin),
            "interval": interval,
            "current_price": current_price,
            "action": action,
            "confidence": confidence,
            "risk_level": parsed.get("risk_level", "MEDIUM"),
            "target_profit_pct": float(parsed.get("target_profit_pct", 3.5)),
            "stop_loss_pct": float(parsed.get("stop_loss_pct", 2.0)),
            "summary": parsed.get("summary", ""),
            "reasons": parsed.get("reasons", []),
            "market_sentiment": parsed.get("market_sentiment", "NEUTRAL"),
            "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": target_model,
            "_cached_at": now,
        }

        _ANALYSIS_CACHE[cache_key] = result
        return result

    except Exception as e:
        logger.exception(f"[{norm_coin}] Gemini 분석 중 예외 발생: {e}")
        return {
            "success": False,
            "action": "HOLD",
            "confidence": 0,
            "coin": norm_coin,
            "name": bithumb.COINS.get(norm_coin, norm_coin),
            "summary": f"AI 분석 처리 오류: {str(e)}",
            "reasons": [str(e)],
            "risk_level": "HIGH",
            "market_sentiment": "NEUTRAL",
        }


def scan_all_coins(interval: str = "1h") -> Dict[str, Any]:
    """빗썸 원화마켓 5개 코인 전체를 일괄 AI 분석하여 스캔 결과 및 추천 순위 반환."""
    results = []
    for coin_code in bithumb.COINS.keys():
        res = analyze_coin(coin_code, interval=interval, force_refresh=True)
        results.append(res)

    def sort_key(item):
        action_weight = {"BUY": 3, "HOLD": 2, "SELL": 1}.get(item.get("action", "HOLD"), 0)
        return (action_weight, item.get("confidence", 0))

    ranked = sorted(results, key=sort_key, reverse=True)

    return {
        "success": True,
        "interval": interval,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": gemini_keystore.model,
        "results": ranked
    }
