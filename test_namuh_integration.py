"""나무증권 Namuh PLUG 연동 테스트."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

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
        p = StrategyParams(strategyType="raoer_infinite", raoerVersion="v4", splitCount=40, targetProfitPct=10.0)
        bot = TradingBot(
            bot_id="SOXL-test-01",
            coin="SOXL",
            interval="1h",
            mode="PAPER",
            capital_krw=1000.0,
            params=p,
            broker="namuh"
        )
        # 1회차 매수 ($25)
        bot._enter_chunk(price=50.0, invest_krw=25.0, reason="1회차 분할매수")
        self.assertEqual(bot.pos.turn, 1)
        self.assertAlmostEqual(bot.pos.entryPrice, 50.0, places=2)
        self.assertGreater(bot.pos.units, 0.49)
        self.assertEqual(bot.trade_history[0]["currency"], "USD")
        self.assertEqual(bot.trade_history[0]["price"], 50.0)

        # 10% 상승 후 익절 매도
        bot._exit(price=55.0, reason="익절 도달")
        self.assertEqual(bot.pos.units, 0.0)
        self.assertFalse(bot.pos.open)
        self.assertGreater(bot.realized_pnl, 0.0)
        self.assertEqual(bot.trade_history[0]["action"], "SELL")
        self.assertEqual(bot.trade_history[0]["currency"], "USD")

    def test_namuh_keystore(self):
        ks = NamuhKeyStore()
        # Mock status check
        st = ks.status()
        self.assertIn("connected", st)
        self.assertIn("source", st)

if __name__ == "__main__":
    unittest.main()
