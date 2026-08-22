import os
import sys
import math
import logging
import requests
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

# 현재 디렉토리 기준 import
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from services.market_service import get_asset_quote, get_chart_data, resolve_symbol, POPULAR_ASSETS, SYMBOL_DICTIONARY
from services.backtester import QuantBacktester
from services.gemini_ai import GeminiAIService
from services.auto_trader import bot_manager, broker_manager
from services.market_feed import get_live_price
from services import auth
from services.guru_service import get_all_gurus, get_guru_by_id
from services.market_trading_bots import get_marketplace_bots, get_bot_by_id

app = FastAPI(
    title="Gemini Alpha Lab",
    description="AI Quantitative Investment & Auto-Trading Operating System",
    version="2.0.0"
)

# Gzip High-Speed Compression (압축 전송)
app.add_middleware(GZipMiddleware, minimum_size=500)

# 세션 쿠키를 쓰므로 와일드카드 오리진을 허용하면 안 된다.
# (브라우저도 allow_origins=["*"] + credentials 조합은 거부한다)
# 기본값은 '동일 오리진만'. 필요하면 APP_ALLOWED_ORIGINS 에 콤마로 나열한다.
_allowed_origins = [o.strip() for o in os.getenv("APP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# ===================== 인증 게이트 =====================
# 인증 없이 열어두는 경로. 이 목록에 없는 /api/* 는 전부 세션이 있어야 한다.
# (라우트를 새로 추가할 때 인증을 '깜빡하는' 사고를 막기 위해 화이트리스트 방식)
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path

    # 정적 파일과 index.html 은 열어둔다 — 로그인 화면 자체를 띄워야 하고,
    # 이 파일들에는 계좌 데이터가 없다. 데이터는 전부 /api/* 뒤에 있다.
    if not path.startswith("/api/"):
        return await call_next(request)

    if path in PUBLIC_API_PATHS:
        return await call_next(request)

    if request.method == "OPTIONS":
        return await call_next(request)

    if not auth.is_configured():
        # 비밀번호 미설정 시 '열린 상태' 가 아니라 '잠긴 상태' 로 실패한다.
        return JSONResponse(
            status_code=503,
            content={
                "detail": "서버에 APP_ACCESS_PASSWORD 환경변수가 설정되지 않아 모든 데이터 API가 잠겨 있습니다.",
                "code": "AUTH_NOT_CONFIGURED",
            },
        )

    if not auth.validate_session(request.cookies.get(auth.COOKIE_NAME)):
        return JSONResponse(
            status_code=401,
            content={"detail": "로그인이 필요합니다.", "code": "AUTH_REQUIRED"},
        )

    return await call_next(request)


class LoginRequest(BaseModel):
    password: str


@app.get("/api/auth/status")
def auth_status(request: Request):
    """로그인 화면이 부팅 시 호출한다. 비밀번호 값은 절대 내보내지 않는다."""
    configured = auth.is_configured()
    return {
        "configured": configured,
        "authenticated": configured and auth.validate_session(request.cookies.get(auth.COOKIE_NAME)),
        "lockedForSeconds": int(auth.lock_remaining(auth.client_ip(request))),
        "warning": auth.password_strength_warning(),
    }


@app.post("/api/auth/login")
def auth_login(req: LoginRequest, request: Request):
    ip = auth.client_ip(request)

    if not auth.is_configured():
        raise HTTPException(
            status_code=503,
            detail="서버에 APP_ACCESS_PASSWORD 환경변수가 설정되지 않았습니다. Render 대시보드에서 먼저 설정하세요.",
        )

    locked = auth.lock_remaining(ip)
    if locked > 0:
        raise HTTPException(
            status_code=429,
            detail=f"로그인 시도가 너무 많습니다. {math.ceil(locked / 60)}분 후 다시 시도하세요.",
        )

    if not auth.verify_password(req.password):
        remaining_lock = auth.register_failure(ip)
        logger.warning(f"로그인 실패 ip={ip}")
        if remaining_lock > 0:
            raise HTTPException(
                status_code=429,
                detail=f"로그인 시도 한도를 초과했습니다. {math.ceil(remaining_lock / 60)}분 후 다시 시도하세요.",
            )
        raise HTTPException(
            status_code=401,
            detail=f"비밀번호가 올바르지 않습니다. (남은 시도 {auth.attempts_left(ip)}회)",
        )

    auth.clear_failures(ip)
    token, expires_at = auth.create_session()
    logger.info(f"로그인 성공 ip={ip}")

    resp = JSONResponse({"success": True, "expiresAt": int(expires_at)})
    resp.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=int(auth.SESSION_TTL_SEC),
        httponly=True,               # JS 가 읽을 수 없다 (XSS 로 탈취 방지)
        samesite="strict",           # 외부 사이트발 요청에 쿠키가 실리지 않는다 (CSRF 방지)
        secure=auth.is_https(request),
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))
    resp = JSONResponse({"success": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


gemini_ai = GeminiAIService()
backtester = QuantBacktester(initial_capital=10000.0)

# Request Models
class DeployBotRequest(BaseModel):
    symbol: str = "NVDA"
    mode: str = "PAPER" # PAPER or LIVE
    broker: str = "ALPACA_PAPER"
    capital: float = 10000.0
    strategyParams: Dict[str, Any]

class StopBotRequest(BaseModel):
    botId: str

class ConnectBrokerRequest(BaseModel):
    brokerCode: str
    apiKey: str
    secretKey: Optional[str] = ""
    accountNo: Optional[str] = ""

class DisconnectBrokerRequest(BaseModel):
    brokerCode: str

class BacktestRequest(BaseModel):
    symbol: str = "NVDA"
    strategyType: str = "custom"
    fastMa: int = 5
    slowMa: int = 20
    rsiBuy: float = 35.0
    rsiSell: float = 70.0
    takeProfitPct: float = 10.0
    stopLossPct: float = 5.0
    period: str = "1y"

class ParseStrategyRequest(BaseModel):
    userPrompt: str

class GenerateReportRequest(BaseModel):
    symbol: str
    quote: Dict[str, Any]
    backtest: Dict[str, Any]
    sentiment: Dict[str, Any]

# Search Autocomplete API
@app.get("/api/search")
def search_symbols(q: str = Query("", description="Search query")):
    query = q.strip().lower()
    if not query:
        return {"results": POPULAR_ASSETS}

    results = []
    seen = set()
    for name, sym in SYMBOL_DICTIONARY.items():
        if query in name.lower() or query in sym.lower():
            if sym not in seen:
                seen.add(sym)
                results.append({"name": name, "symbol": sym})
                if len(results) >= 8:
                    break

    return {"results": results}

# Instant All-In-One Bundle API
@app.get("/api/symbol/bundle")
def get_symbol_bundle(symbol: str = Query("NVDA")):
    resolved = resolve_symbol(symbol)
    quote = get_asset_quote(resolved)
    chart = get_chart_data(resolved, timeframe="6mo")
    sentiment = gemini_ai.analyze_sentiment_and_news(resolved, quote)
    financials = gemini_ai.analyze_filing_and_financials(resolved, quote)
    backtest = backtester.run_backtest(resolved, fast_ma=5, slow_ma=20, take_profit_pct=12.0, stop_loss_pct=5.0)

    return {
        "symbol": resolved,
        "quote": quote,
        "chart": chart,
        "sentiment": sentiment,
        "financials": financials,
        "backtest": backtest
    }

# Wall Street Guru Endpoints
@app.get("/api/gurus")
def get_gurus_endpoint():
    return {"gurus": get_all_gurus()}

@app.get("/api/gurus/{guru_id}")
def get_single_guru_endpoint(guru_id: str):
    guru = get_guru_by_id(guru_id)
    if guru is None:
        raise HTTPException(status_code=404, detail=f"존재하지 않는 구루 id: {guru_id}")
    return guru

# AI Marketplace & Leaderboard Endpoints
@app.get("/api/marketplace/bots")
def get_marketplace_endpoint():
    return {"bots": get_marketplace_bots()}

# API Endpoints
@app.get("/api/health")
def health_check():
    return {"status": "ok", "app": "Gemini Alpha Lab", "version": "1.0.0"}

@app.get("/api/popular")
def get_popular_assets():
    return {"assets": POPULAR_ASSETS}

@app.get("/api/quote")
def quote_endpoint(symbol: str = Query(..., description="Stock or Crypto Symbol e.g., NVDA, TSLA, BTC-USD, 005930.KS")):
    try:
        quote = get_asset_quote(symbol.upper())
        return quote
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chart")
def chart_endpoint(
    symbol: str = Query(..., description="Symbol"),
    timeframe: str = Query("6mo", description="1mo, 3mo, 6mo, 1y, 2y")
):
    try:
        chart_data = get_chart_data(symbol.upper(), timeframe=timeframe)
        return chart_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sentiment")
def sentiment_endpoint(symbol: str = Query(...)):
    try:
        sym = symbol.upper()
        quote = get_asset_quote(sym)
        sentiment = gemini_ai.analyze_sentiment_and_news(sym, quote)
        return {"symbol": sym, "quote": quote, "sentiment": sentiment}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/financials")
def financials_endpoint(symbol: str = Query(...)):
    try:
        sym = symbol.upper()
        quote = get_asset_quote(sym)
        analysis = gemini_ai.analyze_filing_and_financials(sym, quote)
        return analysis
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/backtest")
def run_backtest_endpoint(req: BacktestRequest):
    try:
        result = backtester.run_backtest(
            symbol=req.symbol.upper(),
            strategy_type=req.strategyType,
            fast_ma=req.fastMa,
            slow_ma=req.slowMa,
            rsi_buy=req.rsiBuy,
            rsi_sell=req.rsiSell,
            take_profit_pct=req.takeProfitPct,
            stop_loss_pct=req.stopLossPct,
            period=req.period
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/strategy/parse")
def parse_strategy_endpoint(req: ParseStrategyRequest):
    try:
        parsed = gemini_ai.parse_natural_language_strategy(req.userPrompt)
        return parsed
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/report/generate")
def generate_report_endpoint(req: GenerateReportRequest):
    try:
        report = gemini_ai.generate_premium_monetization_report(
            symbol=req.symbol.upper(),
            quote=req.quote,
            backtest=req.backtest,
            sentiment=req.sentiment
        )
        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Bot Endpoints
@app.post("/api/bot/deploy")
def deploy_bot_endpoint(req: DeployBotRequest):
    try:
        resolved_sym = resolve_symbol(req.symbol)

        # 실측 시세만 사용한다. 예전엔 조회가 실패하면 100.0 을 대입해
        # 존재하지 않는 가격으로 봇을 띄웠다.
        tick = get_live_price(resolved_sym, req.broker or "")
        init_price = float(tick["price"]) if tick else 0.0

        if init_price <= 0 and req.mode.upper() == "LIVE":
            raise HTTPException(
                status_code=503,
                detail=(f"{resolved_sym} 의 실시간 시세를 받지 못해 실전(LIVE) 봇을 가동할 수 없습니다. "
                        f"추정 가격으로 실주문을 내지 않습니다.")
            )

        q = get_asset_quote(resolved_sym)

        # Get AI sentiment
        sentiment = gemini_ai.analyze_sentiment_and_news(resolved_sym, q)
        sentiment_score = int(sentiment.get("sentimentScore", 75))

        bot = bot_manager.deploy_bot(
            symbol=resolved_sym,
            mode=req.mode,
            broker=req.broker,
            capital=float(req.capital),
            strategy_params=req.strategyParams,
            initial_price=init_price,
            sentiment_score=sentiment_score
        )
        return bot.get_status()
    except HTTPException:
        # 503(시세 없음) 같은 의도된 상태코드를 500 으로 덮어쓰지 않는다.
        raise
    except Exception as e:
        logger.error(f"Error deploying bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bot/list")
def list_bots_endpoint():
    return {"bots": bot_manager.get_all_bots()}

@app.post("/api/bot/stop")
def stop_bot_endpoint(req: StopBotRequest):
    success = bot_manager.stop_bot(req.botId)
    return {"success": success, "botId": req.botId}

@app.post("/api/bot/stop_all")
def stop_all_bots_endpoint():
    stopped_count = bot_manager.stop_all_bots()
    return {"success": True, "stoppedCount": stopped_count}

@app.post("/api/bot/delete")
def delete_bot_endpoint(req: StopBotRequest):
    success = bot_manager.delete_bot(req.botId)
    return {"success": success, "botId": req.botId}

# Broker Management Endpoints
@app.get("/api/broker/list")
def list_brokers_endpoint():
    return {"brokers": broker_manager.get_status_list()}

@app.post("/api/broker/connect")
def connect_broker_endpoint(req: ConnectBrokerRequest):
    success = broker_manager.save_key(
        broker_code=req.brokerCode,
        api_key=req.apiKey,
        secret_key=req.secretKey or "",
        account_no=req.accountNo or ""
    )
    return {"success": success, "broker": req.brokerCode}

@app.post("/api/broker/test_bithumb")
def test_bithumb_endpoint(req: ConnectBrokerRequest):
    result = broker_manager.test_bithumb_connection(
        api_key=req.apiKey,
        secret_key=req.secretKey or ""
    )
    return result

@app.get("/api/system/my_ip")
def get_my_ip_endpoint():
    """서버의 실제 공인 IP (Outbound Egress IP) 조회.
    빗썸 API 키의 [IP 주소 등록]란에는 반드시 '이 서버'가 실제로 나가는 IP를 넣어야 한다.
    조회에 실패하면 절대 임의의 IP를 반환하지 않는다 (잘못된 IP 등록을 유발하므로)."""
    providers = [
        ("https://api.ipify.org?format=json", "ip"),
        ("https://ifconfig.co/json", "ip"),
        ("https://api.myip.com", "ip"),
    ]
    errors = []
    for url, field in providers:
        try:
            data = requests.get(url, timeout=4).json()
            ip = str(data.get(field, "")).strip()
            if ip:
                return {"ip": ip, "source": url, "detected": True}
        except Exception as e:
            errors.append(f"{url}: {e}")
            continue

    logger.error(f"Outbound IP detection failed: {errors}")
    return JSONResponse(
        status_code=503,
        content={
            "ip": None,
            "detected": False,
            "message": "서버 공인 IP 자동 감지 실패. 배포 플랫폼(Render 등) 대시보드의 Outbound IP 목록을 직접 확인해 빗썸에 등록하세요.",
        },
    )

@app.post("/api/broker/disconnect")
def disconnect_broker_endpoint(req: DisconnectBrokerRequest):
    success = broker_manager.disconnect(req.brokerCode)
    return {"success": success, "broker": req.brokerCode}

# Static Files
static_dir = os.path.join(CURRENT_DIR, "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(
            index_file,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return HTMLResponse("<h1>Gemini Alpha Lab Server Running</h1>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8888, reload=True)
