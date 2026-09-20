"""매크로 국면 감지 적응형 변속 기어 (Macro Regime Adaptive Gear) 테스트."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import macro_regime


def test_regime_evaluation():
    print("\n[TEST 1] 매크로 국면 판정 로직 검증")
    # 1) 3단 강세장 (Bull): QQQ > 200일선 & VIX < 20
    r_bull = macro_regime.evaluate_regime(qqq_price=520.0, sma_200=480.0, vix=15.5)
    print(f"  · 강세장 판정: {r_bull['gearName']} (배수 {r_bull['sizingMultiplier']}x, 목표 +{r_bull['recommendedTargetProfitPct']}%)")
    assert r_bull["gear"] == macro_regime.GEAR_BULL
    assert r_bull["gearNumber"] == 3
    assert r_bull["sizingMultiplier"] == 1.2
    assert r_bull["recommendedTargetProfitPct"] == 12.0
    assert r_bull["isQqqAbove200"] is True

    # 2) 1단 위기/하락장 (Bear): QQQ < 200일선 & VIX >= 30
    r_bear = macro_regime.evaluate_regime(qqq_price=440.0, sma_200=480.0, vix=35.2)
    print(f"  · 약세장 판정: {r_bear['gearName']} (배수 {r_bear['sizingMultiplier']}x, 목표 +{r_bear['recommendedTargetProfitPct']}%)")
    assert r_bear["gear"] == macro_regime.GEAR_BEAR
    assert r_bear["gearNumber"] == 1
    assert r_bear["sizingMultiplier"] == 0.5
    assert r_bear["recommendedTargetProfitPct"] == 7.0
    assert r_bear["isQqqAbove200"] is False

    # 3) 2단 횡보/중립장 (Neutral): QQQ > 200일선이나 VIX가 25인 경우
    r_neut1 = macro_regime.evaluate_regime(qqq_price=500.0, sma_200=480.0, vix=24.0)
    print(f"  · 횡보장1 판정: {r_neut1['gearName']} (배수 {r_neut1['sizingMultiplier']}x, 목표 +{r_neut1['recommendedTargetProfitPct']}%)")
    assert r_neut1["gear"] == macro_regime.GEAR_NEUTRAL
    assert r_neut1["gearNumber"] == 2
    assert r_neut1["sizingMultiplier"] == 1.0

    # 4) 2단 횡보/중립장 (Neutral): QQQ < 200일선이나 VIX가 22인 경우
    r_neut2 = macro_regime.evaluate_regime(qqq_price=470.0, sma_200=480.0, vix=22.0)
    print(f"  · 횡보장2 판정: {r_neut2['gearName']} (배수 {r_neut2['sizingMultiplier']}x, 목표 +{r_neut2['recommendedTargetProfitPct']}%)")
    assert r_neut2["gear"] == macro_regime.GEAR_NEUTRAL
    assert r_neut2["gearNumber"] == 2

    print("  ✅ 매크로 국면 판정 로직 검증 통과")


def test_macro_regime_fetch_or_fallback():
    print("\n[TEST 2] 매크로 국면 데이터 조회 및 캐싱 검증")
    regime = macro_regime.get_macro_regime()
    print(f"  · 조회된 기어: {regime.get('gearName')}")
    print(f"  · QQQ: ${regime.get('qqqPrice')} (200 SMA: ${regime.get('qqqSma200')})")
    print(f"  · VIX: {regime.get('vix')}")
    assert "gear" in regime
    assert "sizingMultiplier" in regime
    assert "recommendedTargetProfitPct" in regime
    print("  ✅ 매크로 국면 데이터 조회 및 캐싱 검증 통과")


if __name__ == "__main__":
    test_regime_evaluation()
    test_macro_regime_fetch_or_fallback()
    print("\n🎉 모든 매크로 국면 테스트 통과!")
