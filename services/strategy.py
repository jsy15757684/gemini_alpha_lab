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

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "StrategyParams":
        d = d or {}
        p = cls()
        for k, v in d.items():
            if not hasattr(p, k) or v is None:
                continue
            cur = getattr(p, k)
            try:
                setattr(p, k, bool(v) if isinstance(cur, bool)
                        else int(v) if isinstance(cur, int) else float(v))
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
        return self

    def warmup(self) -> int:
        """지표가 유효해지기까지 필요한 최소 캔들 수."""
        return max(self.slowMa, self.rsiPeriod + 1) + 1

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


def compute_indicators(candles: List[Dict[str, Any]], p: StrategyParams) -> List[Dict[str, Any]]:
    """캔들마다 rsi / smaFast / smaSlow 를 붙여 돌려준다. 원본은 수정하지 않는다."""
    closes = [c["close"] for c in candles]
    rsi = _rsi(closes, p.rsiPeriod)
    fast = _sma(closes, p.fastMa)
    slow = _sma(closes, p.slowMa)
    return [
        {**c, "rsi": rsi[i], "smaFast": fast[i], "smaSlow": slow[i],
         "ready": rsi[i] is not None and slow[i] is not None}
        for i, c in enumerate(candles)
    ]


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
    prev_rsi = bars[i - 1]["rsi"] if i > 0 else None
    if prev_rsi is None:
        return Decision("HOLD", "직전 RSI 없음")

    # 과매도 구간을 아래에서 위로 통과 = 반등 확인 후 진입 (하락 중 매수 방지)
    crossed_up = prev_rsi < p.rsiBuy <= rsi
    if not crossed_up:
        return Decision("HOLD",
                        f"진입 대기 (RSI {prev_rsi:.1f}→{rsi:.1f}, 기준 {p.rsiBuy:.0f} 상향 돌파 필요)")

    if p.useTrendFilter and bar["close"] < sma_slow:
        return Decision("HOLD",
                        f"추세 필터 (종가 {bar['close']:,.0f} < {p.slowMa}봉 평균 {sma_slow:,.0f})")

    trend = "추세 위" if bar["close"] >= sma_slow else "추세 아래"
    return Decision("BUY",
                    f"RSI {prev_rsi:.1f}→{rsi:.1f} 로 {p.rsiBuy:.0f} 상향 돌파 ({trend})",
                    {"rule": "rsiCrossUp", "rsi": round(rsi, 1),
                     "prevRsi": round(prev_rsi, 1)})
