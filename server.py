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
#
# APP_SKIP_DOTENV=1 이면 읽지 않는다. 화면 확인용 로컬 인스턴스를 띄울 때
# 실계좌 키가 섞여 들어가는 것을 막기 위한 장치다. 호출부가 미리
# os.environ 에서 키를 지워도 여기서 .env 를 다시 주입하면 무의미해진다
# — 실제로 그렇게 실계좌 인증 호출이 한 번 나갔다(화이트리스트가 막았다).
_skip_dotenv = (os.getenv("APP_SKIP_DOTENV") or "").strip().lower() in ("1", "true", "yes", "on")
_env_file = os.path.join(CURRENT_DIR, ".env")
if _skip_dotenv:
    print("APP_SKIP_DOTENV=1 — .env 를 읽지 않습니다 (격리 실행)")
elif os.path.exists(_env_file):
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

from services import auth, backtest, bithumb, gemini_service, arbitrage, spread_recorder, namuh
from services.gemini_service import gemini_keystore
from services.keystore import keystore, namuh_keystore
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
    ns = namuh_keystore.status()
    ns = namuh_keystore.status()
    if ns.get("connected"):
        from services import namuh as _nm
        _mock = _nm.use_mock()
        logger.info(
            f"나무증권 키: 등록됨({ns['source']}) · 계좌 {ns.get('maskedAccount') or '미설정'} · "
            f"{'🧪 모의투자' if _mock else '💰 실계좌'} ({_nm.trade_base_url()})")
        if not _mock:
            logger.warning("나무증권이 실계좌로 설정돼 있습니다 — 주문이 실제로 나갑니다. "
                           "모의로 돌리려면 NAMUH_MOCK=1 을 넣으세요.")
    else:
        logger.info("나무증권 키: 미등록")
    gs = gemini_keystore.status()
    logger.info(f"Gemini 키: {'등록됨(' + gs['source'] + ', ' + gs['model'] + ')' if gs['configured'] else '미등록'}")

    # 저장된 봇을 복원한다. LIVE 포지션은 거래소 실제 보유량과 대조한 뒤에만 재가동한다.
    global RESTORE_SUMMARY
    RESTORE_SUMMARY = bot_manager.restore(keystore.account, namuh_keystore.account)

    # 차익거래 시뮬레이터도 복원한다. 실주문이 없으니 거래소 대조는 없다.
    try:
        arbitrage_manager.restore()
    except Exception as e:
        logger.error(f"차익거래 시뮬레이터 복원 실패: {e}")

    # 거래소 간 괴리를 계속 기록한다. 주문은 내지 않고 공개 호가만 읽는다.
    # 무전송 양방향을 구현할지 판단할 근거를 자금 0원으로 모으기 위한 것이다.
    # APP_SPREAD_RECORDER=0 으로 끌 수 있다.
    if (os.getenv("APP_SPREAD_RECORDER") or "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            spread_recorder.start()
        except Exception as e:
            logger.error(f"괴리 기록기 시작 실패 (서버는 계속 뜹니다): {e}")


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
    broker: str = "bithumb"
    params: Dict[str, Any] = {}


class BotIdRequest(BaseModel):
    botId: str


@app.post("/api/bot/deploy")
def deploy_bot(req: DeployRequest):
    broker = (req.broker or "bithumb").lower()
    raw_coin = req.coin.upper().strip()

    if broker == "namuh" or raw_coin in namuh.NAMUH_STOCKS:
        broker = "namuh"
        coin = raw_coin
        if coin not in namuh.NAMUH_STOCKS:
            raise HTTPException(400, f"나무증권 지원 종목이 아닙니다: {coin} (지원: {', '.join(namuh.NAMUH_STOCKS.keys())})")
        if req.capitalKrw < 10:
            raise HTTPException(400, "미국주식 운용 자본은 $10 이상이어야 합니다.")
        mode = req.mode.upper()
        if mode not in ("PAPER", "LIVE"):
            raise HTTPException(400, "mode 는 PAPER 또는 LIVE 여야 합니다.")

        try:
            namuh.get_price(coin)
        except Exception as e:
            raise HTTPException(503, f"{coin} 시세를 받지 못해 봇을 가동할 수 없습니다: {e}")

        if mode == "LIVE":
            if not namuh_keystore.account.configured:
                raise HTTPException(400, "실전(LIVE) 가동 전에 나무증권 API 키를 등록해야 합니다.")
            test = namuh_keystore.account.test_connection()
            if not test.get("success"):
                raise HTTPException(400, f"나무증권 실계좌 연결 실패: {test.get('message')}")
            usd_avail = float(test.get("usdAvailable", 0))
            if usd_avail < req.capitalKrw:
                raise HTTPException(400, f"나무증권 주문가능 외화(${usd_avail:,.2f})가 운용 자본(${req.capitalKrw:,.2f})보다 적습니다.")

        try:
            bot = bot_manager.deploy(coin, req.interval, mode, req.capitalKrw,
                                     req.params, account=keystore.account,
                                     namuh_account=namuh_keystore.account, broker="namuh")
        except TooManyBots as e:
            raise HTTPException(429, str(e))
        return bot.status()

    coin = bithumb.normalize_coin(req.coin)
    if not coin:
        raise HTTPException(400, f"빗썸 원화마켓에 없는 코인입니다: {req.coin}")
    if req.interval not in bithumb.INTERVALS:
        raise HTTPException(400, f"지원하지 않는 캔들 간격입니다: {req.interval}")
    if req.capitalKrw < 10_000:
        raise HTTPException(400, "운용 자본은 10,000원 이상이어야 합니다.")

    # 화면에서 고를 수 있는 전략만 배포를 허용한다.
    #
    # 제거한 전략(기술적 지표 · 퀀트 하이브리드 · 밸류리밸런싱 VR ·
    # Gemini AI 전용)은 설정 화면도 함께 없앴다. API 로 들어오면 사용자가
    # 본 적 없는 기본값으로 실매매가 나간다. 그 함정을 만들지 않는다.
    _allowed = ("raoer_infinite", "usdt_premium")
    _st = (req.params or {}).get("strategyType")
    if _st and _st not in _allowed:
        raise HTTPException(400,
            f"'{_st}' 전략은 제거됐습니다. 사용 가능: {', '.join(_allowed)} "
            f"(화면에서는 무한매수 V4/V1 · USDT 환차익 으로 보입니다).")
    if (req.params or {}).get("useGemini"):
        raise HTTPException(400,
            "Gemini AI 전용 매매는 제거됐습니다. 무한매수의 'AI 스마트 조절' "
            "옵션으로 AI 판단을 쓸 수 있습니다.")

    # 하이브리드(지표 신호 + AI 승인)는 화면에서 없앴다. 진입 규칙·지표
    # 설정을 보여주는 칸도 함께 뺐으므로, API 로 이 모드를 만들면 사용자가
    # 본 적 없는 기본 진입조건으로 매매하게 된다. 그 함정을 만들지 않는다.
    # 지표 엔진 자체는 백테스트에 남아 있다.
    if (req.params or {}).get("geminiMode") == "hybrid":
        raise HTTPException(400,
            "퀀트 하이브리드 모드는 제거됐습니다. Gemini 는 'ai_only' 로만 가동합니다 "
            "(진입 규칙 설정 화면이 없어 사용자가 보지 못한 조건으로 매매하게 됩니다).")

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
                                 req.params, keystore.account,
                                 namuh_account=namuh_keystore.account, broker="bithumb")
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
    """보류 알림을 닫는다.

    상태 파일을 아예 읽지 못한 경우(fatal)는 닫아 주지 않는다. 그 상황은
    빗썸에 포지션이 남았는데 감시하는 봇이 없는 상태라, 화면에서 사라지면
    안 된다. 재시작해서 원인이 풀려야 없어진다.
    """
    global RESTORE_SUMMARY
    if RESTORE_SUMMARY.get("fatal"):
        return {"success": False,
                "message": "봇 상태를 복원하지 못한 알림은 닫을 수 없습니다. "
                           "원인을 고친 뒤 서비스를 재시작하세요."}
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


# ───────────────────────── 나무증권 (해외주식) ─────────────────────────

class NamuhKeyRequest(BaseModel):
    appKey: str
    appSecret: str
    accountNo: str = ""


@app.get("/api/namuh/stocks")
def namuh_stocks():
    """나무증권 지원 미국 ETF 종목 목록."""
    return {"stocks": [{"code": c, **info} for c, info in namuh.NAMUH_STOCKS.items()]}


@app.get("/api/namuh/account")
def namuh_account_status():
    st = namuh_keystore.status()
    st["mock"] = namuh.use_mock()
    if namuh_keystore.account.configured:
        try:
            bal = namuh_keystore.account.get_balance()
            # get_balance 는 종목코드를 키로 한 dict 를 준다. 화면은 배열을
            # 기대하므로 여기서 한 번만 바꿔준다 — 이걸 빼먹어서 보유 ETF 가
            # 항상 '없음' 으로 나왔었다.
            holdings = [
                {"symbol": t, "name": v.get("name") or t,
                 "quantity": v.get("qty", 0.0),
                 "sellableQty": v.get("sellableQty", 0.0),
                 "avgPrice": v.get("avgPrice", 0.0),
                 "lastPrice": v.get("lastPrice", 0.0),
                 "evalAmountUsd": v.get("evalAmountUsd", 0.0),
                 "pnlUsd": v.get("pnlUsd", 0.0)}
                for t, v in (bal.get("holdings") or {}).items()
            ]
            st.update({
                "balanceOk": True,
                "usdAvailable": bal.get("usdAvailable", 0.0),
                "usdTotal": bal.get("usdTotal", 0.0),
                "krwDeposit": bal.get("krwDeposit", 0.0),
                "totalAssetKrw": bal.get("totalAssetKrw", 0.0),
                "holdings": holdings,
            })
        except namuh.NamuhError as e:
            st.update({"balanceOk": False, "error": e.message})
    return st


@app.post("/api/namuh/test")
def namuh_test(req: NamuhKeyRequest):
    return namuh.NamuhAccount(req.appKey, req.appSecret, req.accountNo).test_connection()


@app.post("/api/namuh/save")
def namuh_save(req: NamuhKeyRequest):
    result = namuh.NamuhAccount(req.appKey, req.appSecret, req.accountNo).test_connection()
    if not result.get("success"):
        raise HTTPException(400, result.get("message", "나무증권 인증에 실패했습니다."))
    try:
        namuh_keystore.save(req.appKey.strip(), req.appSecret.strip(), req.accountNo.strip())
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return {"success": True, **namuh_keystore.status()}


@app.post("/api/namuh/clear")
def namuh_clear():
    try:
        namuh_keystore.clear()
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return {"success": True, **namuh_keystore.status()}


@app.get("/api/namuh/prices")
def namuh_prices():
    """미국 ETF 현재가 조회."""
    out = []
    for c, info in namuh.NAMUH_STOCKS.items():
        try:
            ticker = namuh_keystore.account.get_ticker(c)
            out.append(ticker)
        except Exception as e:
            out.append({"symbol": c, "name": info["name"], "error": str(e)})
    return {"prices": out}


@app.get("/api/namuh/candles")
def namuh_candles(symbol: str = Query(...), interval: str = Query("1h")):
    """미국 ETF 캔들 및 지표."""
    sym = symbol.upper().strip()
    if sym not in namuh.NAMUH_STOCKS:
        raise HTTPException(400, f"지원하지 않는 종목: {sym}")
    try:
        rows = namuh_keystore.account.get_candles(sym, interval, limit=200)
        p = StrategyParams()
        bars = compute_indicators(rows, p)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"symbol": sym, "name": namuh.NAMUH_STOCKS[sym]["name"], "interval": interval,
            "candles": bars, "params": p.to_dict(), "dataSource": "namuh-plug"}


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
    """테더 프리미엄·김프·무전송 괴리 실시간 지표. 조회 실패 항목은 null 로 오고 errors 에 사유가 담긴다."""
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
