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


def _fake_candles(coin, interval, limit=200):
    p = _MARKET["price"] or 1380.0
    now = 1_700_000_000_000
    return [{"time": now + i * 3600_000, "open": p, "close": p,
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
