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

DEFAULT_MODEL = "gemini-flash-latest"
SUPPORTED_MODELS = [
    "gemini-flash-latest",
    "gemini-pro-latest",
    "gemini-flash-lite-latest",
]


class GeminiKeyStore:
    """Gemini API 키 보관 및 관리."""

    def __init__(self):
        self._env_key = os.getenv("GEMINI_API_KEY", "").strip()
        self._model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip()
        self._disk_key = ""
        self._load_dotenv_if_needed()
        self._load_disk()

    def _load_dotenv_if_needed(self):
        if not self._env_key:
            env_path = os.path.join(os.path.dirname(CURRENT_DIR), ".env")
            if os.path.exists(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("GEMINI_API_KEY="):
                                val = line.split("=", 1)[1].strip()
                                if val:
                                    self._env_key = val
                            elif line.startswith("GEMINI_MODEL="):
                                val = line.split("=", 1)[1].strip()
                                if val:
                                    self._model = val
                except Exception as e:
                    logger.warning(f".env 파일 로드 실패: {e}")

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

# ── AI 분석 캐시 및 세션 ──
_ANALYSIS_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SEC = 30.0

_HTTP_SESSION = requests.Session()


def _build_analysis_prompt(coin: str, interval: str, current_price: float,
                           bars: List[Dict[str, Any]], pos_open: bool,
                           entry_price: Optional[float] = None) -> str:
    """Gemini AI에게 전달할 종합 기술적 지표 및 시황 데이터 프롬프트 구성 (경량화)."""
    coin_name = bithumb.COINS.get(coin, coin)

    # 최근 10개 봉의 핵심 데이터만 슬림하게 요약
    recent_bars = bars[-10:] if len(bars) >= 10 else bars
    bars_summary = []
    for b in recent_bars:
        bars_summary.append({
            "t": b.get("time"),
            "c": b.get("close"),
            "v": round(b.get("volume", 0), 2),
            "rsi": round(b.get("rsi", 0), 1) if b.get("rsi") is not None else None,
            "ma_f": round(b.get("smaFast", 0), 1) if b.get("smaFast") is not None else None,
            "ma_s": round(b.get("smaSlow", 0), 1) if b.get("smaSlow") is not None else None,
            "macd": round(b.get("macd", 0), 1) if b.get("macd") is not None else None,
            "bb_l": round(b.get("bbLower", 0), 1) if b.get("bbLower") is not None else None,
            "bb_u": round(b.get("bbUpper", 0), 1) if b.get("bbUpper") is not None else None,
        })

    last_bar = bars[-1] if bars else {}
    rsi_cur = last_bar.get("rsi")
    rsi_str = f"{rsi_cur:.1f}" if rsi_cur is not None else "N/A"

    pos_info = "무포지션"
    if pos_open and entry_price:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        pos_info = f"보유중(진입가 {entry_price:,.0f}원, 손익 {pnl_pct:+.2f}%)"

    prompt = f"""당신은 월스트리트 최정상 가상자산 퀀트 트레이딩 알고리즘입니다.
빗썸 {coin_name}({coin}/KRW)의 실시간 지표 데이터를 퀀트 4대 팩터(모멘텀, 평균회귀, 변동성, 거래량)로 정밀 평가하여 JSON 매매 판단을 내려주세요.

[시장 데이터]
- 대상: {coin_name}({coin}), 주기: {interval}, 현재가: {current_price:,.0f}원, RSI: {rsi_str}, 상태: {pos_info}
- 최근 지표 추이 (c:종가, v:거래량, rsi:RSI, ma_f/s:단/장기이평, macd:MACD, bb_l/u:볼린저하/상단):
{json.dumps(bars_summary, ensure_ascii=False)}

[퀀트 평가 기준]
1. 평균회귀: RSI 30~35 반등 또는 볼린저 하단 지지 여부
2. 모멘텀: MACD 양전환 또는 단기 이평선 상승 탄력
3. 거래량: 신호 발생 시 거래량 동반 여부 (가짜 신호 필터링)
4. 손익비: 손절 -1.8% 대비 최소 +3.5% 이상 기대 수익률 확보 가능 여부

반드시 아래 JSON 스키마로만 엄격히 응답하세요:
{{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0~100 정수 (확신도),
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "target_profit_pct": 3.8,
  "stop_loss_pct": 1.8,
  "summary": "퀀트 관점 한 줄 요약",
  "reasons": ["팩터 1 평가 근거", "팩터 2 평가 근거"],
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
            "reasons": ["'연동 계정·설정' 탭 또는 .env 에서 GEMINI_API_KEY 를 등록해주세요."],
            "risk_level": "LOW",
            "market_sentiment": "NEUTRAL",
            "target_profit_pct": 3.0,
            "stop_loss_pct": 2.0,
        }

    try:
        if current_price is None:
            current_price = bithumb.get_price(norm_coin)

        if custom_bars is not None:
            bars = custom_bars
        else:
            raw_candles = bithumb.get_candles(norm_coin, interval, limit=60)
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
                "temperature": 0.1,
                "maxOutputTokens": 250,
                "response_mime_type": "application/json"
            }
        }

        resp = None
        for attempt in range(2):
            resp = _HTTP_SESSION.post(url, json=payload, timeout=45)
            if resp.status_code != 429:
                break
            # 429 할당량 초과 시 2초 대기 후 1회 재시도
            time.sleep(2.0)

        if resp.status_code != 200:
            err_msg = f"Gemini API 오류 ({resp.status_code})"
            try:
                err_msg = resp.json().get("error", {}).get("message", err_msg)
            except Exception:
                pass
            if resp.status_code == 429:
                err_msg = "Google Gemini 무료 할당량(분당 요청 한도)에 도달했습니다. 약 30초~1분 후 다시 스캔해 주세요."
            return {
                "success": False,
                "action": "HOLD",
                "confidence": 0,
                "coin": norm_coin,
                "name": bithumb.COINS.get(norm_coin, norm_coin),
                "summary": f"AI 분석 일시 지연: {err_msg}",
                "reasons": [err_msg],
                "risk_level": "LOW",
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
    """빗썸 원화마켓 5개 코인 전체를 순차 스로틀링(Throttling) 분석하여 스캔 결과 반환."""
    coins = list(bithumb.COINS.keys())
    results = []

    for i, c in enumerate(coins):
        # 무료 할당량(RPM) 초과 방지를 위해 코인 간 1.5초 간격 유지
        if i > 0:
            time.sleep(1.5)
        res = analyze_coin(c, interval=interval, force_refresh=True)
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
