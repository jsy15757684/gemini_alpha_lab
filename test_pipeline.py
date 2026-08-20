import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.market_service import get_asset_quote, get_chart_data
from services.backtester import QuantBacktester
from services.gemini_ai import GeminiAIService

def test_all():
    print("1. Testing Quote...")
    quote = get_asset_quote("NVDA")
    print(f"Quote result: {quote['symbol']}, Price: {quote['currentPrice']}")

    print("\n2. Testing Chart...")
    chart = get_chart_data("NVDA", timeframe="3mo")
    print(f"Candles count: {len(chart['candles'])}, Tech signals: {len(chart['techSignals'])}")

    print("\n3. Testing Quant Backtester...")
    bt = QuantBacktester()
    res = bt.run_backtest("NVDA", fast_ma=5, slow_ma=20, take_profit_pct=10, stop_loss_pct=5)
    print(f"Backtest Total Return: {res['totalReturnPct']}%, Win Rate: {res['winRatePct']}%, Trades: {res['totalTrades']}")

    print("\n4. Testing Gemini AI Report...")
    ai = GeminiAIService()
    sentiment = ai.analyze_sentiment_and_news("NVDA", quote)
    report = ai.generate_premium_monetization_report("NVDA", quote, res, sentiment)
    print(f"Report Generated: {report['title']}, Length: {report['charCount']} chars")

    print("\n✅ All module tests passed successfully!")

if __name__ == "__main__":
    test_all()
