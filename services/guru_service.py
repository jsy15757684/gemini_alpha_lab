from typing import List, Dict, Any

GURU_MASTERS = [
    {
        "id": "buffett",
        "name": "워런 버핏 (Warren Buffett)",
        "firm": "버크셔 해서웨이 (Berkshire Hathaway)",
        "title": "오마하의 현인 / 해자 가치투자 대가",
        "avatar": "👔",
        "philosophy": "뛰어난 기업을 적정 가격에 사서 영원히 보유하라. 독점적 경제적 해자(Moat)와 높은 ROE(>15%), 강력한 잉여현금흐름(FCF)이 핵심이다.",
        "keyMetrics": "ROE > 15%, 부채비율 < 60%, 지속적 자사주 매입, 독점력",
        "strategyParams": {
            "strategyType": "buffett_value",
            "fastMa": 20, "slowMa": 60, "rsiBuy": 40.0, "rsiSell": 75.0,
            "takeProfitPct": 25.0, "stopLossPct": 7.0,
            "enableVolumeSurge": False, "enableAiSentimentGate": True, "minSentimentScore": 70,
            "enableTrailingStop": True, "trailingStopPct": 5.0,
            "enableMarketRegime": True, "enableScaleInOut": True
        },
        "recommendedPicks": [
            {"symbol": "AAPL", "name": "애플", "reason": "버크셔 포트폴리오 1위 비중. 강력한 iOS 생태계 락인과 막대한 자사주 소각.", "targetReturn": "+22%"},
            {"symbol": "MSFT", "name": "마이크로소프트", "reason": "클라우드(Azure)와 오피스 독점력. 견고한 ROE 35%+ 유지.", "targetReturn": "+19%"},
            {"symbol": "005930.KS", "name": "삼성전자", "reason": "글로벌 메모리 반도체 1위 해자 보유 및 바닥권 밸류에이션 매력.", "targetReturn": "+28%"},
            {"symbol": "KO", "name": "코카콜라", "reason": "100년 브랜드 파워와 50년 연속 배당 증액의 안정성.", "targetReturn": "+14%"}
        ]
    },
    {
        "id": "lynch",
        "name": "피터 린치 (Peter Lynch)",
        "firm": "피델리티 마젤란 펀드 (Magellan Fund)",
        "title": "전설의 텐배거(10배주) 사냥꾼",
        "avatar": "📈",
        "philosophy": "생활 속에서 폭발적으로 성장하는 기업을 찾아라. PEG(PER/성장률)가 1.0 미만인 고성장주(연 20%+ 성장)에 집중 투자하라.",
        "keyMetrics": "PEG < 1.0, 매출성장률 > 20%, 현금흐름 턴어라운드",
        "strategyParams": {
            "strategyType": "lynch_growth",
            "fastMa": 5, "slowMa": 20, "rsiBuy": 35.0, "rsiSell": 70.0,
            "takeProfitPct": 30.0, "stopLossPct": 6.0,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 140,
            "enableAiSentimentGate": True, "minSentimentScore": 65,
            "enableTrailingStop": True, "trailingStopPct": 4.0,
            "enableMarketRegime": True, "enableScaleInOut": True
        },
        "recommendedPicks": [
            {"symbol": "NVDA", "name": "엔비디아", "reason": "AI 인프라 필수재 독점. 높은 성장률 대비 매력적인 PEG 지표.", "targetReturn": "+45%"},
            {"symbol": "PLTR", "name": "팔란티어", "reason": "정부/기업용 AI 소프트웨어(AIP) 폭발적 고객사 확대 및 영업이익 급증.", "targetReturn": "+38%"},
            {"symbol": "000660.KS", "name": "SK하이닉스", "reason": "HBM(고대역폭 메모리) 시장 점유율 1위 질주 및 사상 최대 실적 랠리.", "targetReturn": "+32%"},
            {"symbol": "AMZN", "name": "아마존", "reason": "AWS 클라우드 + 이커머스 AI 물류 효율화에 따른 마진 극대화.", "targetReturn": "+26%"}
        ]
    },
    {
        "id": "simons",
        "name": "짐 시몬스 (Jim Simons)",
        "firm": "르네상스 테크놀로지 (Renaissance Tech)",
        "title": "퀀트 투자의 신 (연평균 수익률 66%)",
        "avatar": "🤖",
        "philosophy": "인간의 감정을 배제하고 순수 수학과 통계 알고리즘으로만 매매하라. 미세한 가격 패턴의 불균형(Arbitrage)과 평균 회귀를 공략한다.",
        "keyMetrics": "단기 모멘텀, 볼린저 밴드 반등, 머신러닝 패턴 일치율 80%+",
        "strategyParams": {
            "strategyType": "simons_quant",
            "fastMa": 3, "slowMa": 15, "rsiBuy": 30.0, "rsiSell": 68.0,
            "takeProfitPct": 10.0, "stopLossPct": 3.5,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 160,
            "enableAiSentimentGate": True, "minSentimentScore": 60,
            "enableTrailingStop": True, "trailingStopPct": 2.5,
            "enableMarketRegime": False, "enableScaleInOut": False
        },
        "recommendedPicks": [
            {"symbol": "TSLA", "name": "테슬라", "reason": "변동성이 풍부하여 단기 통계적 모멘텀 및 스윙 차익거래 최적 종목.", "targetReturn": "+24%"},
            {"symbol": "BTC-USD", "name": "비트코인", "reason": "24시간 유동성과 명확한 온체인 수급 사이클 패턴 존재.", "targetReturn": "+35%"},
            {"symbol": "COIN", "name": "코인베이스", "reason": "가상자산 거래량 변동성에 따른 초단기 고수익 퀀트 타점 다수 발생.", "targetReturn": "+29%"},
            {"symbol": "IONQ", "name": "아이온큐", "reason": "양자컴퓨팅 테마의 높은 베타 계수와 단기 지지선 반등 탄력성.", "targetReturn": "+40%"}
        ]
    },
    {
        "id": "dalio",
        "name": "레이 달리오 (Ray Dalio)",
        "firm": "브리지워터 (Bridgewater Associates)",
        "title": "헤지펀드의 제왕 / 올웨더(All-Weather) 창시자",
        "avatar": "🌐",
        "philosophy": "미래를 예측하려 하지 말고, 인플레이션/디플레이션/성장/침체 사계절 모두에서 돈을 버는 무위험 리스크 패리티 자산배분을 구축하라.",
        "keyMetrics": "상관관계 0 이하 분산, 변동성 통제, 샤프지수 극대화",
        "strategyParams": {
            "strategyType": "dalio_allweather",
            "fastMa": 50, "slowMa": 200, "rsiBuy": 45.0, "rsiSell": 75.0,
            "takeProfitPct": 15.0, "stopLossPct": 4.0,
            "enableVolumeSurge": False, "enableAiSentimentGate": True, "minSentimentScore": 60,
            "enableTrailingStop": True, "trailingStopPct": 3.0,
            "enableMarketRegime": True, "enableScaleInOut": True
        },
        "recommendedPicks": [
            {"symbol": "SPY", "name": "S&P 500 ETF", "reason": "미국 최우량 500개 기업 자산배분의 절대적 코어 자산.", "targetReturn": "+12%"},
            {"symbol": "QQQ", "name": "나스닥 100 ETF", "reason": "글로벌 기술 혁신 주도주로 포트폴리오 성장성 견인.", "targetReturn": "+18%"},
            {"symbol": "GLD", "name": "금(Gold) ETF", "reason": "통화 가치 하락 및 지정학적 리스크 완벽 헤지 자산.", "targetReturn": "+15%"},
            {"symbol": "BTC-USD", "name": "디지털 금 (비트코인)", "reason": "차세대 디지털 가치 저장 수단으로 포트폴리오 알파 기여.", "targetReturn": "+30%"}
        ]
    },
    {
        "id": "greenblatt",
        "name": "조엘 그린블라트 (Joel Greenblatt)",
        "firm": "고담 에셋 (Gotham Asset Management)",
        "title": "마법 공식 (The Magic Formula)의 창시자",
        "avatar": "🪄",
        "philosophy": "단 두 가지 지표만 보면 된다: '자본수익률(ROC)이 높은 좋은 회사'를 '이익수익률(Earnings Yield)이 높은 싼 가격'에 사라.",
        "keyMetrics": "높은 ROC (자본 효율성) + 높은 EBIT/EV (극단적 저평가)",
        "strategyParams": {
            "strategyType": "magic_formula",
            "fastMa": 10, "slowMa": 40, "rsiBuy": 38.0, "rsiSell": 72.0,
            "takeProfitPct": 20.0, "stopLossPct": 5.5,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 130,
            "enableAiSentimentGate": True, "minSentimentScore": 65,
            "enableTrailingStop": True, "trailingStopPct": 4.0,
            "enableMarketRegime": True, "enableScaleInOut": True
        },
        "recommendedPicks": [
            {"symbol": "GOOGL", "name": "알파벳 (구글)", "reason": "막대한 검색/유튜브 독점 현금 창출력 대비 PER 20배 초반의 극저평가 매력.", "targetReturn": "+25%"},
            {"symbol": "META", "name": "메타 (페이스북)", "reason": "광고 효율 극대화 및 메타 AI 오픈소스 생태계 지배력.", "targetReturn": "+27%"},
            {"symbol": "005380.KS", "name": "현대차", "reason": "사상 최대 영업이익률 및 글로벌 전기/하이브리드차 시장 점유율 확대.", "targetReturn": "+22%"},
            {"symbol": "QCOM", "name": "퀄컴", "reason": "온디바이스 AI 칩셋 독점과 높은 잉여현금 창출력.", "targetReturn": "+20%"}
        ]
    },
    {
        "id": "livermore",
        "name": "제시 리버모어 (Jesse Livermore)",
        "firm": "전설의 월가 개인 트레이더",
        "title": "추세추종 및 피라미딩(불타기)의 아버지",
        "avatar": "⚡",
        "philosophy": "시장의 저항선(신고가)을 강력하게 돌파할 때 매수하고, 이익이 나면 포지션을 늘려라(피라미딩). 손실은 가차 없이 짧게 잘라라.",
        "keyMetrics": "52주 신고가 돌파, 거래량 폭증, 주도주 추세 추종",
        "strategyParams": {
            "strategyType": "livermore_breakout",
            "fastMa": 5, "slowMa": 20, "rsiBuy": 40.0, "rsiSell": 80.0,
            "takeProfitPct": 25.0, "stopLossPct": 4.0,
            "enableVolumeSurge": True, "volumeSurgeThreshold": 180,
            "enableAiSentimentGate": True, "minSentimentScore": 60,
            "enableTrailingStop": True, "trailingStopPct": 3.0,
            "enableMarketRegime": True, "enableScaleInOut": True
        },
        "recommendedPicks": [
            {"symbol": "NVDA", "name": "엔비디아", "reason": "역사적 신고가 영역을 거래량과 함께 뚫어내는 전형적인 시장 최강 주도주.", "targetReturn": "+35%"},
            {"symbol": "PLTR", "name": "팔란티어", "reason": "강력한 저항선 상향 돌파 후 기관 대량 매수세 유입 지속.", "targetReturn": "+32%"},
            {"symbol": "TSM", "name": "TSMC", "reason": "글로벌 첨단 파운드리 90% 독점 및 전고점 돌파 랠리.", "targetReturn": "+24%"},
            {"symbol": "BTC-USD", "name": "비트코인", "reason": "사상 최고가 돌파 국면에서 폭발적인 모멘텀 시세 분출.", "targetReturn": "+40%"}
        ]
    }
]

def get_all_gurus() -> List[Dict[str, Any]]:
    return GURU_MASTERS

def get_guru_by_id(guru_id: str) -> Dict[str, Any]:
    for g in GURU_MASTERS:
        if g["id"] == guru_id:
            return g
    return GURU_MASTERS[0]
