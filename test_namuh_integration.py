"""나무증권 Namuh PLUG 연동 테스트."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)


# 저장 경로를 임시 디렉터리로 갈아끼운 뒤에 봇 모듈을 import 한다.
# 이걸 빼먹으면 테스트가 개발 기계의 data/bots.json · trades.json 을
# 덮어쓴다 (한 번 그렇게 장부를 날려 체결 일지로 복원한 적이 있다).
import tempfile                                    # noqa: E402
_SANDBOX = tempfile.mkdtemp(prefix="namuh-integration-")

from services import botstore                      # noqa: E402
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore._DATA_DIR = _SANDBOX
botstore.arb_store.path = os.path.join(_SANDBOX, "arb_bots.json")

from services import tradelog                      # noqa: E402
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")
tradelog._rows.clear()
tradelog._loaded = True        # 운영 일지를 읽지 않는다

from services.namuh import NamuhAccount, NAMUH_STOCKS, get_price
from services.keystore import NamuhKeyStore
from services.strategy import StrategyParams, Position
from services.trader import TradingBot, BotManager

class TestNamuhIntegration(unittest.TestCase):
    def test_namuh_stocks_catalog(self):
        self.assertIn("TQQQ", NAMUH_STOCKS)
        self.assertIn("SOXL", NAMUH_STOCKS)
        self.assertIn("UPRO", NAMUH_STOCKS)
        self.assertEqual(NAMUH_STOCKS["TQQQ"]["currency"], "USD")
        # 3배 레버리지 ETF 만 취급한다 (개별주 제거)
        self.assertNotIn("AAPL", NAMUH_STOCKS)
        self.assertTrue(all(v["leverage"] == "3x" for v in NAMUH_STOCKS.values()))

    def test_trading_bot_namuh_init(self):
        p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4", splitCount=40)
        bot = TradingBot(
            bot_id="TQQQ-test-01",
            coin="TQQQ",
            interval="1h",
            mode="PAPER",
            capital_krw=1000.0,
            params=p,
            broker="namuh"
        )
        self.assertEqual(bot.broker, "namuh")
        self.assertEqual(bot.currency, "USD")
        self.assertEqual(bot.curr_symbol, "$")
        self.assertEqual(bot.market, "US_STOCK")
        
        st = bot.status()
        self.assertEqual(st["broker"], "namuh")
        self.assertEqual(st["currency"], "USD")
        self.assertEqual(st["currSymbol"], "$")
        self.assertEqual(st["initialKrw"], 1000.0)

    def test_namuh_paper_chunk_buy_and_exit(self):
        """미국 주식은 **정수 주수**로만 산다.

        예전 이 테스트는 $50 짜리 주식에 $25 를 배정하고 0.5주를 기대했다.
        나무증권 해외주식 주문은 정수 주수라, 소수점 수량은 장부에만 있고
        계좌에는 없는 값이 된다 — 거래소 대조가 매번 어긋난다. 그래서 지금은
        1주 값에 못 미치는 배정액은 사지 않고 다음 봉으로 이월한다.
        """
        p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4",
                           splitCount=40, targetProfitPct=10.0, feePct=0.0)
        bot = TradingBot(
            bot_id="SOXL-test-01",
            coin="SOXL",
            interval="1h",
            mode="PAPER",
            capital_krw=1000.0,
            params=p,
            broker="namuh"
        )
        # 1회차: $200 배정 · 주가 $50 → 정확히 4주
        bot._enter_chunk(price=50.0, invest_krw=200.0, reason="1회차 분할매수")
        self.assertEqual(bot.pos.turn, 1)
        self.assertAlmostEqual(bot.pos.entryPrice, 50.0, places=2)
        self.assertEqual(bot.pos.units, 4.0)
        self.assertEqual(bot.pos.units, int(bot.pos.units))   # 소수점 주식 금지
        self.assertAlmostEqual(bot.cash, 800.0, places=2)
        self.assertEqual(bot.budget_carryover, 0.0)
        self.assertEqual(bot.trade_history[0]["currency"], "USD")
        self.assertEqual(bot.trade_history[0]["price"], 50.0)

        # 10% 상승 후 익절 매도
        bot._exit(price=55.0, reason="익절 도달")
        self.assertEqual(bot.pos.units, 0.0)
        self.assertFalse(bot.pos.open)
        self.assertGreater(bot.realized_pnl, 0.0)
        self.assertEqual(bot.trade_history[0]["action"], "SELL")
        self.assertEqual(bot.trade_history[0]["currency"], "USD")
        self.assertEqual(bot.budget_carryover, 0.0)   # 청산하면 이월금도 초기화

    def test_namuh_sub_share_budget_carries_over(self):
        """1주 값에 못 미치는 배정액은 사지 않고 이월한다 (회차도 유지)."""
        p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4",
                           splitCount=40, targetProfitPct=10.0, feePct=0.0)
        bot = TradingBot(
            bot_id="SOXL-test-02", coin="SOXL", interval="1h", mode="PAPER",
            capital_krw=1000.0, params=p, broker="namuh")

        # $50 짜리 주식에 $25 배정 → 0주. 예전에는 0.5주를 사들였다.
        bot._enter_chunk(price=50.0, invest_krw=25.0, reason="1회차")
        self.assertEqual(bot.pos.units, 0.0)
        self.assertFalse(bot.pos.open)
        self.assertEqual(bot.pos.turn, 0)             # 못 샀으면 회차도 안 센다
        self.assertAlmostEqual(bot.budget_carryover, 25.0, places=2)
        self.assertAlmostEqual(bot.cash, 1000.0, places=2)   # 현금 미차감
        self.assertEqual(len(bot.trade_history), 0)   # 체결 없는 일지 기록 금지

        # 다시 $25 배정 → 이월 $25 와 합쳐 $50 → 1주 체결
        bot._enter_chunk(price=50.0, invest_krw=25.0, reason="2회차")
        self.assertEqual(bot.pos.units, 1.0)
        self.assertEqual(bot.pos.turn, 1)
        self.assertAlmostEqual(bot.budget_carryover, 0.0, places=2)
        self.assertAlmostEqual(bot.cash, 950.0, places=2)

    def test_namuh_keystore(self):
        ks = NamuhKeyStore()
        # Mock status check
        st = ks.status()
        self.assertIn("connected", st)
        self.assertIn("source", st)

if __name__ == "__main__":
    unittest.main()
