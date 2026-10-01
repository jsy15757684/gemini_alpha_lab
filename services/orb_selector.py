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
하나씩 훑는다(호출 간격 포함 종목당 약 1.35초 → 339종목 약 7.6분).

두 번 본다 (2026-10-01 · 사흘 실측으로 바꿨다 · 10-02 시각을 고쳤다)
  08:50     넓게 훑기   전 후보. 갭을 느슨하게(±looseGapPct) 보고 거래대금은 보지 않는다
                        → 점수 상위 preN(50) 종목의 '예비 목록'
  08:57:50  다시 거르기  예비 목록만 다시 조회해 원래 기준(갭 · 거래대금)으로 골라 topN
  예상체결은 08:50 무렵부터 나온다 (10-02: 08:40~08:47 에 338종목 모두 비었다).
  08:48 하나로 고르던 때는 예상 갭이 실제 시가와 ±3%p 씩 어긋나(성호전자 +2.8 →
  +0.76% · NC +5.3 → +8.4% · 원익홀딩스 +3.0 → +0.56%) 감시 종목 대부분이 09:05
  실제 갭 조건에서 바로 떨어졌다. 동시호가 주문은 09:00 직전에 몰리므로 08:58 값이
  시가에 훨씬 가깝다. 또 08:48 시작으로는 시간이 모자라 매일 57~59종목을 못 봤다.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

from services import krx
from services.namuh import NamuhError

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


@dataclass
class SelectParams:
    topN: int = 28
    # 예상 갭 범위. 09:05 에 실제 시초가 갭(OrbParams 2~5%)으로 다시 거르므로, 예상과
    # 실제의 차이를 감안해 그보다 조금 넓게 고른다. ORB 는 매수만 하므로 위로 뜨는 종목.
    gapMinPct: float = 1.5
    gapMaxPct: float = 6.0
    # 예상 거래대금 하한(억 원) — 호가가 얇으면 신호가 가짜다. 08:48~08:54 의 예상체결량은
    # 동시호가 주문이 09:00 직전에 몰려 전일 거래량의 1~7% 뿐이다(2026-09-29 실측). 처음 둔
    # 3억은 09:01 실제 체결량으로 잡은 값이라 189종목 중 1종목만 남았다 → 0.5억.
    minExpTurnoverEok: float = 0.5
    preN: int = 50                  # 예비 목록 크기 — 08:57:50 에 이만큼 다시 조회한다(약 70초)
    looseGapPct: float = 3.0        # 넓게 훑기에서는 갭을 이만큼 넓게 본다 (08:48 → 시가 어긋남 실측 ±3%p)

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
        if not (p.topN <= p.preN <= 60):
            raise ValueError(f"예비 목록은 감시 종목 수({p.topN}) 이상 60 이하여야 합니다 (08:58 재조회 시간).")
        if not (0 <= p.looseGapPct <= 10):
            raise ValueError("넓게 훑기 갭 여유는 0 ~ 10%p 입니다.")
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


def _sweep(acc, rows: List[Dict[str, Any]], budget: float, p: SelectParams, deadline: float,
           should_stop: Callable[[], bool], quote: Callable) -> Dict[str, Any]:
    """rows 를 순서대로 조회해 판정한다. deadline(유닉스 초)이 지나면 거기까지."""
    t0 = time.time()
    judged, errors, with_exp = [], 0, 0
    for c in rows:
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
        judged.append({"code": c["code"], "name": c["name"], "index": c.get("market") or c.get("index"), **j})
    passed = sorted((j for j in judged if j["ok"]), key=lambda j: -j["score"])
    # 서버 시계는 UTC 다 — 화면에 08:54 가 23:54 로 보였다
    return {"at": datetime.now(KST).strftime("%H:%M:%S"), "elapsedSec": round(time.time() - t0),
            "scanned": len(judged), "withExpected": with_exp, "passed": len(passed), "errors": errors,
            "truncated": len(judged) + errors < len(rows), "judged": judged,
            "chosen": passed[:p.topN], "params": p.to_dict(),
            "rejectedTop": sorted((j for j in judged if not j["ok"] and j["expPrice"] > 0),
                                  key=lambda j: -j["score"])[:10]}


def affordable(cands: List[Dict[str, Any]], budget: float) -> List[Dict[str, Any]]:
    # 전일 종가로 먼저 거른다 — 살 수 없는 종목에 호출을 쓰지 않는다. 시가총액 큰 순.
    pool = [c for c in cands if not c.get("prevClose") or c["prevClose"] * 1.01 <= budget]
    return sorted(pool, key=lambda c: -int(c.get("capEok") or 0))


def select(acc, cands: List[Dict[str, Any]], budget: float, p: SelectParams,
           deadline: float, should_stop: Callable[[], bool],
           quote: Callable = None) -> Dict[str, Any]:
    """후보를 한 번 훑어 고른다(08:50 넓게 훑기의 '엄격한' 결과도 이것이다).

    반환에 prelist 가 붙는다 — 다시 거르기에 쓸 예비 목록. 갭은 ±looseGapPct
    넓게, 거래대금은 보지 않고(이른 시각의 예상 체결량은 아직 작다), 점수 순 preN 개.
    예상체결을 하나도 못 받았으면(예상체결이 늦게 나온 날) 시가총액 상위 preN 개를
    예비 목록으로 둔다 — 다시 거르기에서 살아날 기회를 남긴다.
    """
    pool = affordable(cands, budget)
    res = _sweep(acc, pool, budget, p, deadline, should_stop, quote or krx.quote)
    loose = SelectParams(topN=p.topN, gapMinPct=p.gapMinPct - p.looseGapPct,
                         gapMaxPct=p.gapMaxPct + p.looseGapPct, minExpTurnoverEok=0.0,
                         preN=p.preN, looseGapPct=p.looseGapPct)
    pre = [j for j in res["judged"]
           if judge({"expPrice": j["expPrice"], "expChangePct": j["gap"], "expVolume": j["expVolume"],
                     "prevVolume": j["prevVolume"]}, budget, loose)["ok"]]
    pre.sort(key=lambda j: -j["score"])
    prelist = [{"code": j["code"], "name": j["name"], "index": j["index"], "score": j["score"],
                "gap": j["gap"]} for j in pre[:p.preN]]
    if not res["withExpected"]:
        prelist = [{"code": c["code"], "name": c["name"], "index": c.get("market"), "score": 0.0, "gap": None}
                   for c in pool[:p.preN]]
    res.update(candidates=len(cands), affordable=len(pool), prelist=prelist,
               prelistByCap=not res["withExpected"])
    res.pop("judged")
    return res


def refine(acc, prelist: List[Dict[str, Any]], budget: float, p: SelectParams, deadline: float,
           should_stop: Callable[[], bool], quote: Callable = None) -> Dict[str, Any]:
    """08:58 — 예비 목록만 다시 조회해 원래 기준으로 고른다. 예비 목록 점수 순으로 본다."""
    res = _sweep(acc, list(prelist), budget, p, deadline, should_stop, quote or krx.quote)
    res.pop("judged")
    res.update(candidates=len(prelist), affordable=len(prelist), refined=True)
    return res
