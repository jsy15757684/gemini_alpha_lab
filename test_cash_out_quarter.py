#!/usr/bin/env python3
"""코인 무한매수: 회차가 남았어도 현금이 최소 주문(5,000원) 미만이면 40회 소진으로 보고 쿼터매도하는가.

2026-10 빗썸 실계좌: SOL 39/40 · 현금 615원, XRP 38/40 · 4,623원 — '매수 보류' 만 되풀이하며
쿼터매도도 일어나지 않고 멈춰 있었다. 모의 봇으로 루프를 한 바퀴 돌려 본다.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SANDBOX = tempfile.mkdtemp(prefix="cashout-test-")
from services import botstore, tradelog              # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True

from services import trader, strategy                # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


CANDLES = [{"time": 1_790_000_000_000 + i * 21_600_000, "open": 150_000.0, "high": 151_000.0,
            "low": 149_000.0, "close": 149_000.0, "volume": 10.0} for i in range(200)]


def one_tick(cash, turn, units=5.05, avg=158_211.0):
    p = strategy.StrategyParams.from_dict({"strategyType": "raoer_infinite", "raoerVersion": "v4",
                                           "splitCount": 40, "targetProfitPct": 10.0, "quarterCutPct": 25.0,
                                           "raoerUseAi": False, "feePct": 0.0})
    b = trader.TradingBot(bot_id=f"c-{turn}-{cash}", coin="SOL", interval="6h", mode="PAPER",
                          capital_krw=800_000.0, params=p)
    b._persist = lambda: None
    b.pos.units, b.pos.entryPrice, b.pos.turn, b.pos.totalInvested, b.cash = units, avg, turn, units * avg, cash
    b._fetch_candles = lambda limit=200: CANDLES
    b._fetch_price = lambda: 149_000.0
    b.is_running = True

    def stop_after(_):
        b.is_running = False
    orig = trader.time.sleep
    trader.time.sleep = stop_after
    try:
        b._loop()
    finally:
        trader.time.sleep = orig
    return b


b = one_tick(615.0, 39)
q = [t for t in b.trade_history if t["action"] == "SELL_QUARTER"]
check("39/40 회차 · 현금 615원 → 쿼터매도 (25%)", len(q) == 1 and abs(q[0]["units"] - 5.05 * 0.25) < 1e-6,
      f"{[t['action'] for t in b.trade_history]} · {b.last_decision[:40]}")
check("회차를 10 되돌리고 판 돈으로 다시 살 수 있다", b.pos.turn == 29 and b.cash > 5000, f"T={b.pos.turn} 현금 {b.cash:,.0f}원")
check("사유에 '현금 소진' 이 남는다", "현금 소진" in (q[0]["reason"] if q else ""), "")

b = one_tick(4_623.0, 38)
check("38/40 회차 · 현금 4,623원도 같다", any(t["action"] == "SELL_QUARTER" for t in b.trade_history) and b.pos.turn == 28,
      f"T={b.pos.turn}")

b = one_tick(60_000.0, 38)
check("현금이 있으면 예전처럼 산다 (쿼터매도 아님)",
      [t["action"] for t in b.trade_history] == ["BUY_CHUNK"] and b.pos.turn == 39, f"{[t['action'] for t in b.trade_history]}")

print()
print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
