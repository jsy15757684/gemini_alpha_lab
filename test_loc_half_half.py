"""라오어 원조 반반 LOC (Half/Half Limit-On-Close) 매수 체계 단위 테스트."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import trader, strategy


def test_first_cycle_initial_market_buy():
    print("\n[TEST 1] 1회차 첫 매수 (시장가 진입으로 평단가 형성)")
    params = strategy.StrategyParams(
        strategyType="raoer_infinite",
        raoerVersion="v4",
        splitCount=40,
        targetProfitPct=10.0,
        locMode="half_half",
        feePct=0.0,
    )
    bot = trader.TradingBot(
        bot_id="test-loc-first-cycle",
        coin="TQQQ",
        interval="1h",
        mode="PAPER",
        capital_krw=4000.0,  # $4,000
        params=params,
        broker="namuh",
    )
    assert bot.currency == "USD"
    assert bot.pos.turn == 0
    assert not bot.pos.open

    # 1회차 매수 (배정액 $100, 주가 $50)
    bot._enter_chunk(price=50.0, invest_krw=100.0, reason="1회차 첫 매수")
    assert bot.pos.open
    assert bot.pos.turn == 1
    assert bot.pos.units == 2.0  # $100 / $50 = 2주
    assert bot.pos.entryPrice == 50.0
    assert bot.cash == 3900.0
    assert bot.budget_carryover == 0.0
    print(f"  · 1회차 체결: {bot.pos.units}주 @ ${bot.pos.entryPrice}, 잔여현금 ${bot.cash}")
    print("  ✅ 1회차 첫 매수 검증 통과")


def test_first_half_both_loc_filled():
    print("\n[TEST 2] 전반전 (T <= N/2): 종가 <= 평단가 -> A(평단) & B(평단+5%) 둘 다 체결")
    params = strategy.StrategyParams(
        strategyType="raoer_infinite",
        raoerVersion="v4",
        splitCount=40,
        targetProfitPct=10.0,
        locMode="half_half",
        feePct=0.0,
    )
    bot = trader.TradingBot(
        bot_id="test-loc-both-fill",
        coin="TQQQ",
        interval="1h",
        mode="PAPER",
        capital_krw=4000.0,
        params=params,
        broker="namuh",
    )
    # 1회차: $50에 2주 매수 (평단 $50)
    bot._enter_chunk(price=50.0, invest_krw=100.0, reason="1회차")
    assert bot.pos.entryPrice == 50.0

    # 2회차 (전반전 T=1 <= 20): 주가가 $48로 하락 (평단 $50 이하)
    # A LOC: $50 ($50 예산 -> 1주)
    # B LOC: $52.5 ($50 예산 -> 1주)
    # 종가 $48 <= $50 이므로 둘 다 체결! (총 2주 @ $48 = $96 매수, 잔돈 $4 이월)
    bot._enter_chunk(price=48.0, invest_krw=100.0, reason="2회차 종가 $48")
    assert bot.pos.turn == 2
    assert bot.pos.units == 4.0  # 2 + 2 = 4주
    # 평단: (2 * 50 + 2 * 48) / 4 = 49.0
    assert bot.pos.entryPrice == 49.0
    assert bot.budget_carryover == 4.0
    # 자산 정합성: 현금 + 주식 가치 == 총 자산
    equity = bot.cash + bot.pos.units * 48.0
    print(f"  · 2회차 체결: 총 {bot.pos.units}주, 평단 ${bot.pos.entryPrice}, 잔돈이월 ${bot.budget_carryover}, 잔여현금 ${bot.cash}")
    assert round(bot.cash + bot.pos.totalInvested, 2) == 4000.0
    print("  ✅ 전반전 양쪽 체결 검증 통과")


def test_first_half_only_loc_b_filled():
    print("\n[TEST 3] 전반전 (T <= N/2): 평단 < 종가 <= 평단+5% -> B(평단+5%)만 체결, A는 이월")
    params = strategy.StrategyParams(
        strategyType="raoer_infinite",
        raoerVersion="v4",
        splitCount=40,
        targetProfitPct=10.0,
        locMode="half_half",
        feePct=0.0,
    )
    bot = trader.TradingBot(
        bot_id="test-loc-b-only",
        coin="TQQQ",
        interval="1h",
        mode="PAPER",
        capital_krw=4000.0,
        params=params,
        broker="namuh",
    )
    bot._enter_chunk(price=50.0, invest_krw=100.0, reason="1회차")
    assert bot.pos.entryPrice == 50.0

    # 2회차: 주가가 $51로 소폭 상승 ($50 < $51 <= $52.5)
    # A LOC ($50): 미체결 (종가 $51 > $50)
    # B LOC ($52.5): 체결! ($50 예산 -> 0주 or 소액방어로 1주 매수 가능 여부)
    # 배정 $100 -> half_budget $50 -> $50 // $51 = 0. 단, total_budget=$100 >= $51 이므로 소액방어로 1주 체결!
    bot._enter_chunk(price=51.0, invest_krw=100.0, reason="2회차 종가 $51")
    assert bot.pos.turn == 2
    assert bot.pos.units == 3.0  # 2 + 1 = 3주
    # 1주만 $51에 매수됨 ($51 지출). 가용 $100 중 $51 지출 -> $49 잔돈 이월!
    assert bot.budget_carryover == 49.0
    assert bot.cash == 3900.0 - 51.0
    print(f"  · 2회차 B만 체결: 총 {bot.pos.units}주, 잔돈이월 ${bot.budget_carryover}, 잔여현금 ${bot.cash}")
    print("  ✅ 전반전 B만 체결 검증 통과")


def test_first_half_surge_no_fill():
    print("\n[TEST 4] 전반전 (T <= N/2): 종가 > 평단+5% 급등 -> A & B 둘 다 미체결 (추격매수 방지)")
    params = strategy.StrategyParams(
        strategyType="raoer_infinite",
        raoerVersion="v4",
        splitCount=40,
        targetProfitPct=10.0,
        locMode="half_half",
        feePct=0.0,
    )
    bot = trader.TradingBot(
        bot_id="test-loc-surge-no-fill",
        coin="TQQQ",
        interval="1h",
        mode="PAPER",
        capital_krw=4000.0,
        params=params,
        broker="namuh",
    )
    bot._enter_chunk(price=50.0, invest_krw=100.0, reason="1회차")
    initial_cash_after_1 = bot.cash

    # 2회차: 주가가 $55로 급등 ($55 > $50*1.05 = $52.5)
    bot._enter_chunk(price=55.0, invest_krw=100.0, reason="2회차 종가 $55 (급등)")
    # 미체결은 '안 산 것' 이다. 회차(T)는 몇 번 샀는지를 세므로 올리지 않는다.
    # 예전에는 여기서 turn 을 올려, 평단 +5~12% 구간에 머물면 한 주도 안 사고
    # 40회를 다 태운 뒤 수익 중인 포지션을 쿼터매도했다.
    assert bot.pos.turn == 1, bot.pos.turn
    assert bot.pos.units == 2.0  # 매수 안 함, 2주 유지
    # 안 쓴 배정액은 현금에서 빠진 적이 없다. 이월금으로 쌓으면 눈덩이처럼
    # 불어나 나중에 한 번에 지르게 된다.
    assert bot.budget_carryover == 0.0, bot.budget_carryover
    assert bot.cash == initial_cash_after_1  # 현금 미차감
    print(f"  · 2회차 급등 미체결: 보유 {bot.pos.units}주 유지, 회차 유지 T={bot.pos.turn}, 이월 ${bot.budget_carryover}")

    # 가격이 내려오면 그 회차가 정상적으로 체결된다
    bot._enter_chunk(price=48.0, invest_krw=100.0, reason="3회차 종가 $48")
    assert bot.pos.turn == 2, bot.pos.turn
    assert bot.pos.units > 2.0
    print(f"  · 가격 회복 후 정상 체결: 보유 {bot.pos.units}주, T={bot.pos.turn}")
    print("  ✅ 전반전 급등 추격매수 방지 검증 통과")


def test_second_half_entry_price_loc():
    print("\n[TEST 5] 후반전 (T > N/2): 100% 예산 평단 LOC 집중 (평단 이하만 매수, 초과 시 전액 이월)")
    params = strategy.StrategyParams(
        strategyType="raoer_infinite",
        raoerVersion="v4",
        splitCount=40,
        targetProfitPct=10.0,
        locMode="half_half",
        feePct=0.0,
    )
    bot = trader.TradingBot(
        bot_id="test-loc-second-half",
        coin="TQQQ",
        interval="1h",
        mode="PAPER",
        capital_krw=4000.0,
        params=params,
        broker="namuh",
    )
    bot._enter_chunk(price=50.0, invest_krw=100.0, reason="1회차")
    bot.pos.turn = 25  # 후반전으로 설정 (25 > 20)

    # 1) 종가가 평단 초과 ($52 > $50) -> 후반전에는 평단 낮추기 위해 매수하지 않고 전액 이월!
    bot._enter_chunk(price=52.0, invest_krw=100.0, reason="후반전 $52")
    assert bot.pos.turn == 25, bot.pos.turn   # 미체결 → 회차 유지
    assert bot.pos.units == 2.0
    assert bot.budget_carryover == 0.0, bot.budget_carryover
    print(f"  · 후반전 평단 초과 시 미체결 (회차 유지 T={bot.pos.turn}, 이월 ${bot.budget_carryover})")

    # 2) 종가가 평단 이하 ($48 <= $50) -> 배정액 $100 투입
    # $100 // $48 = 2주 매수 ($96 지출, $4 이월)
    bot._enter_chunk(price=48.0, invest_krw=100.0, reason="후반전 $48")
    assert bot.pos.turn == 26, bot.pos.turn
    assert bot.pos.units == 4.0, bot.pos.units   # 2 + 2 = 4주
    assert bot.budget_carryover == 4.0, bot.budget_carryover
    print(f"  · 후반전 평단 이하 집중 매수: 총 {bot.pos.units}주, 잔돈이월 ${bot.budget_carryover}")
    print("  ✅ 후반전 평단 LOC 집중 검증 통과")


if __name__ == "__main__":
    test_first_cycle_initial_market_buy()
    test_first_half_both_loc_filled()
    test_first_half_only_loc_b_filled()
    test_first_half_surge_no_fill()
    test_second_half_entry_price_loc()
    print("\n🎉 모든 라오어 원조 반반 LOC 테스트 완벽 통과!")
