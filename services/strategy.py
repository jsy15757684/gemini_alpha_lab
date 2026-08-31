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
    rsiPeriod: int = 14
    rsiBuy: float = 35.0          # 이 값을 아래에서 위로 통과하면 진입 후보
    rsiSell: float = 70.0         # 이 값 이상이면 과매수 청산
    fastMa: int = 10
    slowMa: int = 30
    # 추세 필터 기본값은 끔.
    # RSI 과매도 반등은 정의상 '가격이 이동평균 아래' 일 때 발생하므로,
    # '종가 > 장기MA' 를 요구하면 두 조건이 배타적이 되어 진입 신호가 사라진다.
    # 실측: 5종목 x 2간격(각 ~170봉)에서 필터 없음 35회 진입 -> 어떤 필터를 걸어도 0~1회.
    # 옵션은 남겨두되(다른 RSI 기준선에서는 의미가 있을 수 있다) 기본은 끈다.
    useTrendFilter: bool = False
    takeProfitPct: float = 4.0
    stopLossPct: float = 2.0
    trailingStopPct: float = 0.0  # 0 이면 사용 안 함
    feePct: float = 0.04          # 빗썸 시장가 수수료(%). 백테스트와 손익 계산에 반영

    # ── 진입 규칙 선택 ──────────────────────────────────────
    # 기본은 rsiCrossUp 하나. 여러 개를 고르면 entryMode 로 결합 방식을 정한다.
    #   "any" = 하나라도 충족하면 진입 (신호가 잦아짐)
    #   "all" = 전부 충족해야 진입 (신호가 드물어짐)
    #
    # 어떤 조합이 좋은지는 코드가 단정하지 않는다. 20개 데이터셋(5코인 x 4간격)
    # 측정에서 '양 구간 모두 시장을 이긴' 조합은 없었다. 백테스트로 직접
    # 비교해 보고 고르라는 뜻에서 옵션으로만 제공한다.
    entryRules: List[str] = field(default_factory=lambda: ["rsiCrossUp"])
    entryMode: str = "any"

    # 개별 규칙 파라미터
    volumeSurgeMult: float = 2.0   # volumeSurge: 20봉 평균 거래량의 몇 배
    breakoutLookback: int = 20     # breakout: 직전 N봉 최고가 돌파
    bbPeriod: int = 20             # bollinger 기간
    bbStdMult: float = 2.0         # bollinger 표준편차 배수
    macdFast: int = 12
    macdSlow: int = 26
    macdSignal: int = 9

    # Gemini AI 매매 파라미터
    useGemini: bool = False
    geminiMode: str = "ai_only"    # "ai_only" (AI 판단만으로 매매) | "hybrid" (기술지표 + AI 승인)
    geminiMinConfidence: int = 70  # AI 진입 최소 신뢰도 (0~100)
    geminiModel: str = "gemini-2.5-flash"

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

    @property
    def open(self) -> bool:
        return self.units > 0


@dataclass
class Decision:
    action: str            # "BUY" | "SELL" | "HOLD"
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


def decide(bars: List[Dict[str, Any]], i: int, price: float,
           pos: Position, p: StrategyParams) -> Decision:
    """i 번째 캔들 시점의 판단.

    bars  : compute_indicators 결과
    i     : 판단 기준 캔들 (지표가 확정된 캔들)
    price : 체결 기준 가격. 실시간 봇은 현재 호가, 백테스트는 해당 캔들 종가.
    """
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
