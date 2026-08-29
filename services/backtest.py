"""빗썸 원화 캔들 기반 백테스트.

실시간 봇과 **같은 strategy.decide()** 를 호출한다. 그래서 백테스트 결과가
실제 봇의 행동을 그대로 예측한다. (예전 구조는 봇과 백테스터의 판단 로직이
서로 달라서 백테스트가 아무것도 예측하지 못했다.)

체결 가정은 보수적으로 둔다:
  · 신호가 뜬 캔들의 '종가' 에 체결된다고 본다 (다음 봉 시가가 아님)
  · 매수/매도 양쪽에 수수료를 뺀다
  · 슬리피지는 반영하지 않는다 — 실전은 이보다 나쁠 수 있다
"""

import logging
from typing import Any, Dict, List

from services import bithumb
from services.strategy import Decision, Position, StrategyParams, compute_indicators, decide

logger = logging.getLogger(__name__)


def run(coin: str, interval: str = "1h", params: Dict[str, Any] = None,
        initial_krw: float = 1_000_000.0) -> Dict[str, Any]:
    p = StrategyParams.from_dict(params)
    candles = bithumb.get_candles(coin, interval, limit=200)
    bars = compute_indicators(candles, p)

    warmup = p.warmup()
    if len(bars) <= warmup + 2:
        raise bithumb.BithumbError(
            f"캔들이 부족합니다 (필요 {warmup + 3}개, 확보 {len(bars)}개). "
            f"이동평균 기간을 줄이거나 더 짧은 간격을 쓰세요.")

    cash = float(initial_krw)
    pos = Position()
    fee = p.feePct / 100.0

    trades: List[Dict[str, Any]] = []
    equity: List[Dict[str, Any]] = []
    entry_time = None
    entry_reason = ""

    for i in range(warmup, len(bars)):
        bar = bars[i]
        price = bar["close"]

        if pos.open and price > pos.peakPrice:
            pos.peakPrice = price

        d: Decision = decide(bars, i, price, pos, p)

        if d.action == "BUY" and not pos.open:
            invest = cash
            units = invest * (1 - fee) / price
            pos = Position(units=units, entryPrice=price, peakPrice=price)
            cash = 0.0
            entry_time, entry_reason = bar["time"], d.reason

        elif d.action == "SELL" and pos.open:
            proceeds = pos.units * price * (1 - fee)
            pnl = proceeds - (pos.units * pos.entryPrice)
            trades.append({
                "entryTime": entry_time, "exitTime": bar["time"],
                "entryPrice": round(pos.entryPrice, 2), "exitPrice": round(price, 2),
                "units": round(pos.units, 8),
                "returnPct": round((price - pos.entryPrice) / pos.entryPrice * 100, 2),
                "pnlKrw": round(pnl, 0),
                "entryReason": entry_reason, "exitReason": d.reason,
                "rule": d.detail.get("rule", ""),
                "result": "WIN" if pnl > 0 else "LOSS",
            })
            cash = proceeds
            pos = Position()

        equity.append({"time": bar["time"],
                       "value": round(cash + pos.units * price, 0),
                       "close": round(price, 2)})

    final_price = bars[-1]["close"]
    final_value = cash + pos.units * final_price

    # 벤치마크: 같은 구간을 그냥 들고 있었을 때 (수수료 1회 왕복 반영)
    bench_entry = bars[warmup]["close"]
    bench_units = initial_krw * (1 - fee) / bench_entry
    bench_value = bench_units * final_price * (1 - fee)

    wins = [t for t in trades if t["pnlKrw"] > 0]
    losses = [t for t in trades if t["pnlKrw"] <= 0]
    gross_profit = sum(t["pnlKrw"] for t in wins)
    gross_loss = abs(sum(t["pnlKrw"] for t in losses))

    peak = -1.0
    mdd = 0.0
    for e in equity:
        peak = max(peak, e["value"])
        if peak > 0:
            mdd = max(mdd, (peak - e["value"]) / peak * 100)

    total_return = (final_value - initial_krw) / initial_krw * 100
    bench_return = (bench_value - initial_krw) / initial_krw * 100

    return {
        "coin": bithumb.normalize_coin(coin),
        "interval": interval,
        "params": p.to_dict(),
        "currency": "KRW",
        "initialKrw": round(initial_krw, 0),
        "finalKrw": round(final_value, 0),
        "totalReturnPct": round(total_return, 2),
        "benchmarkReturnPct": round(bench_return, 2),
        "alphaPct": round(total_return - bench_return, 2),
        "maxDrawdownPct": round(mdd, 2),
        "totalTrades": len(trades),
        "winningTrades": len(wins),
        "losingTrades": len(losses),
        "winRatePct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profitFactor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "openPositionAtEnd": pos.open,
        "periodFrom": bars[warmup]["time"],
        "periodTo": bars[-1]["time"],
        "candleCount": len(bars),
        "trades": trades[-50:],
        "equityCurve": equity,
        "dataSource": "bithumb-candles",
        "note": ("신호 발생 캔들의 종가 체결·수수료 반영·슬리피지 미반영 가정입니다. "
                 "실전 성과는 이보다 나쁠 수 있습니다."),
    }
