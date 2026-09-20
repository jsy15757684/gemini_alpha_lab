"""미국 주식 특화 3대 핵심 기능 단위 및 통합 테스트:
1. 미국 증시 스케줄러 & 서머타임 & 휴장일 판정
2. 정수 1주 단위 체결 & 잔돈 이월(Budget Carry-Over)
3. 연간 250만 원 해외주식 양도소득세 비과세 트래커
"""

from datetime import datetime, date
import os
import sys

# 프로젝트 루트 경로 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import market_schedule, trader, strategy, namuh
import server


def test_market_schedule():
    print("\n[TEST 1] 미국 증시 운영 시간 및 휴장일 스케줄러 검증")
    st = market_schedule.get_us_market_status()
    print(f"  · 현재 증시 상태: {st['statusText']}")
    print(f"  · 미국 동부 시각: {st['easternTime']} ({'EDT' if st['isDst'] else 'EST'})")
    print(f"  · 한국 시각: {st['koreanTime']}")
    print(f"  · 개장 여부: {st['isOpen']}")
    print(f"  · 다음 개장 예정(KST): {st.get('nextOpenKst', '-')}")
    print(f"  · 다음 마감 예정(KST): {st.get('nextCloseKst', '-')}")

    assert "isOpen" in st
    assert "status" in st
    assert "nextOpenKst" in st

    # 공휴일 판정 테스트 (2026년 추수감사절 및 성금요일)
    holidays_2026 = market_schedule.get_nyse_holidays(2026)
    print(f"  · 2026년 휴장일 수: {len(holidays_2026)}일")
    assert any("성금요일" in name or "Good Friday" in name for name in holidays_2026.values())
    assert any("추수감사절" in name or "Thanksgiving" in name for name in holidays_2026.values())
    print("  ✅ 스케줄러 검증 통과")


def test_budget_carryover_and_integer_shares():
    print("\n[TEST 2] 정수 1주 단위 체결 및 잔돈 이월 (Budget Carry-Over) 검증")
    params = strategy.StrategyParams(
        strategyType="raoer_infinite",
        raoerVersion="v4",
        splitCount=40,
        targetProfitPct=10.0,
        feePct=0.0,
    )
    # USD 봇 생성 ($1,000, TQQQ)
    bot = trader.TradingBot(
        bot_id="test-usd-carryover",
        coin="TQQQ",
        interval="1h",
        mode="PAPER",
        capital_krw=1000.0,
        params=params,
        broker="namuh",
    )
    assert bot.currency == "USD"
    assert bot.budget_carryover == 0.0
    assert bot.cash == 1000.0

    # 시나리오 1: 주가가 $40인데, 1회 배정액이 $25인 경우 -> 1주 미만이므로 이월되어야 함
    # 40분할 첫 회차 배정액: $25
    chunk_1 = 25.0
    price = 40.0
    bot._enter_chunk(price, chunk_1, "1회차 테스트 매수")

    print(f"  · [회차 1 후] 보유수량: {bot.pos.units}주, 잔돈이월: ${bot.budget_carryover:.2f}, 현금: ${bot.cash:.2f}")
    assert bot.pos.units == 0.0, "1주 미만은 매수되지 않아야 함"
    assert bot.budget_carryover == 25.0, "미집행 배정액 $25가 그대로 이월되어야 함"
    assert bot.cash == 1000.0, "현금 잔고는 아직 차감되지 않아야 함"

    # 시나리오 2: 2회차 매수 (배정액 $25 + 이월금 $25 = 가용예산 $50) -> 주가 $40이므로 1주 매수, 잔돈 $10 이월
    chunk_2 = 25.0
    bot._enter_chunk(price, chunk_2, "2회차 테스트 매수")

    print(f"  · [회차 2 후] 보유수량: {bot.pos.units}주, 잔돈이월: ${bot.budget_carryover:.2f}, 현금: ${bot.cash:.2f}")
    assert bot.pos.units == 1.0, "가용예산 $50으로 1주($40)가 매수되어야 함"
    assert abs(bot.budget_carryover - 10.0) < 1e-4, "남은 잔돈 $10가 이월되어야 함"
    assert abs(bot.cash - 960.0) < 1e-4, "현금 잔고는 실제 주식 매수 대금($40)만큼 차감되어 $960이어야 함"

    # 스냅샷 & 복원 영속화 테스트
    snap = bot.snapshot()
    assert "budgetCarryover" in snap
    assert snap["budgetCarryover"] == 10.0

    restored_bot = trader.TradingBot.restore(snap, account=None)
    assert restored_bot.budget_carryover == 10.0
    assert restored_bot.pos.units == 1.0
    assert restored_bot.cash == 960.0

    status_data = restored_bot.status()
    print(f"  · 상태 데이터 환산원화: {status_data['equityKrwConverted']:,}원 (환율 {status_data['fxRate']}원/$)")
    assert status_data["budgetCarryover"] == 10.0
    assert "equityKrwConverted" in status_data
    assert "realizedPnlKrwConverted" in status_data

    # 시나리오 3: 익절 청산 시 이월금 리셋 검증
    bot._exit(50.0, "목표 익절 전량 청산")
    assert bot.pos.units == 0.0
    assert bot.budget_carryover == 0.0, "익절 후 새 사이클을 위해 잔돈 이월금 리셋되어야 함"
    print("  ✅ 정수 주수 체결 및 잔돈 이월 검증 통과")


def test_tax_tracker_and_endpoints():
    print("\n[TEST 3] 연간 250만 원 양도소득세 비과세 트래커 및 API 엔드포인트 검증")

    # 1. /api/namuh/market_status 엔드포인트
    m_data = server.namuh_market_status()
    assert "isOpen" in m_data
    print(f"  · API /api/namuh/market_status 응답: {m_data['statusText']}")

    # 2. /api/namuh/tax 엔드포인트
    t_data = server.namuh_tax_status()
    assert t_data["success"] is True
    assert "taxTracker" in t_data
    tax = t_data["taxTracker"]
    print(f"  · 250만 원 공제 한도: {tax['deductionLimitKrw']:,}원")
    print(f"  · 당해연도 해외주식 실현익: ${tax['annualStockPnlUsd']:,.2f} (약 {tax['annualStockPnlKrw']:,}원)")
    print(f"  · 잔여 비과세 한도: {tax['remainingDeductionKrw']:,}원")
    print(f"  · 비과세 소진율: {tax['usagePct']}%")
    print(f"  · 예상 양도세(22%): {tax['estimatedTaxKrw']:,}원")
    print("  ✅ 양도세 비과세 트래커 및 API 검증 통과")


if __name__ == "__main__":
    test_market_schedule()
    test_budget_carryover_and_integer_shares()
    test_tax_tracker_and_endpoints()
    print("\n🎉 모든 미국 주식 특화 3대 핵심 기능 검증이 성공적으로 완료되었습니다!\n")
