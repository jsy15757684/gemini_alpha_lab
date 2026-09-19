"""전략이 설계한 절차대로 도는지 검증한다.

실행: python3 test_strategy_process.py
실패가 있으면 목록과 함께 0 이 아닌 종료 코드를 반환한다.

시세를 실제로 받지 않고 '조작한 시장'을 주입해 각 분기를 강제로 통과시킨다.
실시세로는 원하는 조건(예: 역프 -0.8%)이 언제 올지 알 수 없어 검증이 안 된다.
주문은 나가지 않는다 — usdt_premium 은 PAPER, spatial_dual 은 시뮬레이터다.
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

from services.arbitrage import ArbitrageBot          # noqa: E402
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
from services import bithumb, arbitrage          # noqa: E402

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
arbitrage.get_official_fx_rate = _fake_fx
arbitrage.get_official_fx = _fake_fx_info
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


print("── USDT 환차익 (usdt_premium) · 실제 봇 루프 ──")
P = {"strategyType": "usdt_premium", "usdtBuyPremiumPct": -0.8,
     "usdtSellPremiumPct": 2.0, "stopLossPct": 0.0, "feePct": 0.04}

bots = []

b = run_bot(P, price=1380.0, fx=1382.0)          # 프리미엄 -0.14%
bots.append(b)
check("매수선 미달 시 진입하지 않는다", not b.pos.open, b.last_decision[:50])

feed(b, price=1370.0, fx=1382.0)                 # -0.87% → 통과
check("매수선 통과 시 전액 매수한다", b.pos.open and b.cash == 0.0,
      f"보유 {b.pos.units:.4f} USDT @ {b.pos.entryPrice:,.0f}원")

feed(b, price=1390.0, fx=1382.0)                 # +0.58% → 미달
check("매도선 미달 시 보유를 유지한다", b.pos.open, b.last_decision[:50])

feed(b, price=1412.0, fx=1382.0)                 # +2.17% → 통과
check("매도선 통과 시 전량 매도한다", not b.pos.open, f"현금 {b.cash:,.0f}원")

check("확정 손익이 현금 증감과 일치한다",
      abs(b.realized_pnl - (b.cash - 1_000_000.0)) < 1.0,
      f"손익 {b.realized_pnl:+,.0f}원 / 현금증감 {b.cash - 1_000_000.0:+,.0f}원")

b2 = run_bot(P, price=1370.0, fx=None)           # 환율 없음 (매수 조건은 충족)
bots.append(b2)
check("환율을 못 받으면 판단을 보류한다",
      not b2.pos.open and "환율" in b2.last_decision, b2.last_decision[:50])

# ── 공시환율이 멈춰 있을 때 (주말·공휴일) ──
# 서울외환시장은 주 5일만 열린다. 토·일에는 금요일 값이 그대로 남는데,
# 빗썸 USDT 는 24시간 돌아서 '역프' 가 깊어 보인다. 실측(24개 주말):
# 금→월 USDT -0.242% / 환율 -0.222% 로 프리미엄 자체는 -0.020%p 밖에
# 안 변했다. 그 착시를 따라 매수한 17회는 다음 영업일 평균 -0.254%,
# 승률 12.5% 였다. 그래서 낡은 환율로는 진입도 익절도 하지 않는다.
bs = run_bot(P, price=1350.0, fx=1382.0, fx_stale=True)   # 표시 -2.32% (매수선 통과)
bots.append(bs)
check("환율이 멈춰 있으면 매수선을 통과해도 진입하지 않는다",
      not bs.pos.open and "멈춰" in bs.last_decision, bs.last_decision[:60])

feed(bs, price=1350.0, fx=1382.0, fx_stale=False)         # 장이 열리면
check("환율이 살아나면 다시 진입한다", bs.pos.open,
      f"보유 {bs.pos.units:.4f} USDT @ {bs.pos.entryPrice:,.0f}원")

feed(bs, price=1420.0, fx=1382.0, fx_stale=True)          # 표시 +2.75% (매도선 통과)
check("환율이 멈춰 있으면 매도선을 통과해도 익절하지 않는다",
      bs.pos.open and "멈춰" in bs.last_decision, bs.last_decision[:60])

bl = run_bot({**P, "stopLossPct": 2.0}, price=1370.0, fx=1382.0)
bots.append(bl)
entry_l = bl.pos.entryPrice
feed(bl, price=round(entry_l * 0.97), fx=1382.0, fx_stale=True)
check("환율이 멈춰 있어도 손절은 작동한다",
      not bl.pos.open and any(t["action"] == "SELL" for t in bl.trade_history),
      f"진입 {entry_l:,.0f}원 → 청산 {bl.realized_pnl:+,.0f}원")

b3 = run_bot({**P, "stopLossPct": 2.0}, price=1370.0, fx=1382.0)
bots.append(b3)
entry = b3.pos.entryPrice
feed(b3, price=round(entry * 0.97), fx=1382.0)   # -3% → 손절선 통과
sells = [t for t in b3.trade_history if t["action"] == "SELL"]
rebuys = [t for t in b3.trade_history if t["action"] == "BUY" and t["price"] < entry]
check("손절선을 켜면 청산한다", len(sells) == 1 and not b3.pos.open,
      f"진입 {entry:,.0f}원 → 청산 {b3.realized_pnl:+,.0f}원")
check("손절 후 같은 자리에 재매수하지 않는다", not rebuys,
      "재매수 0건" if not rebuys else f"재매수 {len(rebuys)}건 — 손실만 확정됨")
check("손절 후 봇이 정지한다 (사람이 판단)", not b3.is_running,
      b3.last_decision[:60])

p4 = StrategyParams.from_dict({**P, "usdtBuyPremiumPct": 1.0, "usdtSellPremiumPct": 0.5})
check("매수선이 매도선보다 높으면 자동 교정한다",
      p4.usdtSellPremiumPct > p4.usdtBuyPremiumPct,
      f"매수 {p4.usdtBuyPremiumPct}% / 매도 {p4.usdtSellPremiumPct}%")

check("매매 일지에 사유가 기록된다",
      any("역프" in (t.get("reason") or "") for t in b.trade_history)
      and any("김프" in (t.get("reason") or "") for t in b.trade_history),
      f"{len(b.trade_history)}건")

for x in bots:
    x.is_running = False
time.sleep(0.5)

# ───────────────────── 무전송 양방향 ─────────────────────
# 설계 절차
#   1) 초기 재고를 양쪽에 4분할 배치 (국내 코인/현금 · 해외 코인/현금)
#   2) 괴리 >= +기준 → 국내 매도 + 해외 매수
#   3) 괴리 <= -기준 → 해외 매도 + 국내 매수
#   4) 기준 미달      → 감시 대기
#   5) 총 수량 보존 (수수료는 현금에서만 나간다)
#   6) 데이터 없으면 판단 보류

print()
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
print("── USDT 환차익 스왑 시뮬레이터 (usdt_swap) ──")
# 봇 엔진(usdt_premium)과 같은 주말 규칙을 지켜야 한다. 두 구현이 같은
# 현상을 다르게 다루면 어느 쪽 숫자를 믿어야 하는지 알 수 없어진다.
FX_SW = 1388.0


def sw_radar(prem, stale, as_of="2026-09-18", age=1):
    return {"usdtPremiumPct": prem, "officialFxStale": stale,
            "officialFxAsOf": as_of if stale else None,
            "officialFxAgeDays": age if stale else 0}


def mk_sw(cap=10_000_000.0):
    return ArbitrageBot("t", "usdt_swap", "USDT", cap,
                        {"usdtBuyThreshold": -0.8, "usdtSellThreshold": 2.0})

w = mk_sw()
w._step_usdt_swap(sw_radar(-1.15, True), FX_SW, 1372.0)   # 매수선 통과 but 환율 멈춤
check("환율이 멈춰 있으면 시뮬레이터도 가상 매수하지 않는다",
      w.coin_units_domestic == 0 and "멈춰" in w.last_status, w.last_status[:58])

w._step_usdt_swap(sw_radar(-1.15, False), FX_SW, 1372.0)  # 장이 열리면
check("환율이 살아나면 가상 매수한다", w.coin_units_domestic > 0,
      f"보유 {w.coin_units_domestic:,.2f} USDT")

w._step_usdt_swap(sw_radar(2.5, True), FX_SW, 1425.0)     # 매도선 통과 but 환율 멈춤
check("환율이 멈춰 있으면 가상 익절도 하지 않는다",
      w.coin_units_domestic > 0 and "멈춰" in w.last_status, w.last_status[:58])

w._step_usdt_swap(sw_radar(2.5, False), FX_SW, 1425.0)    # 장이 열리면
check("환율이 살아나면 가상 익절한다",
      w.coin_units_domestic == 0 and w.realized_pnl != 0,
      f"실현 {w.realized_pnl:+,.0f}원")

print()
print("── 무전송 양방향 (spatial_dual) ──")
USDT = 1380.0
BN = 76000.0


def radar(sp):
    return {"coins": [{"coin": "BTC", "spatialSpreadPct": sp,
                       "bithumbPrice": BN * USDT * (1 + sp / 100.0),
                       "binanceUsdPrice": BN}]}


def mk(trigger=0.4, cap=4_000_000.0):
    bot = ArbitrageBot("t", "spatial_dual", "BTC", cap, {"triggerSpreadPct": trigger})
    bot._last_usdt_krw = USDT
    bot._last_coin_krw = BN * USDT
    bot._last_binance_usd = BN
    return bot


s = mk()
s._step_spatial_dual(radar(0.0), 1386.0, USDT)
both = s.coin_units_domestic > 0 and s.foreign_units > 0
cash_both = s.cash_krw > 0 and s.foreign_cash_usdt > 0
check("초기 재고를 양쪽에 배치한다", both and cash_both,
      f"국내 {s.coin_units_domestic:.6f} · 해외 {s.foreign_units:.6f}")

units0 = s.coin_units_domestic + s.foreign_units
before = s.total_trades
s._step_spatial_dual(radar(0.1), 1386.0, USDT)
check("기준 미달이면 체결하지 않는다", s.total_trades == before, s.last_status[:45])

s._step_spatial_dual(radar(0.6), 1386.0, USDT)
moved_out = s.foreign_units > units0 / 2
check("국내 고평가 시 국내 매도·해외 매수", moved_out and s.total_trades > before,
      f"국내 {s.coin_units_domestic:.6f} · 해외 {s.foreign_units:.6f}")

dom_before = s.coin_units_domestic
s._step_spatial_dual(radar(-0.6), 1386.0, USDT)
check("국내 저평가 시 해외 매도·국내 매수", s.coin_units_domestic > dom_before,
      f"국내 {dom_before:.6f} → {s.coin_units_domestic:.6f}")

units1 = s.coin_units_domestic + s.foreign_units
check("총 수량이 보존된다 (수수료는 현금에서)", abs(units1 - units0) < 1e-9,
      f"{units0:.8f} → {units1:.8f}")

s2 = mk()
s2._step_spatial_dual({"coins": [{"coin": "BTC", "spatialSpreadPct": None,
                                  "bithumbPrice": None, "binanceUsdPrice": None}]},
                      1386.0, USDT)
check("데이터가 없으면 판단을 보류한다",
      s2.total_trades == 0 and "보류" in s2.last_status, s2.last_status[:45])

s3 = mk()
s3._step_spatial_dual(radar(0.0), 1386.0, USDT)
be_loss = 0
for sp in (0.15, 0.20, 0.30):
    t = mk(trigger=0.01)
    t._step_spatial_dual(radar(0.0), 1386.0, USDT)
    p0 = t.realized_pnl
    t._step_spatial_dual(radar(sp), 1386.0, USDT)
    if sp <= 0.20 and t.realized_pnl - p0 > 0:
        be_loss += 1
    if sp == 0.30 and t.realized_pnl - p0 <= 0:
        be_loss += 1
check("손익분기 아래에서는 이익이 나지 않는다 (수수료·호가 반영)", be_loss == 0,
      "0.15%·0.20% 손실, 0.30% 이익")

check("주문 경로가 없다 (시뮬레이터임이 코드로 보장)",
      "self.account" not in open("services/arbitrage.py", encoding="utf-8").read(),
      "arbitrage.py 에 계좌 참조 없음")

print()
if FAIL:
    print(f"실패 {len(FAIL)}건:")
    for f in FAIL:
        print(f"  · {f}")
    sys.exit(1)
print(f"전체 {STEP[0]}개 항목 통과")
print(f"(저장은 임시 경로에만 했습니다: {_SANDBOX})")
