import os
import sys
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

# 현재 디렉토리 기준 import
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

from services.market_service import get_asset_quote, get_chart_data, resolve_symbol, POPULAR_ASSETS, SYMBOL_DICTIONARY
from services.backtester import QuantBacktester
from services.gemini_ai import GeminiAIService
from services.auto_trader import bot_manager, broker_manager
from services.guru_service import get_all_gurus, get_guru_by_id
from services.market_trading_bots import get_marketplace_bots, get_bot_by_id

app = FastAPI(
    title="Gemini Alpha Lab",
    description="AI Quantitative Investment & Auto-Trading Operating System",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return get_guru_by_id(guru_id)

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
        # Initial quote check
        q = get_asset_quote(resolved_sym)
        init_price = float(q.get("currentPrice", 0.0))
        if init_price <= 0:
            init_price = 100.0
        
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
        return FileResponse(index_file)
    return HTMLResponse("<h1>Gemini Alpha Lab Server Running</h1>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8888, reload=True)
