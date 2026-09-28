"""ORB 감시 종목 자동 선정 — 매일 장 전, 그날 '움직일 종목(stocks in play)' 을 고른다.

ORB 는 갭과 거래가 몰리는 종목에서 잘 맞는다(Zarattini & Aziz 도 매일 상대
거래량 상위 종목을 골라 쓴다). 매일 같은 대형주를 보면 이 취지와 어긋난다.

  후보    코스피200 ∪ 코스닥150 (종목 마스터 · 위험 종목 제외 · krx_master)
  재료    08:48 부터 후보마다 REST 현재가의 동시호가 예상체결(Output_2):
          예상체결가 · 예상 등락률(갭) · 예상체결량, 그리고 전일 거래량
  거르기  1주 값 > 종목당 자본 · 갭이 gapMin~gapMax 밖 · 예상 거래대금 < 최소
  순위    예상체결량 ÷ 전일 거래량 (장 전 RVOL) — 평소보다 동시호가에 몰린 종목
  결과    상위 topN(최대 28 = 실시간 구독 30 − 지수 ETF 2)

실시간 예상체결 채널(oa)은 연결당 30종목이라 350종목을 못 본다. 그래서 REST 로
하나씩 훑는다(1.1초 간격 → 약 6분). 08:55 에 실시간 감시를 붙여야 하므로
시가총액이 큰 종목부터 훑고, 시간이 모자라면 작은 종목을 남긴다.
"""

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

from services import krx
from services.namuh import NamuhError

logger = logging.getLogger(__name__)


@dataclass
class SelectParams:
    topN: int = 28
    gapMinPct: float = 1.0          # 예상 갭 하한 — ORB 는 매수만 하므로 위로 뜨는 종목
    gapMaxPct: float = 15.0         # 상한 — 상한가 근처 · 과열은 돌파 뒤 여유가 없다
    minExpTurnoverEok: float = 3.0  # 예상 거래대금 하한(억 원) — 호가가 얇으면 신호가 가짜다

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "SelectParams":
        d = d or {}
        p = cls()
        for k in asdict(p):
            if d.get(k) is not None:
                setattr(p, k, type(getattr(p, k))(d[k]))
        if not (1 <= p.topN <= krx.MAX_WATCH):
            raise ValueError(f"자동 선정 종목 수는 1 ~ {krx.MAX_WATCH} 입니다.")
        if not (-30 <= p.gapMinPct < p.gapMaxPct <= 30):
            raise ValueError("갭 범위가 올바르지 않습니다 (하한 < 상한, ±30% 안).")
        if p.minExpTurnoverEok < 0:
            raise ValueError("예상 거래대금 하한은 0 이상이어야 합니다.")
        return p

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def judge(q: Dict[str, Any], budget: float, p: SelectParams) -> Dict[str, Any]:
    """후보 하나. {ok, score, why, gap, expPrice, expVolume, prevVolume, turnoverEok}"""
    exp, gap, vol, prev = q.get("expPrice") or 0, q.get("expChangePct") or 0.0, q.get("expVolume") or 0, q.get("prevVolume") or 0
    out = {"gap": round(gap, 2), "expPrice": exp, "expVolume": vol, "prevVolume": prev,
           "turnoverEok": round(exp * vol / 1e8, 2), "score": 0.0, "ok": False, "why": ""}
    if exp <= 0 or vol <= 0:
        out["why"] = "예상체결 없음"
        return out
    out["score"] = round(vol / prev, 4) if prev else 0.0
    if exp * 1.01 > budget:
        out["why"] = f"1주 {exp:,}원 > 종목당 {budget:,.0f}원"
    elif not (p.gapMinPct <= gap <= p.gapMaxPct):
        out["why"] = f"갭 {gap:+.2f}% (범위 {p.gapMinPct:+g}~{p.gapMaxPct:+g}%)"
    elif out["turnoverEok"] < p.minExpTurnoverEok:
        out["why"] = f"예상 거래대금 {out['turnoverEok']:.1f}억 < {p.minExpTurnoverEok:g}억"
    elif not prev:
        out["why"] = "전일 거래량 없음"
    else:
        out["ok"] = True
    return out


def select(acc, cands: List[Dict[str, Any]], budget: float, p: SelectParams,
           deadline: float, should_stop: Callable[[], bool],
           quote: Callable = None) -> Dict[str, Any]:
    """후보를 훑어 고른다. deadline(유닉스 초)이 지나면 거기까지로 끝낸다."""
    quote = quote or krx.quote
    t0 = time.time()
    # 전일 종가로 먼저 거른다 — 살 수 없는 종목에 호출을 쓰지 않는다
    pool = [c for c in cands if not c.get("prevClose") or c["prevClose"] * 1.01 <= budget]
    pool.sort(key=lambda c: -int(c.get("capEok") or 0))
    judged, errors, with_exp = [], 0, 0
    for c in pool:
        if should_stop() or time.time() >= deadline:
            break
        try:
            q = quote(acc, c["code"])
        except (NamuhError, Exception) as e:
            errors += 1
            if errors <= 3:
                logger.warning(f"자동 선정 시세 실패 {c['code']}: {e}")
            continue
        j = judge(q, budget, p)
        if j["expPrice"] > 0:
            with_exp += 1
        judged.append({"code": c["code"], "name": c["name"], "index": c["market"], **j})
    passed = sorted((j for j in judged if j["ok"]), key=lambda j: -j["score"])
    chosen = passed[:p.topN]
    return {"at": time.strftime("%H:%M:%S"), "elapsedSec": round(time.time() - t0),
            "candidates": len(cands), "affordable": len(pool), "scanned": len(judged),
            "withExpected": with_exp, "passed": len(passed), "errors": errors,
            "truncated": len(judged) + errors < len(pool),
            "chosen": chosen, "params": p.to_dict(),
            "rejectedTop": sorted((j for j in judged if not j["ok"] and j["expPrice"] > 0),
                                  key=lambda j: -j["score"])[:10]}
