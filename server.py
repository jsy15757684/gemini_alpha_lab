"""빗썸 원화 자동매매 콘솔 — API 서버.

범위: 빗썸 원화마켓 5종의 시세 / 전략 백테스트 / 자동매매 봇. 그 외 기능은 없다.

인증: 모든 데이터 API 는 세션 뒤에 있다. APP_ACCESS_PASSWORD 가 없으면
열린 상태가 아니라 잠긴 상태로 실패한다(fail closed).
"""

import os
import sys
import math
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from services import auth, backtest, bithumb
from services.keystore import keystore
from services.strategy import StrategyParams, compute_indicators
from services.trader import MAX_ACTIVE_BOTS, TooManyBots, bot_manager

app = FastAPI(title="빗썸 원화 자동매매 콘솔", version="4.0.0")
app.add_middleware(GZipMiddleware, minimum_size=500)

_origins = [o.strip() for o in os.getenv("APP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

PUBLIC_API_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_API_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    if not auth.is_configured():
        return JSONResponse(status_code=503, content={
            "detail": "서버에 APP_ACCESS_PASSWORD 가 설정되지 않아 모든 데이터 API 가 잠겨 있습니다.",
            "code": "AUTH_NOT_CONFIGURED"})
    if not auth.validate_session(request.cookies.get(auth.COOKIE_NAME)):
        return JSONResponse(status_code=401,
                            content={"detail": "로그인이 필요합니다.", "code": "AUTH_REQUIRED"})
    return await call_next(request)


@app.on_event("startup")
def _startup_log():
    logger.info(auth.password_debug_line())
    if auth.is_configured() and auth.password_strength_warning():
        logger.warning(auth.password_strength_warning())
    ks = keystore.status()
    logger.info(f"빗썸 키: {'등록됨(' + ks['source'] + ')' if ks['connected'] else '미등록'}")


# ───────────────────────── 인증 ─────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.get("/api/health")
def health():
    return {"status": "ok", "app": "빗썸 원화 자동매매 콘솔", "version": "4.0.0"}


@app.get("/api/auth/status")
def auth_status(request: Request):
    configured = auth.is_configured()
    return {"configured": configured,
            "authenticated": configured and auth.validate_session(
                request.cookies.get(auth.COOKIE_NAME)),
            "lockedForSeconds": int(auth.lock_remaining(auth.client_ip(request))),
            "warning": auth.password_strength_warning()}


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    ip = auth.client_ip(request)
    if not auth.is_configured():
        raise HTTPException(503, "서버에 APP_ACCESS_PASSWORD 가 설정되지 않았습니다.")
    locked = auth.lock_remaining(ip)
    if locked > 0:
        raise HTTPException(429, f"로그인 시도가 너무 많습니다. {math.ceil(locked / 60)}분 후 다시 시도하세요.")
    if not auth.verify_password(req.password):
        remaining = auth.register_failure(ip)
        logger.warning(f"로그인 실패 ip={ip}")
        if remaining > 0:
            raise HTTPException(429, f"로그인 시도 한도를 초과했습니다. {math.ceil(remaining / 60)}분 후 다시 시도하세요.")
        raise HTTPException(401, f"비밀번호가 올바르지 않습니다. (남은 시도 {auth.attempts_left(ip)}회)")

    auth.clear_failures(ip)
    token, expires = auth.create_session()
    logger.info(f"로그인 성공 ip={ip}")
    resp = JSONResponse({"success": True, "expiresAt": int(expires)})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=int(auth.SESSION_TTL_SEC),
                    httponly=True, samesite="strict",
                    secure=auth.is_https(request), path="/")
    return resp


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))
    resp = JSONResponse({"success": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# ───────────────────────── 시세 ─────────────────────────

@app.get("/api/coins")
def coins():
    return {"coins": [{"code": c, "name": n} for c, n in bithumb.COINS.items()],
            "intervals": bithumb.INTERVALS}


@app.get("/api/prices")
def prices():
    """5종 현재가. 하나가 실패해도 나머지는 돌려주고, 실패는 실패로 표시한다."""
    out = []
    for c in bithumb.COINS:
        try:
            out.append(bithumb.get_ticker(c))
        except bithumb.BithumbError as e:
            out.append({"coin": c, "name": bithumb.COINS[c], "error": e.message})
    return {"prices": out}


@app.get("/api/candles")
def candles(coin: str = Query(...), interval: str = Query("1h"),
            params: Optional[str] = Query(None)):
    """캔들 + 지표. 차트와 전략 확인용."""
    try:
        p = StrategyParams()
        rows = bithumb.get_candles(coin, interval, limit=200)
        bars = compute_indicators(rows, p)
    except bithumb.BithumbError as e:
        raise HTTPException(502, e.message)
    return {"coin": bithumb.normalize_coin(coin), "interval": interval,
            "candles": bars, "params": p.to_dict(), "dataSource": "bithumb-candles"}


# ───────────────────────── 백테스트 ─────────────────────────

class BacktestRequest(BaseModel):
    coin: str = "BTC"
    interval: str = "1h"
    initialKrw: float = 1_000_000.0
    params: Dict[str, Any] = {}


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    try:
        return backtest.run(req.coin, req.interval, req.params, req.initialKrw)
    except bithumb.BithumbError as e:
        raise HTTPException(502, e.message)


# ───────────────────────── 봇 ─────────────────────────

class DeployRequest(BaseModel):
    coin: str = "BTC"
    interval: str = "1h"
    mode: str = "PAPER"
    capitalKrw: float = 1_000_000.0
    params: Dict[str, Any] = {}


class BotIdRequest(BaseModel):
    botId: str


@app.post("/api/bot/deploy")
def deploy_bot(req: DeployRequest):
    coin = bithumb.normalize_coin(req.coin)
    if not coin:
        raise HTTPException(400, f"빗썸 원화마켓에 없는 코인입니다: {req.coin}")
    if req.interval not in bithumb.INTERVALS:
        raise HTTPException(400, f"지원하지 않는 캔들 간격입니다: {req.interval}")
    if req.capitalKrw < 10_000:
        raise HTTPException(400, "운용 자본은 10,000원 이상이어야 합니다.")

    mode = req.mode.upper()
    if mode not in ("PAPER", "LIVE"):
        raise HTTPException(400, "mode 는 PAPER 또는 LIVE 여야 합니다.")

    # 시세를 못 받으면 봇을 띄우지 않는다.
    try:
        bithumb.get_price(coin)
    except bithumb.BithumbError as e:
        raise HTTPException(503, f"{coin} 시세를 받지 못해 봇을 가동할 수 없습니다: {e.message}")

    if mode == "LIVE":
        if not keystore.account.configured:
            raise HTTPException(400, "실전(LIVE) 가동 전에 빗썸 API 키를 등록해야 합니다.")
        test = keystore.account.test_connection()
        if not test.get("success"):
            raise HTTPException(400, f"빗썸 실계좌 연결에 실패해 실전 가동을 중단했습니다. {test.get('message')}")
        if test.get("krwAvailable", 0) < req.capitalKrw:
            raise HTTPException(400,
                f"빗썸 주문가능 원화({test.get('krwAvailable', 0):,.0f}원)가 "
                f"운용 자본({req.capitalKrw:,.0f}원)보다 적습니다.")

    try:
        bot = bot_manager.deploy(coin, req.interval, mode, req.capitalKrw,
                                 req.params, keystore.account)
    except TooManyBots as e:
        raise HTTPException(429, str(e))
    return bot.status()


@app.get("/api/bot/list")
def list_bots():
    return {"bots": bot_manager.all_status(),
            "activeCount": bot_manager.active_count(), "maxActive": MAX_ACTIVE_BOTS}


@app.post("/api/bot/stop")
def stop_bot(req: BotIdRequest):
    if not bot_manager.stop(req.botId):
        raise HTTPException(404, f"봇을 찾을 수 없습니다: {req.botId}")
    return {"success": True, "botId": req.botId}


@app.post("/api/bot/delete")
def delete_bot(req: BotIdRequest):
    if not bot_manager.delete(req.botId):
        raise HTTPException(404, f"봇을 찾을 수 없습니다: {req.botId}")
    return {"success": True, "botId": req.botId}


@app.post("/api/bot/stop_all")
def stop_all_bots():
    return {"success": True, "stoppedCount": bot_manager.stop_all()}


# ───────────────────────── 빗썸 계정 ─────────────────────────

class KeyRequest(BaseModel):
    apiKey: str
    secretKey: str


@app.get("/api/account")
def account_status():
    st = keystore.status()
    if keystore.account.configured:
        try:
            bal = keystore.account.get_balance()
            st.update({"balanceOk": True, "apiVersion": bal["apiVersion"],
                       "krwAvailable": bal["krwAvailable"], "krwTotal": bal["krwTotal"],
                       "coins": bal["coins"]})
        except bithumb.BithumbError as e:
            st.update({"balanceOk": False, "error": e.message})
    return st


@app.post("/api/account/test")
def test_account(req: KeyRequest):
    return bithumb.BithumbAccount(req.apiKey, req.secretKey).test_connection()


@app.post("/api/account/save")
def save_account(req: KeyRequest):
    result = bithumb.BithumbAccount(req.apiKey, req.secretKey).test_connection()
    if not result.get("success"):
        # 인증을 통과하지 못한 키는 저장하지 않는다.
        raise HTTPException(400, result.get("message", "빗썸 인증에 실패했습니다."))
    try:
        keystore.save(req.apiKey.strip(), req.secretKey.strip())
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return {"success": True, **keystore.status()}


@app.post("/api/account/clear")
def clear_account():
    try:
        keystore.clear()
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return {"success": True, **keystore.status()}


@app.get("/api/system/egress_ip")
def egress_ip():
    """빗썸 [API 관리 > IP 주소 등록] 에 넣어야 하는 IP."""
    info = bithumb.egress_ip()
    return {**info,
            "registerThisIp": info.get("ip"),
            "hint": ("프록시 IP 를 확인할 수 없습니다. 프록시가 살아 있는지 점검하세요."
                     if info.get("proxyConfigured") and not info.get("ip") else None)}


# ───────────────────────── 정적 파일 ─────────────────────────

static_dir = os.path.join(CURRENT_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def index():
    f = os.path.join(static_dir, "index.html")
    if os.path.exists(f):
        return FileResponse(f, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return HTMLResponse("<h1>빗썸 원화 자동매매 콘솔</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.getenv("PORT", "8888")), reload=True)
