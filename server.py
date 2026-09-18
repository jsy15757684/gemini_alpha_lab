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

# .env 파일이 있으면 환경변수로 자동 로드 (직접 실행 및 supervisor 대비)
_env_file = os.path.join(CURRENT_DIR, ".env")
if os.path.exists(_env_file):
    try:
        with open(_env_file, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip()
                if (_v.startswith('"') and _v.endswith('"')) or (_v.startswith("'") and _v.endswith("'")):
                    _v = _v[1:-1]
                # 빈 값은 넣지 않는다 — scripts/load_env.sh 와 같은 규칙이다.
                # os.getenv(name, default) 는 변수가 '있지만 빈' 경우 default 가
                # 아니라 '' 를 돌려주므로, 빈 값을 주입하면 float()/int() 변환이
                # import 시점에 터져 서버가 부팅조차 못 한다. 실제로 .env 의
                # 'APP_SESSION_TTL_SEC=' 한 줄 때문에 systemd 가 64회 재시작했다.
                if _k and _v and _k not in os.environ:
                    os.environ[_k] = _v
    except Exception as _e:
        print(f"Warning: .env 로드 중 오류: {_e}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from services import auth, backtest, bithumb, gemini_service, arbitrage
from services.gemini_service import gemini_keystore
from services.keystore import keystore
from services.arbitrage import arbitrage_manager
from services.strategy import StrategyParams, compute_indicators, entry_rule_catalog
from services.trader import MAX_ACTIVE_BOTS, TooManyBots, bot_manager
from services.envconf import env_int

app = FastAPI(title="빗썸 원화 자동매매 콘솔", version="4.0.0")
app.add_middleware(GZipMiddleware, minimum_size=500)

_origins = [o.strip() for o in os.getenv("APP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

# 서버 시작 시 봇 복원 결과. 화면이 '보류된 봇' 을 알려주는 데 쓴다.
RESTORE_SUMMARY: Dict[str, Any] = {"restored": 0, "resumed": 0, "held": 0, "notes": []}

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
    gs = gemini_keystore.status()
    logger.info(f"Gemini 키: {'등록됨(' + gs['source'] + ', ' + gs['model'] + ')' if gs['configured'] else '미등록'}")

    # 저장된 봇을 복원한다. LIVE 포지션은 빗썸 실제 보유량과 대조한 뒤에만 재가동한다.
    global RESTORE_SUMMARY
    RESTORE_SUMMARY = bot_manager.restore(keystore.account)

    # 차익거래 시뮬레이터도 복원한다. 실주문이 없으니 거래소 대조는 없다.
    try:
        arbitrage_manager.restore()
    except Exception as e:
        logger.error(f"차익거래 시뮬레이터 복원 실패: {e}")


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
                    httponly=True, samesite="lax",
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
    """화면 구성에 필요한 목록. 진입 규칙 카탈로그도 여기서 내려준다."""
    return {"coins": [{"code": c, "name": n} for c, n in bithumb.COINS.items()],
            "intervals": bithumb.INTERVALS,
            "entryRules": entry_rule_catalog(),
            "defaults": StrategyParams().to_dict()}


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

    # USDT 환차익은 '빗썸 USDT 가격 vs 원/달러 공시환율' 을 비교한다.
    # 다른 코인에 걸면 코인 가격을 환율과 비교하게 되어 의미 없는 봇이 된다.
    # 화면은 USDT 로 고정하지만 API 로 직접 호출하면 막히지 않았다.
    if (req.params or {}).get("strategyType") == "usdt_premium" and coin != "USDT":
        raise HTTPException(400,
            f"USDT 환차익 전략은 대상이 USDT 여야 합니다 (요청: {coin}). "
            f"이 전략은 빗썸 USDT 가격과 원/달러 공시환율의 차이를 이용합니다.")

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
            "activeCount": bot_manager.active_count(), "maxActive": MAX_ACTIVE_BOTS,
            "restoreSummary": RESTORE_SUMMARY}


@app.get("/api/bot/trades")
def bot_trades():
    """모든 봇의 실시간 매매 일지 및 누적 손익 정산 데이터."""
    return bot_manager.all_trade_history()


@app.post("/api/bot/stop")
def stop_bot(req: BotIdRequest):
    if not bot_manager.stop(req.botId):
        raise HTTPException(404, f"봇을 찾을 수 없습니다: {req.botId}")
    return {"success": True, "botId": req.botId}


@app.post("/api/bot/delete")
def delete_bot(req: BotIdRequest):
    if not bot_manager.delete(req.botId):
        raise HTTPException(404, f"봇을 찾을 수 없습니다: {req.botId}")
    global RESTORE_SUMMARY
    RESTORE_SUMMARY["notes"] = [n for n in RESTORE_SUMMARY.get("notes", []) if req.botId not in n]
    RESTORE_SUMMARY["held"] = len(RESTORE_SUMMARY["notes"])
    return {"success": True, "botId": req.botId}


@app.post("/api/bot/dismiss_restore_notice")
def dismiss_restore_notice():
    global RESTORE_SUMMARY
    RESTORE_SUMMARY = {"restored": len(bot_manager.bots), "resumed": bot_manager.active_count(), "held": 0, "notes": []}
    return {"success": True}


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


# ───────────────────────── Gemini AI ─────────────────────────

class GeminiKeyRequest(BaseModel):
    apiKey: str
    model: Optional[str] = None


class GeminiAnalyzeRequest(BaseModel):
    coin: str = "BTC"
    interval: str = "1h"
    forceRefresh: bool = True


@app.get("/api/gemini/status")
def gemini_status():
    """Gemini API 키 및 설정 상태."""
    return gemini_keystore.status()


@app.post("/api/gemini/save")
def gemini_save(req: GeminiKeyRequest):
    """Gemini API 키 및 모델 저장."""
    test = gemini_keystore.test_connection(req.apiKey, req.model)
    if not test.get("success"):
        raise HTTPException(400, test.get("message", "Gemini API 키 연결 실패"))
    try:
        gemini_keystore.save(req.apiKey.strip(), req.model)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return {"success": True, **gemini_keystore.status()}


@app.post("/api/gemini/clear")
def gemini_clear():
    """Gemini API 키 삭제."""
    try:
        gemini_keystore.clear()
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return {"success": True, **gemini_keystore.status()}


@app.post("/api/gemini/test")
def gemini_test(req: GeminiKeyRequest):
    """Gemini API 연결 테스트."""
    return gemini_keystore.test_connection(req.apiKey, req.model)


@app.post("/api/gemini/analyze")
def gemini_analyze(req: GeminiAnalyzeRequest):
    """특정 코인 실시간 Gemini AI 분석."""
    coin = bithumb.normalize_coin(req.coin)
    if not coin:
        raise HTTPException(400, f"지원하지 않는 코인: {req.coin}")
    res = gemini_service.analyze_coin(coin, req.interval, force_refresh=req.forceRefresh)
    return res


@app.get("/api/gemini/scan")
def gemini_scan(interval: str = "1h"):
    """5종 코인 전체 실시간 AI 스캔 및 추천 순위."""
    return gemini_service.scan_all_coins(interval=interval)


# ─────────────── 차익거래 지표 모니터 · 전략 시뮬레이터 ───────────────

class ArbitrageDeployRequest(BaseModel):
    strategy: str = "usdt_swap"  # "usdt_swap" | "spatial_dual"
    coin: str = "USDT"
    # mode 는 받기만 하고 무시한다. 이 기능에 실주문 경로가 없기 때문이다.
    # 과거 클라이언트가 "LIVE" 를 보내도 시뮬레이션으로만 동작한다.
    mode: str = "SIM"
    capitalKrw: float = 1_000_000.0
    config: Dict[str, Any] = {}


@app.get("/api/arbitrage/radar")
def arbitrage_radar():
    """김프·펀딩비·환율 실시간 지표. 조회 실패 항목은 null 로 오고 errors 에 사유가 담긴다."""
    return arbitrage.get_arbitrage_radar()


@app.get("/api/arbitrage/bots")
def arbitrage_list_bots():
    """가동 중인 차익거래 시뮬레이터 목록."""
    return {"bots": arbitrage_manager.list_bots()}


@app.post("/api/arbitrage/deploy")
def arbitrage_deploy(req: ArbitrageDeployRequest):
    """차익거래 시뮬레이터 생성 및 가동. 실주문은 나가지 않는다."""
    if req.capitalKrw < 10_000:
        raise HTTPException(400, "운용 자본은 10,000원 이상이어야 합니다.")
    
    valid_strats = ("usdt_swap", "spatial_dual")
    if req.strategy not in valid_strats:
        raise HTTPException(400, f"지원하지 않는 차익거래 전략: {req.strategy}")

    if req.mode.upper() == "LIVE":
        raise HTTPException(400,
            "차익거래는 시뮬레이션만 지원합니다. 해외 거래소 주문 연동이 없어 "
            "헤지 다리를 만들 수 없고, 국내 다리만 실주문으로 내면 무위험이 아니라 "
            "헤지 없는 단방향 매매가 됩니다.")

    target_coin = "USDT" if req.strategy == "usdt_swap" else req.coin
    bot = arbitrage_manager.create_bot(
        strategy=req.strategy,
        coin=target_coin,
        capital_krw=req.capitalKrw,
        config=req.config,
    )
    return {"success": True, "bot": bot.status(), "simulated": True}


@app.post("/api/arbitrage/stop")
def arbitrage_stop(req: BotIdRequest):
    """차익거래 시뮬레이터 정지."""
    ok = arbitrage_manager.stop_bot(req.botId)
    if not ok:
        raise HTTPException(404, f"해당 시뮬레이터를 찾을 수 없습니다: {req.botId}")
    return {"success": True}


@app.post("/api/arbitrage/delete")
def arbitrage_delete(req: BotIdRequest):
    """차익거래 시뮬레이터 삭제. 이제 재시작에도 남으므로 지울 수단이 필요하다."""
    ok = arbitrage_manager.delete_bot(req.botId)
    if not ok:
        raise HTTPException(404, f"해당 시뮬레이터를 찾을 수 없습니다: {req.botId}")
    return {"success": True}



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
    uvicorn.run("server:app", host="127.0.0.1", port=env_int("PORT", 8888), reload=True,
                # 프록시 헤더를 믿지 않는다. 자세한 이유는 deploy/service-start.sh 참고.
                proxy_headers=auth.trusted_proxy_count() > 0)
