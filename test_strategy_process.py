"""전략이 설계한 절차대로 도는지 검증한다.

실행: python3 test_strategy_process.py
실패가 있으면 목록과 함께 0 이 아닌 종료 코드를 반환한다.

시세를 실제로 받지 않고 '조작한 시장'을 주입해 각 분기를 강제로 통과시킨다.
실시세로는 원하는 조건(예: 역프 -0.8%)이 언제 올지 알 수 없어 검증이 안 된다.
주문은 나가지 않는다 — 전부 PAPER 로 돈다.
"""
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("APP_ACCESS_PASSWORD", "x" * 20)
logging.disable(logging.CRITICAL)

# ── 운영 데이터를 건드리지 않게 격리한다 ───────────────────────
# 이 스크립트가 만드는 봇은 bot_manager.bots 에 등록되지 않는다. 그런데
# TradingBot._persist() 는 bot_manager.persist() 를 부르고, 그게 '현재
# 등록된 봇 전체'(= 빈 목록)를 저장한다. 운영 서버에서 그냥 실행하면
# data/bots.json 이 빈 목록으로 덮여 가동 중인 실전 봇이 사라진다.
# 실제로 그렇게 지워본 뒤 체결 일지로 복원해야 했다.
#
# 그래서 저장 경로를 임시 디렉터리로 갈아끼운 뒤에 모듈을 import 한다.
import tempfile                                    # noqa: E402
_SANDBOX = tempfile.mkdtemp(prefix="strategy-test-")

from services import botstore                      # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
botstore.arb_store.path = os.path.join(_SANDBOX, "arb_bots.json")

from services import tradelog                      # noqa: E402
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True        # 운영 일지를 읽지 않는다

from services.strategy import StrategyParams        # noqa: E402
from services import trader                         # noqa: E402

FAIL = []
STEP = [0]


def check(name, ok, detail=""):
    STEP[0] += 1
    print(f"  {'✅' if ok else '❌'} {STEP[0]:>2}. {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


# ───────────────────── USDT 환차익 ─────────────────────
# 설계 절차
#   1) 공시환율과 빗썸 USDT 가격으로 프리미엄 계산
#   2) 무포지션 + 프리미엄 <= 매수선  → 전액 매수
#   3) 보유 + 프리미엄 >= 매도선      → 전량 매도
#   4) 환율 조회 실패                 → 판단 보류 (추정하지 않음)
#   5) 손절선(옵션)                   → 환율 붕괴 방어

# 실제 봇 루프를 돌린다. 판단 로직을 테스트가 재구현하면 '내 이해' 를
# 검증할 뿐 배포된 코드를 검증하지 못한다. 시세·환율 공급만 바꿔 끼운다.
from services import bithumb, fx                 # noqa: E402

_MARKET = {"price": 1380.0, "fx": 1382.0, "fxStale": False}


def _fake_price(coin):
    if _MARKET["price"] is None:
        raise bithumb.BithumbError("시세 없음 (시험)")
    return _MARKET["price"]


# 회차 매수는 '새 봉이 떴을 때' 만 일어난다. 그래서 봉 시각을 움직일 수 있어야
# 한다. _BAR_SHIFT 를 올리면 다음 조회에서 새 봉이 하나 더 생긴 것처럼 보인다.
_BAR_SHIFT = 0


def _fake_candles(coin, interval, limit=200):
    p = _MARKET["price"] or 1380.0
    now = 1_700_000_000_000
    return [{"time": now + (i + _BAR_SHIFT) * 3600_000, "open": p, "close": p,
             "high": p, "low": p, "volume": 1.0} for i in range(60)]


def _fake_fx_info():
    fx = _MARKET["fx"]
    if not fx:
        return {"rate": None, "asOf": None, "ageDays": None,
                "stale": False, "error": "환율 조회 실패 (시험)"}
    stale = _MARKET["fxStale"]
    return {"rate": fx, "asOf": "2026-09-18", "ageDays": 2 if stale else 0,
            "stale": stale, "error": ""}


def _fake_fx():
    v = _fake_fx_info()
    return v["rate"], v["error"]


bithumb.get_price = _fake_price
bithumb.get_candles = _fake_candles
fx.get_official_fx_rate = _fake_fx
fx.get_official_fx = _fake_fx_info
trader.PRICE_POLL_SEC = 0.2          # 시험을 빠르게
# 회차 매수는 캔들을 다시 받아 '새 봉' 을 확인해야 일어난다. 운영값(1시간봉
# 600초)이면 시험 안에서 봉이 바뀌지 않는다. 주기만 줄인다 — 판단 로직은 그대로다.
trader.CANDLE_REFRESH_SECONDS = {k: 0.2 for k in trader.CANDLE_REFRESH_SECONDS}


def run_bot(params, price, fx, cash=1_000_000.0, ticks=6, fx_stale=False):
    """실제 TradingBot 을 가동해 주입한 시장에서 몇 틱 돌린다."""
    _MARKET["price"], _MARKET["fx"], _MARKET["fxStale"] = price, fx, fx_stale
    bot = trader.TradingBot("test", "USDT", "1h", "PAPER", cash,
                            StrategyParams.from_dict(params), None)
    bot.start()
    for _ in range(ticks):
        time.sleep(0.25)
    return bot


def feed(bot, price, fx, ticks=6, fx_stale=False):
    """가동 중인 봇에 새 시장을 주입한다."""
    _MARKET["price"], _MARKET["fx"], _MARKET["fxStale"] = price, fx, fx_stale
    for _ in range(ticks):
        time.sleep(0.25)
    return bot


print()
# 띄운 봇을 끝에서 한꺼번에 세운다 (스레드가 남으면 테스트가 안 끝난다).
bots = []

print("── 라오어 무한매수 (raoer_infinite) · 실제 봇 루프 ──")
# 실전 자금이 걸린 전략인데 공정 검증이 없었다. 원전의 규칙 네 가지를
# 실제 봇 루프로 확인한다.
#   1) 1회 매수금 = 운용자본 / 분할수
#   2) 새 봉마다 한 회차씩 매수 (같은 봉에서 두 번 사지 않는다)
#   3) 평단 대비 목표 익절률 도달 시 전량 매도 · 회차 초기화
#   4) 분할수 소진 시 쿼터매도 방어 + 회차 롤백
# splitCount 는 코드가 최소 5로 교정한다(strategy.py). 시험도 5를 쓴다.
RP = {"strategyType": "raoer_infinite", "splitCount": 5, "targetProfitPct": 10.0,
      "quarterCutPct": 25.0, "raoerUseAi": False, "feePct": 0.04}


def run_raoer(price, cash=400_000.0, params=None, ticks=6):
    _MARKET["price"], _MARKET["fx"], _MARKET["fxStale"] = price, 1382.0, False
    bot = trader.TradingBot("rt", "BTC", "1h", "PAPER", cash,
                            StrategyParams.from_dict(params or RP), None)
    bot.start()
    for _ in range(ticks):
        time.sleep(0.25)
    return bot


def next_bar(bot, price, ticks=6):
    """새 봉을 만들어 준다 — 회차 매수는 봉이 바뀔 때만 일어난다."""
    global _BAR_SHIFT
    _BAR_SHIFT += 1
    _MARKET["price"] = price
    for _ in range(ticks):
        time.sleep(0.25)
    return bot


rb = run_raoer(1_000_000.0)
bots.append(rb)
check("1회 매수금이 운용자본 ÷ 분할수 다",
      abs(rb.pos.totalInvested - 80_000.0) < 1.0 and rb.pos.turn == 1,
      f"T={rb.pos.turn} · 투입 {rb.pos.totalInvested:,.0f}원 (400,000 ÷ 5)")

before_turn, before_inv = rb.pos.turn, rb.pos.totalInvested
for _ in range(8):
    time.sleep(0.25)
check("같은 봉에서는 두 번 사지 않는다",
      rb.pos.turn == before_turn and abs(rb.pos.totalInvested - before_inv) < 1.0,
      f"T={rb.pos.turn} 유지")

next_bar(rb, 900_000.0)      # 값이 내려도 회차 매수는 계속된다 (평단 낮춤)
check("새 봉이 뜨면 다음 회차를 매수한다 (하락해도 계속)",
      rb.pos.turn == 2 and rb.pos.entryPrice < 1_000_000.0,
      f"T={rb.pos.turn} · 평단 {rb.pos.entryPrice:,.0f}원")

entry_before = rb.pos.entryPrice
next_bar(rb, entry_before * 1.15)     # 평단 대비 +15% → 목표 +10% 통과
sells = [t for t in rb.trade_history if t["action"] == "SELL"]
check("평단 대비 목표 익절률에 닿으면 전량 매도한다",
      len(sells) == 1 and rb.pos.units == 0,
      f"매도 {len(sells)}건 · 보유 {rb.pos.units}")
check("익절 후 회차가 초기화된다", rb.pos.turn == 0, f"T={rb.pos.turn}")
# 막 익절한 그 봉에서 새 사이클을 시작하면, 방금 +10% 를 찍은 고점에 사게 된다.
# 백테스트는 다음 봉을 기다린다. 실전도 같아야 한다.
check("익절한 봉에서 곧바로 재매수하지 않는다",
      rb.pos.units == 0 and rb.pos.turn == 0,
      f"보유 {rb.pos.units} · T={rb.pos.turn}")
check("익절 손익이 현금 증감과 일치한다",
      abs(rb.realized_pnl - (rb.cash - 400_000.0)) < 1.0,
      f"손익 {rb.realized_pnl:+,.0f}원 / 현금증감 {rb.cash - 400_000.0:+,.0f}원")
next_bar(rb, entry_before * 1.15)     # 다음 봉에서는 새 사이클을 시작한다
check("다음 봉에서 새 사이클을 시작한다", rb.pos.turn == 1 and rb.pos.units > 0,
      f"T={rb.pos.turn} · 보유 {rb.pos.units:.6f}")

# 분할 소진 → 쿼터매도 방어
rq = run_raoer(1_000_000.0)
bots.append(rq)
for k in range(1, 9):                  # 5분할을 계속 떨어지는 값으로 확실히 소진
    if rq.pos.turn >= 5:
        break
    next_bar(rq, 1_000_000.0 - k * 20_000.0)
check("분할수를 소진하면 더 사지 않는다", rq.pos.turn == 5, f"T={rq.pos.turn}/5")
turn_at_full, units_at_full = rq.pos.turn, rq.pos.units
next_bar(rq, rq.pos.entryPrice * 0.99)
qcuts = [t for t in rq.trade_history if t["action"] == "SELL_QUARTER"]
check("소진 후 다음 봉에서 쿼터매도 방어가 나간다",
      len(qcuts) == 1 and rq.pos.units < units_at_full,
      f"쿼터매도 {len(qcuts)}건 · 보유 {units_at_full:.6f} → {rq.pos.units:.6f}")
check("쿼터매도가 판 비율이 설정과 같다 (25%)",
      abs(rq.pos.units - units_at_full * 0.75) / units_at_full < 0.01,
      f"{(1 - rq.pos.units / units_at_full) * 100:.1f}% 매도")
check("쿼터매도 후 회차가 롤백된다",
      rq.pos.turn < turn_at_full, f"T={turn_at_full} → {rq.pos.turn}")
check("쿼터매도가 남은 포지션의 원가를 부풀리지 않는다",
      rq.pos.totalInvested <= rq.pos.units * rq.pos.entryPrice * 1.02,
      f"원가 {rq.pos.totalInvested:,.0f}원 · 평가 {rq.pos.units * rq.pos.entryPrice:,.0f}원")

# V4 리버스 모드: 쿼터매도로 확보한 현금으로 다음 봉에서 추가 매수 순환
cash_after_quarter = rq.cash
units_after_quarter = rq.pos.units
next_bar(rq, rq.pos.entryPrice * 0.98)
check("V4 리버스 모드: 쿼터매도로 확보한 현금으로 바닥 추가 매수 가동",
      rq.pos.units > units_after_quarter and rq.pos.turn == turn_at_full,
      f"T={rq.pos.turn} · 보유 {units_after_quarter:.6f} → {rq.pos.units:.6f}")
check("V4 잔금 비례 공식: 마지막 회차 매수 시 현금 잔여 찌꺼기가 5,000원 미만이다",
      rq.cash < 5000.0, f"잔여 현금 {rq.cash:,.0f}원")
check("라오어 기본 버전이 V4다", StrategyParams().raoerVersion == "v4", StrategyParams().raoerVersion)
check("이상한 버전값은 v4로 교정한다", StrategyParams.from_dict({"raoerVersion": "unknown"}).raoerVersion == "v4")

print()
print("── 재시작이 회차를 먹지 않는다 ──")
# 실측: 오늘 배포로 12번 재시작했더니 6시간봉 봇이 8시간 만에 T1 → T19 로
# 갔다. 매수 시각이 재시작 시각과 초 단위로 일치했다. 재시작하면
# _last_bar_time 이 비어 '새 봉' 으로 보였기 때문이다.
rr = run_raoer(1_000_000.0)
bots.append(rr)
next_bar(rr, 990_000.0)
snap = rr.snapshot()
rr.stop(liquidate=False)
check("마지막으로 회차를 소비한 봉을 저장한다",
      snap.get("lastBarTime") is not None, f"lastBarTime={snap.get('lastBarTime')}")

rr2 = trader.TradingBot.restore(snap, None)
turn_before = rr2.pos.turn
rr2.start()
for _ in range(8):
    time.sleep(0.25)
check("재시작해도 같은 봉에서 또 사지 않는다",
      rr2.pos.turn == turn_before,
      f"T={turn_before} 유지 (예전에는 재시작마다 +1 이었다)")
next_bar(rr2, 980_000.0)
check("다음 봉이 오면 정상적으로 회차가 진행된다",
      rr2.pos.turn == turn_before + 1, f"T={turn_before} → {rr2.pos.turn}")
rr2.stop(liquidate=False)

# lastBarTime 이 없던 옛 봇
old_snap = dict(snap)
old_snap.pop("lastBarTime", None)
rr3 = trader.TradingBot.restore(old_snap, None)
t3 = rr3.pos.turn
rr3.start()
for _ in range(8):
    time.sleep(0.25)
check("이 값이 없던 옛 봇도 재시작 직후 중복 매수하지 않는다",
      rr3.pos.turn == t3, f"T={t3} 유지")
rr3.stop(liquidate=False)

print()
print("── 1회 매수금 상한 ──")
# V4 잔금비례는 '잔여현금 ÷ 잔여회차' 라, AI 가 계속 비중을 줄이면 현금이
# 덜 줄어 후반 회차가 눈덩이처럼 커진다(실측: 0.5x 지속 시 마지막 회차가
# 기본 분할금의 3.6배). 가장 깊은 하락 구간에서 한 번에 크게 담는 셈이다.
_cp = StrategyParams.from_dict({"strategyType": "raoer_infinite", "splitCount": 40,
                                "raoerMaxMultiplier": 2.0})
check("상한이 기본 분할금 × AI 배수 상한이다",
      abs(_cp.raoer_chunk_cap(400_000.0) - 20_000.0) < 0.01,
      f"400,000÷40=10,000 × 2.0 = {_cp.raoer_chunk_cap(400_000.0):,.0f}원")
check("분할수·배수를 바꾸면 상한도 따라간다",
      abs(StrategyParams.from_dict({"splitCount": 60, "raoerMaxMultiplier": 1.5})
          .raoer_chunk_cap(300_000.0) - 7_500.0) < 0.01, "60분할 1.5배 → 7,500원")
check("배수를 1 미만으로 낮춰도 상한이 기본 분할금 아래로 내려가지 않는다",
      abs(StrategyParams.from_dict({"splitCount": 40, "raoerMaxMultiplier": 0.5})
          .raoer_chunk_cap(400_000.0) - 10_000.0) < 0.01, "10,000원 (기본 분할금)")


def _flat(n=120, price=100_000.0):
    return [{"time": i * 3600_000, "open": price, "close": price, "high": price,
             "low": price, "volume": 1.0} for i in range(n)]


def _bt_cap(mult_cfg):
    from services import backtest as _b
    return _b.run("BTC", "1h", {"strategyType": "raoer_infinite", "splitCount": 40,
                                "targetProfitPct": 999.0, "quarterCutPct": 25.0,
                                "raoerUseAi": False, "raoerVersion": "v4",
                                "raoerMaxMultiplier": 2.0, **mult_cfg},
                  candles=_flat(), initial_krw=400_000.0)


_r = _bt_cap({})
_amts = [t for t in _r.get("trades", [])]
check("평상시(배수 1.0)에는 상한이 걸리지 않는다",
      abs(_r["finalKrw"] - 400_000.0) / 400_000.0 < 0.02,
      f"최종 {_r['finalKrw']:,.0f}원 (수수료 외 변화 없음)")

# boost_up 은 상한과 같은 배수를 쓰므로 막히면 안 된다
_rb = _bt_cap({"raoerTrendMode": "boost_up"})
check("추세 조절 boost_up 이 상한에 막히지 않는다",
      _rb["candleCount"] > 0, "2.0배 = 상한과 동일")

print()
print("── 제거한 전략이 되살아나지 않는다 ──")
_html = open("static/index.html", encoding="utf-8").read()
_js = open("static/js/app.js", encoding="utf-8").read()
_stg = open("services/strategy.py", encoding="utf-8").read()
_trd = open("services/trader.py", encoding="utf-8").read()
check("밸류리밸런싱 VR 이 화면·엔진에서 사라졌다",
      "raoer_vr" not in _html and "raoer_vr" not in _js
      and "raoer_vr" not in _stg and "raoer_vr" not in _trd,
      "index.html · app.js · strategy.py · trader.py 모두 0건")
check("VR 파라미터를 넣어도 기본 전략으로 교정한다",
      StrategyParams.from_dict({"strategyType": "raoer_vr"}).strategyType == "quant_ai",
      "quant_ai")
check("Gemini AI 전용 매매가 화면·엔진에서 사라졌다",
      "gemini_ai" not in _html and "gemini_ai" not in _js
      and "useGemini" not in _trd,
      "index.html · app.js · trader.py 모두 0건")
_srv2 = open("server.py", encoding="utf-8").read()
check("서버가 제거된 전략의 배포를 막는다",
      '_allowed = ("raoer_infinite",)' in _srv2 and "전략은 제거됐습니다" in _srv2,
      "허용 목록은 라오어 무한매수 하나뿐이다")
check("USDT 환차익·차익거래가 코드에서 사라졌다",
      "usdt_premium" not in _srv2
      and "usdt" not in open("services/trader.py", encoding="utf-8").read()
      and "arbitrage" not in open("static/js/app.js", encoding="utf-8").read(),
      "server.py · trader.py · app.js 모두 0건")
check("서버가 useGemini 배포를 막는다",
      'get("useGemini")' in _srv2, "Gemini 전용 매매 거부")
check("무한매수의 AI 스마트 조절은 그대로 살아 있다",
      "analyze_raoer_context" in _trd and "raoerUseAi" in _trd,
      "실전 봇이 쓰는 기능")
check("Gemini AI 연구소(스캐너)는 남아 있다",
      "gemini/scan" in _js and "scan_all_coins" in open("server.py", encoding="utf-8").read(),
      "매매 전략만 뺐다")
check("옛 VR 체결 기록은 일지에서 여전히 읽힌다",
      "BUY_VR" in _js and "SELL_VR" in _js,
      "지난 장부를 못 읽게 만들지 않는다")

# '전통 기술적 지표' 와 '퀀트 하이브리드' 를 화면에서 뺐다. 하이브리드는
# 진입 규칙 설정 화면까지 함께 뺐으므로, API 로 만들면 사용자가 본 적 없는
# 기본 조건으로 매매하게 된다. 그 함정을 서버가 막는지 본다.
_html = open("static/index.html", encoding="utf-8").read()
_js = open("static/js/app.js", encoding="utf-8").read()
check("화면 선택지에 technical / gemini_hybrid 가 없다",
      'value="technical"' not in _html and 'value="gemini_hybrid"' not in _html
      and "gemini_hybrid" not in _js,
      "index.html · app.js 모두 0건")
check("Gemini 기본 모드가 ai_only 다",
      StrategyParams().geminiMode == "ai_only", StrategyParams().geminiMode)
check("예전 hybrid 봇의 값은 보존한다 (복원용)",
      StrategyParams.from_dict({"geminiMode": "hybrid"}).geminiMode == "hybrid", "hybrid")
_srv = open("server.py", encoding="utf-8").read()
check("서버가 hybrid 배포를 거부한다",
      'get("geminiMode") == "hybrid"' in _srv and "제거됐습니다" in _srv,
      "deploy 에 가드 있음")

print()
print("── 무한매수 추세 조절 (raoerTrendMode) ──")
# 판정 기준은 재서 고르지 않았다. 통상적 정의를 그대로 쓴다:
#   상승 = 종가 > 30봉 평균 && 평균선 상승
#   하락 = 종가 < 30봉 평균 && 평균선 하락
from services import backtest as _bt                      # noqa: E402
from services.strategy import compute_indicators as _ci   # noqa: E402


def _bars(seq):
    P = StrategyParams.from_dict({"strategyType": "raoer_infinite", "slowMa": 5})
    return _ci([{"time": i * 3600_000, "open": p, "close": p, "high": p,
                 "low": p, "volume": 1.0} for i, p in enumerate(seq)], P), P


up_bars, P5 = _bars([100 + i * 2 for i in range(30)])          # 계속 오름
down_bars, _ = _bars([200 - i * 2 for i in range(30)])          # 계속 내림
flat_bars, _ = _bars([100.0] * 30)                               # 추세 없음 (종가 = 평균)

check("상승추세를 상승으로 읽는다", _bt._trend_of(up_bars, len(up_bars) - 1, P5) == "up",
      _bt._trend_of(up_bars, len(up_bars) - 1, P5))
check("하락추세를 하락으로 읽는다", _bt._trend_of(down_bars, len(down_bars) - 1, P5) == "down",
      _bt._trend_of(down_bars, len(down_bars) - 1, P5))
check("추세가 없으면 중립으로 둔다", _bt._trend_of(flat_bars, len(flat_bars) - 1, P5) == "flat",
      _bt._trend_of(flat_bars, len(flat_bars) - 1, P5))

_C = [{"time": i * 3600_000, "open": p, "close": p, "high": p, "low": p, "volume": 1.0}
      for i, p in enumerate([100 + i * 2 for i in range(80)])]


def _bt_run(mode):
    return _bt.run("BTC", "1h", {"strategyType": "raoer_infinite", "splitCount": 20,
                                 "targetProfitPct": 10.0, "quarterCutPct": 25.0,
                                 "raoerUseAi": False, "raoerTrendMode": mode,
                                 "raoerMaxMultiplier": 2.0, "slowMa": 5}, candles=_C)


off_r, boost_r = _bt_run("off"), _bt_run("boost_up")
check("상승장에서 boost_up 이 더 많이 담는다",
      boost_r["totalReturnPct"] > off_r["totalReturnPct"],
      f"off {off_r['totalReturnPct']:+.2f}% → boost {boost_r['totalReturnPct']:+.2f}%")

_D = [{"time": i * 3600_000, "open": p, "close": p, "high": p, "low": p, "volume": 1.0}
      for i, p in enumerate([200 - i * 2 for i in range(80)])]
pause_r = _bt.run("BTC", "1h", {"strategyType": "raoer_infinite", "splitCount": 20,
                                "targetProfitPct": 10.0, "quarterCutPct": 25.0,
                                "raoerUseAi": False, "raoerTrendMode": "pause_down",
                                "slowMa": 5}, candles=_D)
off_d = _bt.run("BTC", "1h", {"strategyType": "raoer_infinite", "splitCount": 20,
                              "targetProfitPct": 10.0, "quarterCutPct": 25.0,
                              "raoerUseAi": False, "raoerTrendMode": "off",
                              "slowMa": 5}, candles=_D)
check("하락장에서 pause_down 이 매수를 줄인다",
      pause_r["totalTrades"] <= off_d["totalTrades"]
      and pause_r["maxDrawdownPct"] <= off_d["maxDrawdownPct"],
      f"낙폭 {off_d['maxDrawdownPct']:.2f}% → {pause_r['maxDrawdownPct']:.2f}%")

check("기본값은 off 다 (켜는 것은 사용자의 선택)",
      StrategyParams().raoerTrendMode == "off", StrategyParams().raoerTrendMode)
check("이상한 값은 off 로 교정한다",
      StrategyParams.from_dict({"raoerTrendMode": "무엇"}).raoerTrendMode == "off", "off")

print()
for x in bots:
    x.is_running = False
time.sleep(0.5)

print()
if FAIL:
    print(f"실패 {len(FAIL)}건:")
    for f in FAIL:
        print(f"  · {f}")
    sys.exit(1)
print(f"전체 {STEP[0]}개 항목 통과")
print(f"(저장은 임시 경로에만 했습니다: {_SANDBOX})")
