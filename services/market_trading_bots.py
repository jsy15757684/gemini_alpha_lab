import time
import random
from datetime import datetime
from typing import Dict, Any, List

# 시장에서 가장 인기 있는 6대 검증된 AI 트레이딩 봇 (Market Leaderboard)
FEATURED_AI_BOTS = [
    {
        "id": "bot_gemini_sniper",
        "name": "Gemini AI 실시간 뉴스 스나이퍼",
        "type": "AI_SIGNAL",
        "category": "모멘텀 돌파",
        "targetAsset": "NVDA",
        "apy": 142.8,
        "winRate": 78.4,
        "mdd": 6.8,
        "sharpe": 2.85,
        "activeUsers": 2450,
        "description": "실시간 외신 뉴스 및 공시 감성 점수 80점 이상 돌파 시 거래량 폭증과 함께 초고속 진입 후 ATR 트레일링으로 고수익을 확정하는 기관급 봇.",
        "badge": "🔥 수익률 1위",
        "config": {
            "strategyType": "ai_sniper",
            "fastMa": 5, "slowMa": 20, "rsiBuy": 40.0, "rsiSell": 75.0,
            "takeProfitPct": 15.0, "stopLossPct": 4.0,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 150,
            "enableAiSentimentGate": True, "minSentimentScore": 75,
            "enableBollingerSqueeze": True, "enableMacdMomentum": True,
            "enableTrailingStop": True, "trailingStopPct": 3.0,
            "enableScaleInOut": True
        }
    },
    {
        "id": "bot_infinity_grid",
        "name": "인피니티 AI 그리드 봇 (Grid Bot)",
        "type": "GRID",
        "category": "박스권 횡보 극복",
        "targetAsset": "TSLA",
        "apy": 89.4,
        "winRate": 92.1,
        "mdd": 4.2,
        "sharpe": 2.45,
        "activeUsers": 3890,
        "description": "설정된 가격 밴드 내에서 24시간 쉬지 않고 미세하게 20단계 분할 매수/매도를 반복하여 변동성을 100% 현금 수익으로 전환하는 무패 봇.",
        "badge": "⚡ 승률 92%",
        "config": {
            "strategyType": "grid_trading",
            "fastMa": 3, "slowMa": 10, "rsiBuy": 35.0, "rsiSell": 65.0,
            "takeProfitPct": 6.0, "stopLossPct": 4.5,
            "enableVolumeSurge": False,
            "enableAiSentimentGate": True, "minSentimentScore": 55,
            "enableBollingerSqueeze": True, "enableMacdMomentum": True,
            "enableTrailingStop": False,
            "enableScaleInOut": True
        }
    },
    {
        "id": "bot_dca_martingale",
        "name": "스마트 DCA 평단가 방어 봇",
        "type": "DCA",
        "category": "하락장 무적 방어",
        "targetAsset": "BTC-USD",
        "apy": 118.6,
        "winRate": 84.5,
        "mdd": 7.5,
        "sharpe": 2.62,
        "activeUsers": 4120,
        "description": "주가 하락 시마다 일정 비율로 3단계 스마트 분할 매수를 집행해 평단가를 극적으로 낮추고, 단 2~3%의 기술적 반등 시에도 전량 플러스 익절 청산.",
        "badge": "🛡️ 안정성 최강",
        "config": {
            "strategyType": "dca_martingale",
            "fastMa": 10, "slowMa": 30, "rsiBuy": 30.0, "rsiSell": 70.0,
            "takeProfitPct": 10.0, "stopLossPct": 6.0,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 120,
            "enableAiSentimentGate": True, "minSentimentScore": 60,
            "enableBollingerSqueeze": True, "enableMacdMomentum": True,
            "enableTrailingStop": True, "trailingStopPct": 3.5,
            "enableScaleInOut": True
        }
    },
    {
        "id": "bot_lynch_tenbagger",
        "name": "피터 린치 텐배거 성장 봇",
        "type": "GURU",
        "category": "중장기 스노우볼",
        "targetAsset": "PLTR",
        "apy": 135.2,
        "winRate": 74.2,
        "mdd": 8.1,
        "sharpe": 2.38,
        "activeUsers": 1820,
        "description": "PEG 1.0 미만, 분기 매출 성장률 25% 이상인 고성장 혁신 기업에 집중하여 주가가 52주 신고가를 돌파할 때 피라미딩(불타기)으로 수익 극대화.",
        "badge": "🚀 텐배거 유망",
        "config": {
            "strategyType": "lynch_growth",
            "fastMa": 5, "slowMa": 20, "rsiBuy": 38.0, "rsiSell": 78.0,
            "takeProfitPct": 25.0, "stopLossPct": 5.0,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 160,
            "enableAiSentimentGate": True, "minSentimentScore": 70,
            "enableBollingerSqueeze": True, "enableMacdMomentum": True,
            "enableTrailingStop": True, "trailingStopPct": 4.0,
            "enableScaleInOut": True
        }
    },
    {
        "id": "bot_simons_quant",
        "name": "짐 시몬스 통계적 차익거래 봇",
        "type": "QUANT",
        "category": "초단기 스윙",
        "targetAsset": "ETH-USD",
        "apy": 164.5,
        "winRate": 76.8,
        "mdd": 6.2,
        "sharpe": 3.10,
        "activeUsers": 2980,
        "description": "르네상스 테크놀로지의 알고리즘을 벤치마킹하여 볼린저 밴드 하단과 RSI 과매도 불균형 발생 시 미세 차익을 초단기로 수취하는 퀀트 봇.",
        "badge": "🤖 퀀트 1위",
        "config": {
            "strategyType": "simons_quant",
            "fastMa": 3, "slowMa": 15, "rsiBuy": 30.0, "rsiSell": 68.0,
            "takeProfitPct": 8.0, "stopLossPct": 3.5,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 170,
            "enableAiSentimentGate": True, "minSentimentScore": 60,
            "enableBollingerSqueeze": True, "enableMacdMomentum": True,
            "enableTrailingStop": True, "trailingStopPct": 2.5,
            "enableScaleInOut": False
        }
    },
    {
        "id": "bot_buffett_moat",
        "name": "워런 버핏 해자 가치투자 봇",
        "type": "VALUE",
        "category": "안정적 대형주",
        "targetAsset": "005930.KS",
        "apy": 58.4,
        "winRate": 88.5,
        "mdd": 3.8,
        "sharpe": 2.20,
        "activeUsers": 5200,
        "description": "국내외 압도적 독점 해자를 보유한 초우량주(삼성전자, 애플 등)가 일시적 악재로 과매도될 때 저점 분할 매집하여 안정적인 복리 수익을 실현.",
        "badge": "👔 복리 배당",
        "config": {
            "strategyType": "buffett_value",
            "fastMa": 20, "slowMa": 60, "rsiBuy": 40.0, "rsiSell": 75.0,
            "takeProfitPct": 20.0, "stopLossPct": 6.0,
            "enableVolumeSurge": False,
            "enableAiSentimentGate": True, "minSentimentScore": 68,
            "enableTrailingStop": True, "trailingStopPct": 5.0,
            "enableScaleInOut": True
        }
    }
]

def get_marketplace_bots() -> List[Dict[str, Any]]:
    return FEATURED_AI_BOTS

def get_bot_by_id(bot_id: str) -> Dict[str, Any]:
    for b in FEATURED_AI_BOTS:
        if b["id"] == bot_id:
            return b
    return FEATURED_AI_BOTS[0]
