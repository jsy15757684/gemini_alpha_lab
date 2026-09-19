"""매매 전략 — 지표 계산과 진입/청산 판단.

이 모듈은 외부 통신을 하지 않는 순수 함수만 담는다. 그래서
실시간 봇과 백테스트가 **같은 코드**로 판단한다.

예전 구조에서는 봇과 백테스터가 서로 다른 판단 로직을 갖고 있었다.
그러면 백테스트 결과가 실제 봇의 행동을 예측하지 못한다.

전략은 단순하고 추적 가능해야 한다:
  진입 — RSI 가 과매도 기준선을 아래에서 위로 통과할 때 (반등 확인 후 진입)
         역추세 진입이므로 추세 필터는 기본으로 끈다 (StrategyParams 주석 참고)
  청산 — 익절 / 손절 / 고점 대비 하락(트레일링) / RSI 과매수
각 판단에는 근거 문자열이 함께 나온다.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional


# ───────────────────────── 파라미터 ─────────────────────────

@dataclass
class StrategyParams:
    # ── 전략 유형 ──
    # "quant_ai" (기존 RSI/MA/Gemini AI 퀀트) | "raoer_infinite" (라오어 무한매수법) | "raoer_vr" (라오어 밸류리밸런싱)
    strategyType: str = "quant_ai"

    # ── 라오어 무한매수법 파라미터 ──
    splitCount: int = 40          # 분할 매수 횟수 (20~60, 기본 40분할)
    targetProfitPct: float = 10.0 # 무한매수 목표 익절 수익률 (+10.0%)
    quarterCutPct: float = 25.0   # 40회차 소진 시 쿼터 매도 방어율 (25%)
    raoerUseAi: bool = False      # ✨ Gemini AI 스마트 무한매수 (동적 비중 + 가변 익절)
    raoerMinProfitPct: float = 5.0    # 약세장 조기 익절선 (%)
    raoerMaxProfitPct: float = 20.0   # 강세장 최대 익절선 (%)
    raoerMaxMultiplier: float = 2.0   # 저점 과매도 시 집중 매수 최대 배수 (1.5~2.5배)

    # 추세에 따라 회차 매수를 조절한다. 기준은 재는 것이 아니라 먼저 정했다 —
    # 과거 데이터로 문턱을 고르면 그 순간 과최적화다(192조합 실험이 보여줬다).
    #   상승추세 = 종가가 slowMa(기본 30봉) 위 && slowMa 가 직전보다 높다
    #   하락추세 = 종가가 slowMa 아래 && slowMa 가 직전보다 낮다
    #   그 외 = 중립
    # "off"        : 추세를 보지 않는다 (라오어 원전, 기본값)
    # "pause_down" : 하락추세면 새 회차를 쉰다 (떨어지는 칼날 회피)
    # "boost_up"   : 상승추세면 회차 금액을 raoerMaxMultiplier 배로 (급등장 열세 대응)
    raoerTrendMode: str = "off"

    # ── 라오어 밸류 리밸런싱 (VR) 파라미터 ──
    vrGradient: float = 10.0      # VR 기울기 G (10~20)
    vrBandPct: float = 15.0       # VR 리밸런싱 밴드 (±15%)

    # ── USDT 환차익 (usdt_premium) ──────────────────────────
    # 빗썸 USDT 가격이 서울외환시장 공시환율보다 싸면(역프) 사고,
    # 비싸지면(김프) 판다. 거래소가 하나뿐이라 양다리 실패가 없다.
    #
    # 주의: 이건 무위험 차익거래가 아니다. 손익이 두 갈래다 —
    #   (1) 프리미엄 변화  (2) 원/달러 환율 변화
    # 프리미엄이 목표에 닿아도 그동안 환율이 내리면 원화 기준으로 손실일
    # 수 있다. 사실상 '싸게 산 달러를 들고 있는' 포지션이다.
    usdtBuyPremiumPct: float = -0.8    # 이 값 이하로 내려가면 매수
    usdtSellPremiumPct: float = 2.0    # 이 값 이상으로 올라가면 매도

    # ── 기존 퀀트 기술지표 파라미터 ──
    rsiPeriod: int = 14
    rsiBuy: float = 35.0          # 과매도 반등 매수 기준선
    rsiSell: float = 75.0         # 과매수 익절 기준선 (추세 이익 극대화)
    fastMa: int = 10              # 단기 이동평균
    slowMa: int = 30              # 장기 이동평균
    useTrendFilter: bool = False  # 추세 필터 (장기MA 위에서만 진입)
    
    # ── 퀀트 리스크 관리 (손익비 1:2.1 표준) ──
    takeProfitPct: float = 3.8    # 익절 (+3.8%)
    stopLossPct: float = 1.8      # 손절 (-1.8% 엄격 제한)
    trailingStopPct: float = 1.2  # 트레일링 스탑 (고점 대비 -1.2% 하락 시 이익 보존)
    feePct: float = 0.04          # 빗썸 시장가 수수료(%)

    # ── 퀀트 2중 진입 규칙 ──
    # RSI 반등 + 거래량 확인 (돈이 실린 진짜 반등 포착)
    entryRules: List[str] = field(default_factory=lambda: ["rsiCrossUp", "volumeSurge"])
    entryMode: str = "any"

    # 개별 규칙 파라미터
    volumeSurgeMult: float = 1.5   # 20봉 평균 대비 1.5배 이상 거래량
    breakoutLookback: int = 20     # 직전 20봉 최고가 돌파
    bbPeriod: int = 20             # 볼린저 밴드 기간
    bbStdMult: float = 2.0         # 볼린저 밴드 표준편차 배수
    macdFast: int = 12
    macdSlow: int = 26
    macdSignal: int = 9

    # ── Gemini AI 퀀트 검증 파라미터 ──
    useGemini: bool = True
    geminiMode: str = "hybrid"     # "hybrid" (지표 신호 + AI 승인) | "ai_only"
    geminiMinConfidence: int = 60  # 퀀트 최적 신뢰도 (60% 이상 승인)
    geminiModel: str = "gemini-flash-latest"

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "StrategyParams":
        d = d or {}
        p = cls()
        for k, v in d.items():
            if not hasattr(p, k) or v is None:
                continue
            cur = getattr(p, k)
            try:
                if isinstance(cur, list):
                    setattr(p, k, [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)])
                elif isinstance(cur, str):
                    setattr(p, k, str(v))
                elif isinstance(cur, bool):
                    setattr(p, k, bool(v))
                elif isinstance(cur, int):
                    setattr(p, k, int(v))
                else:
                    setattr(p, k, float(v))
            except (TypeError, ValueError):
                continue
        return p.validated()

    def validated(self) -> "StrategyParams":
        if self.raoerTrendMode not in ("off", "pause_down", "boost_up"):
            self.raoerTrendMode = "off"
        if self.strategyType not in ("quant_ai", "raoer_infinite", "raoer_vr", "usdt_premium"):
            self.strategyType = "quant_ai"
        self.splitCount = max(5, min(100, self.splitCount))
        self.targetProfitPct = max(0.5, min(100.0, self.targetProfitPct))
        self.quarterCutPct = max(5.0, min(50.0, self.quarterCutPct))
        self.vrGradient = max(1.0, min(100.0, self.vrGradient))
        self.vrBandPct = max(1.0, min(50.0, self.vrBandPct))
        # 매수선이 매도선보다 높으면 사자마자 파는 무한 루프가 된다.
        self.usdtBuyPremiumPct = max(-10.0, min(10.0, self.usdtBuyPremiumPct))
        self.usdtSellPremiumPct = max(-10.0, min(20.0, self.usdtSellPremiumPct))
        if self.usdtSellPremiumPct <= self.usdtBuyPremiumPct:
            self.usdtSellPremiumPct = self.usdtBuyPremiumPct + 0.5

        self.rsiPeriod = max(2, min(100, self.rsiPeriod))
        self.fastMa = max(2, min(200, self.fastMa))
        self.slowMa = max(self.fastMa + 1, min(300, self.slowMa))
        self.rsiBuy = max(1.0, min(99.0, self.rsiBuy))
        self.rsiSell = max(self.rsiBuy + 1.0, min(99.0, self.rsiSell))
        self.takeProfitPct = max(0.1, min(100.0, self.takeProfitPct))
        self.stopLossPct = max(0.1, min(100.0, self.stopLossPct))
        self.trailingStopPct = max(0.0, min(100.0, self.trailingStopPct))
        self.feePct = max(0.0, min(1.0, self.feePct))
        self.volumeSurgeMult = max(1.0, min(20.0, self.volumeSurgeMult))
        self.breakoutLookback = max(2, min(200, self.breakoutLookback))
        self.bbPeriod = max(2, min(200, self.bbPeriod))
        self.bbStdMult = max(0.1, min(5.0, self.bbStdMult))
        self.macdSlow = max(3, min(200, self.macdSlow))
        self.macdFast = max(2, min(self.macdSlow - 1, self.macdFast))
        self.macdSignal = max(2, min(100, self.macdSignal))
        self.geminiMinConfidence = max(0, min(100, self.geminiMinConfidence))
        if self.geminiMode not in ("ai_only", "hybrid"):
            self.geminiMode = "ai_only"

        # ENTRY_RULES 는 파일 하단에 정의된다. validated() 는 인스턴스 생성 후에만
        # 호출되므로 이 시점에는 이미 모듈이 끝까지 로드돼 있다.
        valid = [r for r in self.entryRules if r in ENTRY_RULES]
        self.entryRules = valid or ["rsiCrossUp"]
        if self.entryMode not in ("any", "all"):
            self.entryMode = "any"
        return self

    def warmup(self) -> int:
        """지표가 유효해지기까지 필요한 최소 캔들 수."""
        return max(self.slowMa, self.rsiPeriod + 1, self.bbPeriod,
                   self.breakoutLookback + 1, self.macdSlow + self.macdSignal) + 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ───────────────────────── 지표 ─────────────────────────

def _sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    total = sum(values[:period])
    out[period - 1] = total / period
    for i in range(period, len(values)):
        total += values[i] - values[i - period]
        out[i] = total / period
    return out


def _rsi(values: List[float], period: int) -> List[Optional[float]]:
    """Wilder 방식 RSI. 첫 구간은 단순평균, 이후 지수평활."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out

    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def _ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or not values:
        return out
    k = 2.0 / (period + 1)
    e = values[0]
    for i, v in enumerate(values):
        e = v if i == 0 else v * k + e * (1 - k)
        if i >= period - 1:
            out[i] = e
    return out


def _stdev(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        m = sum(w) / period
        out[i] = (sum((x - m) ** 2 for x in w) / period) ** 0.5
    return out


def compute_indicators(candles: List[Dict[str, Any]], p: StrategyParams) -> List[Dict[str, Any]]:
    """캔들마다 지표를 붙여 돌려준다. 원본은 수정하지 않는다.

    선택된 진입 규칙과 무관하게 전부 계산한다. 캔들 200개 기준 비용이
    미미하고, 화면에서 규칙을 바꿔가며 비교할 때 재계산이 단순해진다.
    """
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    vols = [c["volume"] for c in candles]

    rsi = _rsi(closes, p.rsiPeriod)
    fast = _sma(closes, p.fastMa)
    slow = _sma(closes, p.slowMa)

    bb_mid = _sma(closes, p.bbPeriod)
    bb_sd = _stdev(closes, p.bbPeriod)
    bb_low = [(bb_mid[i] - p.bbStdMult * bb_sd[i])
              if (bb_mid[i] is not None and bb_sd[i] is not None) else None
              for i in range(len(closes))]

    ema_f, ema_s = _ema(closes, p.macdFast), _ema(closes, p.macdSlow)
    macd = [(ema_f[i] - ema_s[i]) if (ema_f[i] is not None and ema_s[i] is not None) else None
            for i in range(len(closes))]
    macd_filled = [m if m is not None else 0.0 for m in macd]
    macd_sig_raw = _ema(macd_filled, p.macdSignal)
    macd_sig = [macd_sig_raw[i] if macd[i] is not None else None for i in range(len(closes))]

    vol_avg = _sma(vols, 20)
    prior_high = [max(highs[max(0, i - p.breakoutLookback):i]) if i >= p.breakoutLookback else None
                  for i in range(len(closes))]

    out = []
    for i, c in enumerate(candles):
        row = {**c,
               "rsi": rsi[i], "smaFast": fast[i], "smaSlow": slow[i],
               "bbLower": bb_low[i], "macd": macd[i], "macdSignal": macd_sig[i],
               "volAvg20": vol_avg[i], "priorHigh": prior_high[i]}
        row["ready"] = rsi[i] is not None and slow[i] is not None
        out.append(row)
    return out


# ───────────────────────── 판단 ─────────────────────────

@dataclass
class Position:
    units: float = 0.0
    entryPrice: float = 0.0
    peakPrice: float = 0.0
    turn: int = 0                  # 무한매수 진행 회차 T (1~splitCount)
    totalInvested: float = 0.0     # 총 투입 원금(원)
    vrTargetV: float = 0.0         # VR 목표 평가금액

    @property
    def open(self) -> bool:
        return self.units > 0


@dataclass
class Decision:
    action: str            # "BUY" | "SELL" | "BUY_CHUNK" | "SELL_QUARTER" | "BUY_PARTIAL" | "SELL_PARTIAL" | "HOLD"
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


def decide_raoer_infinite(price: float, pos: Position, p: StrategyParams) -> Decision:
    """라오어 무한매수법 판단 로직."""
    if pos.open:
        pnl_pct = (price - pos.entryPrice) / pos.entryPrice * 100.0
        # 1) 목표 수익률 도달 시 전량 익절
        if pnl_pct >= p.targetProfitPct:
            return Decision("SELL", f"무한매수 목표 익절 (+{pnl_pct:.2f}% ≥ +{p.targetProfitPct:.1f}%) [T={pos.turn}]",
                            {"rule": "raoerTakeProfit", "pnlPct": round(pnl_pct, 2), "turn": pos.turn})

        # 2) splitCount 소진 시 쿼터 매도 (25% 방어)
        if pos.turn >= p.splitCount:
            return Decision("SELL_QUARTER",
                            f"무한매수 {p.splitCount}분할 소진 쿼터매도 방어 ({p.quarterCutPct:.0f}% 매도, 손익 {pnl_pct:+.2f}%) [T={pos.turn}]",
                            {"rule": "raoerQuarterCut", "turn": pos.turn, "pnlPct": round(pnl_pct, 2)})

        # 3) 분할 매수 (T+1 회차)
        return Decision("BUY_CHUNK",
                        f"무한매수 {pos.turn + 1}/{p.splitCount}회차 분할 매수 (평단 대비 {pnl_pct:+.2f}%)",
                        {"rule": "raoerBuyChunk", "turn": pos.turn + 1, "pnlPct": round(pnl_pct, 2)})
    else:
        # 미보유 상태: 1회차 매수 시작
        return Decision("BUY_CHUNK",
                        f"무한매수 1/{p.splitCount}회차 첫 매수 시작",
                        {"rule": "raoerFirstBuy", "turn": 1, "pnlPct": 0.0})


def decide_raoer_vr(price: float, pos: Position, p: StrategyParams, total_equity: float, cash: float) -> Decision:
    """라오어 밸류 리밸런싱 (VR) 판단 로직."""
    cur_val = pos.units * price
    target_v = pos.vrTargetV if pos.vrTargetV > 0 else (total_equity * 0.5)
    next_v = target_v + (cash / max(1.0, p.vrGradient))
    band_high = next_v * (1.0 + p.vrBandPct / 100.0)
    band_low = next_v * (1.0 - p.vrBandPct / 100.0)

    if cur_val > band_high and pos.units > 0:
        excess = cur_val - next_v
        return Decision("SELL_PARTIAL",
                        f"VR 상단 밴드 초과 매도 (평가액 {cur_val:,.0f} > 상단 {band_high:,.0f})",
                        {"rule": "vrSell", "targetV": next_v, "amount": excess})
    elif cur_val < band_low and cash >= 5000:
        deficit = min(cash, next_v - cur_val)
        return Decision("BUY_PARTIAL",
                        f"VR 하단 밴드 이탈 매수 (평가액 {cur_val:,.0f} < 하단 {band_low:,.0f})",
                        {"rule": "vrBuy", "targetV": next_v, "amount": deficit})
    else:
        return Decision("HOLD",
                        f"VR 밴드 내 유지 (평가액 {cur_val:,.0f}, 목표V {next_v:,.0f})",
                        {"rule": "vrHold", "targetV": next_v})


def decide(bars: List[Dict[str, Any]], i: int, price: float,
           pos: Position, p: StrategyParams) -> Decision:
    """i 번째 캔들 시점의 판단.

    bars  : compute_indicators 결과
    i     : 판단 기준 캔들 (지표가 확정된 캔들)
    price : 체결 기준 가격. 실시간 봇은 현재 호가, 백테스트는 해당 캔들 종가.
    """
    if p.strategyType == "raoer_infinite":
        return decide_raoer_infinite(price, pos, p)

    bar = bars[i]
    if not bar.get("ready"):
        return Decision("HOLD", "지표 준비 안 됨 (캔들 부족)")

    rsi = bar["rsi"]
    sma_slow = bar["smaSlow"]

    # ── 보유 중: 청산 조건 ─────────────────────────────
    if pos.open:
        pnl_pct = (price - pos.entryPrice) / pos.entryPrice * 100.0

        if pnl_pct <= -p.stopLossPct:
            return Decision("SELL", f"손절 (-{abs(pnl_pct):.2f}%)",
                            {"rule": "stopLoss", "pnlPct": round(pnl_pct, 2)})

        if pnl_pct >= p.takeProfitPct:
            return Decision("SELL", f"익절 (+{pnl_pct:.2f}%)",
                            {"rule": "takeProfit", "pnlPct": round(pnl_pct, 2)})

        if p.trailingStopPct > 0 and pos.peakPrice > 0:
            drop = (pos.peakPrice - price) / pos.peakPrice * 100.0
            # 고점 대비 하락은 '이익 구간에 들어선 뒤' 에만 발동시킨다.
            if pnl_pct > 0 and drop >= p.trailingStopPct:
                return Decision("SELL",
                                f"트레일링 스탑 (고점 대비 -{drop:.2f}%, 손익 {pnl_pct:+.2f}%)",
                                {"rule": "trailingStop", "dropPct": round(drop, 2),
                                 "pnlPct": round(pnl_pct, 2)})

        if rsi >= p.rsiSell:
            return Decision("SELL", f"RSI 과매수 청산 (RSI {rsi:.1f} ≥ {p.rsiSell:.0f}, 손익 {pnl_pct:+.2f}%)",
                            {"rule": "rsiSell", "rsi": round(rsi, 1),
                             "pnlPct": round(pnl_pct, 2)})

        return Decision("HOLD", f"보유 중 (손익 {pnl_pct:+.2f}%, RSI {rsi:.1f})")

    # ── 미보유: 진입 조건 ─────────────────────────────
    if i < 1:
        return Decision("HOLD", "직전 캔들 없음")

    results = []
    for name in p.entryRules:
        rule = ENTRY_RULES.get(name)
        if not rule:
            continue
        ok, desc = rule["fn"](bars, i, p)
        results.append((rule["label"], ok, desc))

    if not results:
        return Decision("HOLD", "선택된 진입 규칙이 없습니다")

    hits = [r for r in results if r[1]]
    satisfied = (len(hits) == len(results)) if p.entryMode == "all" else bool(hits)

    if not satisfied:
        pending = " / ".join(f"{lbl}: {desc}" for lbl, ok, desc in results if not ok)
        joiner = "전부 충족 필요" if p.entryMode == "all" else "하나 이상 충족 필요"
        return Decision("HOLD", f"진입 대기 ({joiner}) — {pending}")

    if p.useTrendFilter and sma_slow is not None and bar["close"] < sma_slow:
        return Decision("HOLD",
                        f"추세 필터 (종가 {bar['close']:,.0f} < {p.slowMa}봉 평균 {sma_slow:,.0f})")

    why = " + ".join(f"{lbl}({desc})" for lbl, ok, desc in hits)
    return Decision("BUY", f"진입 신호 — {why}",
                    {"rule": "entry", "rules": [lbl for lbl, ok, _ in hits],
                     "mode": p.entryMode, "rsi": round(rsi, 1)})


# ───────────────────────── 진입 규칙 ─────────────────────────
#
# 각 규칙은 (충족 여부, 짧은 설명) 을 돌려준다. 설명은 화면과 로그에 그대로 쓰인다.
# 지표가 아직 없으면 '충족 안 됨' 으로 처리한다 — 없는 값을 추정하지 않는다.

def _rule_rsi_cross_up(bars, i, p):
    prev, cur = bars[i - 1]["rsi"], bars[i]["rsi"]
    if prev is None or cur is None:
        return False, "RSI 준비 안 됨"
    if prev < p.rsiBuy <= cur:
        return True, f"{prev:.1f}→{cur:.1f}"
    return False, f"RSI {prev:.1f}→{cur:.1f} (기준 {p.rsiBuy:.0f} 상향돌파 필요)"


def _rule_ma_golden_cross(bars, i, p):
    pf, ps = bars[i - 1]["smaFast"], bars[i - 1]["smaSlow"]
    cf, cs = bars[i]["smaFast"], bars[i]["smaSlow"]
    if None in (pf, ps, cf, cs):
        return False, "이동평균 준비 안 됨"
    if pf <= ps and cf > cs:
        return True, f"MA{p.fastMa}이 MA{p.slowMa} 상향돌파"
    return False, f"MA{p.fastMa} {'>' if cf > cs else '<='} MA{p.slowMa} (교차 시점 아님)"


def _rule_bb_lower_reclaim(bars, i, p):
    pc, pb = bars[i - 1]["close"], bars[i - 1]["bbLower"]
    cc, cb = bars[i]["close"], bars[i]["bbLower"]
    if None in (pb, cb):
        return False, "볼린저 준비 안 됨"
    if pc < pb and cc >= cb:
        return True, "하단 이탈 후 복귀"
    return False, "하단 이탈 후 복귀 아님"


def _rule_volume_surge(bars, i, p):
    v, avg = bars[i]["volume"], bars[i]["volAvg20"]
    if avg is None or avg <= 0:
        return False, "거래량 평균 준비 안 됨"
    ratio = v / avg
    bullish = bars[i]["close"] > bars[i]["open"]
    if ratio >= p.volumeSurgeMult and bullish:
        return True, f"평균 대비 {ratio:.1f}배 + 양봉"
    return False, f"거래량 {ratio:.1f}배 (기준 {p.volumeSurgeMult:.1f}배{'' if bullish else ', 음봉'})"


def _rule_breakout(bars, i, p):
    ph = bars[i]["priorHigh"]
    if ph is None:
        return False, "전고점 준비 안 됨"
    if bars[i]["close"] > ph:
        return True, f"{p.breakoutLookback}봉 전고점 돌파"
    gap = (ph - bars[i]["close"]) / ph * 100
    return False, f"전고점까지 {gap:.2f}% 남음"


def _rule_macd_golden_cross(bars, i, p):
    pm, psig = bars[i - 1]["macd"], bars[i - 1]["macdSignal"]
    cm, csig = bars[i]["macd"], bars[i]["macdSignal"]
    if None in (pm, psig, cm, csig):
        return False, "MACD 준비 안 됨"
    if pm <= psig and cm > csig:
        return True, "MACD가 시그널 상향돌파"
    return False, f"MACD {'>' if cm > csig else '<='} 시그널 (교차 시점 아님)"


ENTRY_RULES: Dict[str, Dict[str, Any]] = {
    "rsiCrossUp":      {"label": "RSI 상향돌파",   "fn": _rule_rsi_cross_up,
                        "desc": "RSI 가 매수 기준선을 아래에서 위로 통과 (반등 확인 후 진입)"},
    "maGoldenCross":   {"label": "MA 골든크로스",  "fn": _rule_ma_golden_cross,
                        "desc": "단기 이동평균이 장기 이동평균을 상향 돌파"},
    "bbLowerReclaim":  {"label": "볼린저 하단복귀", "fn": _rule_bb_lower_reclaim,
                        "desc": "볼린저 하단을 이탈했다가 다시 위로 복귀"},
    "volumeSurge":     {"label": "거래량 급증",    "fn": _rule_volume_surge,
                        "desc": "20봉 평균 대비 설정 배수 이상 + 양봉"},
    "breakout":        {"label": "전고점 돌파",    "fn": _rule_breakout,
                        "desc": "직전 N봉 최고가를 종가가 상향 돌파"},
    "macdGoldenCross": {"label": "MACD 골든크로스", "fn": _rule_macd_golden_cross,
                        "desc": "MACD 선이 시그널 선을 상향 돌파"},
}


def entry_rule_catalog() -> List[Dict[str, str]]:
    """화면에 뿌릴 규칙 목록."""
    return [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in ENTRY_RULES.items()]
