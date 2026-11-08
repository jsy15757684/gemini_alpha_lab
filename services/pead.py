"""국내 주식 [200일선 추세 방패 + PEAD 실적 모멘텀 + 외인·기관 수급] 스윙 엔진 (모의 전용).

**실주문은 없다.** 실계좌 봇(bots.json)이나 단타 스캘퍼(scalper.json)와 완전히 분리된
별도 장부(data/pead.json)에만 가상 포지션과 파킹 이자를 기록한다.

전략 4단계 (한국 시장 맞춤형 하이브리드 설계)
  1단계 [시장 & 종목 200일선 방패]
    · 코스피 / 코스닥 지수가 200일 이동평균선 아래(역배열 하락장)면 해당 시장 신규 매수 중단
    · 남는 슬롯 현금은 무위험 금리형 ETF(KODEX KOFR금리액티브 423160)에 파킹해 일복리 이자 적립
    · 개별 종목도 현재가 > 200일선(정배열)이어야 하며, 발표 전 20일간 이미 과열(+25% 초과)된
      '뉴스에 팔아라(선반영)' 종목은 제외
  2단계 [실적 서프라이즈 · 공시 판독 (PEAD)]
    · 분기 영업이익 흑자 필수 (영업적자인데 일회성 처분이익으로 당기순이익만 흑자인 착시 차단)
    · 영업이익 YoY +25% 이상(또는 흑자전환 / QoQ +30% 이상) & 매출 성장, 또는 최근 잠정실적·수주 공시 확인
    · 유상증자 · 전환사채(CB) · 신주인수권부사채(BW) 등 국장 악성 희석 공시 종목 자동 배제
  3단계 [국장 '설거지 방지' 진짜 수급 필터]
    · 거래량: 직전 20일 평균 거래량 대비 300%(3.0배) 이상 폭발
    · 캔들: 시가보다 높은 양봉 마감(+4% ~ +23%) & 윗꼬리 비율 38% 이하 (피뢰침 고점 매도 차단)
    · 수급: 당일 외국인 또는 기관 순매수 유입 (합산 순매수 > 0, 개인 단독 추격 매수 배제)
  4단계 [슬롯 포트폴리오 & 기계적 손절·익절]
    · 기본 5슬롯(종목당 시드의 20%) · 반자동(1클릭 승인) 또는 100% 자동 모의매수 선택
    · 손절(STOP_LOSS): 실적 장대양봉의 '시가(수급 주체 평단)' 이탈 시 지체 없이 전량 손절
    · 익절(TRAIL_MA20): 종가가 20일 이동평균선을 이탈할 때까지 1~3개월 PEAD 추세를 끝까지 추종
"""

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from services import botstore, krx, krx_master
from services.namuh import NamuhError
from services.scalper import net_pct, walk_buy, walk_sell

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SCAN_AT = (15, 10, 0)         # 정규장 마감 직전(15:10) 일봉 윤곽·거래량·잠정수급 확인
CLOSE_CHECK_AT = (15, 18, 0)  # 15:18 자동모드 진입 및 20일선 종가 이탈 청산 점검
AFTER_HOURS_AT = (16, 35, 0)  # 장 마감 후(16:35) 잠정실적 공시 스윕 → 익일 감시 대기열
OPEN_AT = (9, 5, 0)
CLOSE_AT = (15, 30, 0)

KOFR_CODE = "423160"          # KODEX KOFR금리액티브(합성) — 유휴 현금 파킹 기준
MAX_TRADES = 2000
MAX_LOGS = 200

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

EARNINGS_KEYWORDS = (
    "영업(잠정)실적", "잠정실적", "매출액또는손익구조", "분기보고서",
    "반기보고서", "사업보고서", "결산실적",
)
CATALYST_KEYWORDS = ("단일판매", "공급계약", "수주")
TOXIC_KEYWORDS = (
    "유상증자", "전환사채", "신주인수권부사채", "교환사채",
    "감자", "상장적격성", "거래정지", "횡령", "배임", "부도",
)


def _make_store() -> botstore.JsonStore:
    return botstore.JsonStore("pead.json", "국내 200일선·PEAD(모의) 상태")


store = _make_store()


def _active_store() -> botstore.JsonStore:
    """테스트·격리 실행에서 botstore._DATA_DIR 가 바뀐 경우 그 경로를 따른다."""
    global store
    want_dir = os.path.abspath(botstore._DATA_DIR)
    cur_dir = os.path.abspath(os.path.dirname(store.path))
    if cur_dir != want_dir:
        store = botstore.JsonStore("pead.json", "국내 200일선·PEAD(모의) 상태")
    return store


@dataclass
class PeadParams:
    seedKrw: float = 10_000_000       # 총 가상 운용 시드 (원)
    maxSlots: int = 5                 # 최대 슬롯 수 (5종목 = 슬롯당 20%)
    candidateMultiplier: int = 2      # 추천 기업 조회 배수 (남은 빈 슬롯 수 × 2배수만 엄선 노출)
    approvalMode: str = "SEMI"        # "SEMI"(90% 자동 + 1클릭 승인) | "AUTO"(100% 자동 모의매수)
    parkingRatePct: float = 3.25      # 유휴 현금 KOFR 파킹 연환산 수익률 (%)
    indexSmaPeriod: int = 60          # 시장·종목 기준 이동평균선 (60일 분기 수급선 기본 · 60/120/200일 선택)
    maMigrated60: bool = True         # 기존 200일선 기본값 저장분을 60일 수급선으로 1회 자동 전환
    indexBearBufferPct: float = 0.0   # 지수 기준선 버퍼 (0.0 = 기준선 위만 허용, -2.0 = -2%까지 허용)
    minOpYoyPct: float = 25.0         # 영업이익 YoY 최소 성장률 (%, 흑자전환은 자동 통과)
    minRevYoyPct: float = 10.0        # 매출액 YoY 최소 성장률 (%)
    minVolRatio: float = 3.0          # 20일 평균 대비 최소 거래량 배수 (3.0 = 300%)
    minDayGainPct: float = 4.0        # 돌파 당일 최소 상승률 (%)
    maxDayGainPct: float = 23.0       # 상한가 추격 방지 상한 상승률 (%)
    maxWickRatio: float = 0.38        # 윗꼬리 상한 비율 ((고가-종가)/(고가-저가), 설거지 피뢰침 차단)
    maxPrior20dGainPct: float = 25.0  # 발표 전 20거래일 선반영 상승률 상한 (%)
    requireSmartMoney: bool = True    # 외국인 또는 기관 순매수 필수 여부
    exitMaPeriod: int = 20            # 추세 익절 이동평균선 (20일선 종가 이탈)
    maxHoldDays: int = 60             # 최대 보유 거래일 (60거래일 ≈ 3개월)
    costPct: float = krx.SELL_COST_PCT
    lookbackDays: int = 7             # 최근 실적/수주 공시 탐색 일수

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "PeadParams":
        p = cls()
        raw = d or {}
        for k, v in raw.items():
            if hasattr(p, k) and v is not None:
                target_type = type(getattr(p, k))
                if target_type is bool:
                    setattr(p, k, bool(v))
                else:
                    setattr(p, k, target_type(v))
        if raw and "maMigrated60" not in raw and int(raw.get("indexSmaPeriod") or 200) == 200:
            p.indexSmaPeriod = 60
            p.maMigrated60 = True
        p.indexSmaPeriod = max(20, min(240, int(p.indexSmaPeriod)))
        p.maxSlots = max(1, min(10, int(p.maxSlots)))
        p.candidateMultiplier = max(1, min(5, int(p.candidateMultiplier)))
        p.seedKrw = max(500_000.0, float(p.seedKrw))
        p.approvalMode = "AUTO" if str(p.approvalMode).upper() == "AUTO" else "SEMI"
        p.minVolRatio = max(1.0, min(20.0, float(p.minVolRatio)))
        p.exitMaPeriod = max(5, min(120, int(p.exitMaPeriod)))
        p.maxHoldDays = max(5, min(250, int(p.maxHoldDays)))
        return p

    def slot_budget(self) -> float:
        return self.seedKrw / max(1, self.maxSlots)


# ───────────────────── 외부 데이터 수집 (테스트에서 교체 가능) ─────────────────────


def _parse_num(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s in ("-", "N/A", "nan", "적자전환", "적자지속", "흑자전환"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_daily_bars(symbol: str, count: int = 230) -> List[Dict[str, Any]]:
    """네이버 금융 fchart 일봉 조회 (수정주가 · 지수 KOSPI/KOSDAQ 및 개별 종목 지원)."""
    url = (
        f"https://fchart.stock.naver.com/sise.nhn"
        f"?symbol={symbol}&timeframe=day&count={count}&requestType=0"
    )
    res = requests.get(url, headers=HEADERS, timeout=7)
    res.raise_for_status()
    items = re.findall(r'data="([^"]+)"', res.text)
    bars: List[Dict[str, Any]] = []
    for raw in items:
        parts = raw.split("|")
        if len(parts) < 6:
            continue
        dt_raw = parts[0].strip()
        try:
            c = float(parts[4])
            if c <= 0:
                continue
            o = float(parts[1]) or c
            h = float(parts[2]) or c
            low = float(parts[3]) or c
            vol = float(parts[5] or 0)
        except ValueError:
            continue
        dt_fmt = f"{dt_raw[:4]}-{dt_raw[4:6]}-{dt_raw[6:8]}" if len(dt_raw) == 8 else dt_raw
        bars.append({"date": dt_fmt, "open": o, "high": max(h, o, c), "low": min(low, o, c),
                     "close": c, "volume": max(0.0, vol)})
    return bars


def fetch_investor_trend(code: str) -> Dict[str, Any]:
    """네이버 모바일 증권 투자자별 매매동향 — 당일 및 최근 3거래일 외국인·기관 순매수(주)."""
    url = f"https://m.stock.naver.com/api/stock/{code}/trend"
    res = requests.get(url, headers=HEADERS, timeout=6)
    res.raise_for_status()
    rows = res.json()
    if not isinstance(rows, list) or not rows:
        return {"bizdate": None, "foreignNet": 0, "instNet": 0, "retailNet": 0, "smartNet3d": 0}
    top = rows[0]
    f0 = int(_parse_num(top.get("foreignerPureBuyQuant")) or 0)
    i0 = int(_parse_num(top.get("organPureBuyQuant")) or 0)
    r0 = int(_parse_num(top.get("individualPureBuyQuant")) or 0)
    smart_3d = 0
    for row in rows[:3]:
        smart_3d += int(_parse_num(row.get("foreignerPureBuyQuant")) or 0)
        smart_3d += int(_parse_num(row.get("organPureBuyQuant")) or 0)
    raw_dt = str(top.get("bizdate") or "")
    dt_fmt = f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]}" if len(raw_dt) == 8 else raw_dt
    return {
        "bizdate": dt_fmt,
        "foreignNet": f0,
        "instNet": i0,
        "retailNet": r0,
        "smartNet": f0 + i0,
        "smartNet3d": smart_3d,
        "foreignHoldRatio": top.get("foreignerHoldRatio") or "",
    }


def fetch_financial_surprise(code: str) -> Dict[str, Any]:
    """분기 실적(매출액·영업이익·당기순이익) 및 컨센서스 성장률 판독.

    일회성 영업외이익(부동산 매각 등)으로 당기순이익만 흑자이고 영업이익이 적자인 종목은
    oneOffTrap=True 로 표시해 가짜 서프라이즈를 걸러낸다.
    """
    url = f"https://m.stock.naver.com/api/stock/{code}/finance/quarter"
    res = requests.get(url, headers=HEADERS, timeout=6)
    res.raise_for_status()
    data = res.json() or {}
    fi = data.get("financeInfo") or {}
    titles = fi.get("trTitleList") or []
    row_list = fi.get("rowList") or []

    by_title: Dict[str, Dict[str, Optional[float]]] = {}
    for row in row_list:
        t = str(row.get("title") or "").strip()
        cols = row.get("columns") or {}
        by_title[t] = {k: _parse_num((v or {}).get("value")) for k, v in cols.items()}

    rev_map = by_title.get("매출액") or by_title.get("영업수익") or by_title.get("순영업수익") or {}
    op_map = by_title.get("영업이익") or {}
    net_map = by_title.get("당기순이익") or by_title.get("지배주주순이익") or {}

    actual_keys = [x["key"] for x in titles if x.get("isConsensus") != "Y" and x.get("key")]
    cons_keys = [x["key"] for x in titles if x.get("isConsensus") == "Y" and x.get("key")]

    # 가장 최근 확정(또는 잠정 발표 반영된) 분기 찾기
    latest_k = None
    for k in reversed(actual_keys):
        if op_map.get(k) is not None or rev_map.get(k) is not None:
            latest_k = k
            break

    if not latest_k:
        return {"available": False, "reason": "분기 재무 데이터 없음"}

    op_now = op_map.get(latest_k)
    rev_now = rev_map.get(latest_k)
    net_now = net_map.get(latest_k)

    # 전년 동기 키 계산 (예: "202606" -> "202506")
    yoy_k = f"{int(latest_k[:4]) - 1}{latest_k[4:]}" if len(latest_k) == 6 and latest_k.isdigit() else ""
    idx = actual_keys.index(latest_k)
    qoq_k = actual_keys[idx - 1] if idx > 0 else ""

    op_yoy_base = op_map.get(yoy_k)
    rev_yoy_base = rev_map.get(yoy_k)
    op_qoq_base = op_map.get(qoq_k)

    turnaround = False
    op_yoy_pct: Optional[float] = None
    if op_now is not None and op_yoy_base is not None:
        if op_yoy_base <= 0 and op_now > 0:
            turnaround = True
            op_yoy_pct = 100.0
        elif op_yoy_base > 0:
            op_yoy_pct = round((op_now - op_yoy_base) / abs(op_yoy_base) * 100.0, 1)

    rev_yoy_pct: Optional[float] = None
    if rev_now is not None and rev_yoy_base is not None and rev_yoy_base > 0:
        rev_yoy_pct = round((rev_now - rev_yoy_base) / abs(rev_yoy_base) * 100.0, 1)

    op_qoq_pct: Optional[float] = None
    if op_now is not None and op_qoq_base is not None:
        if op_qoq_base <= 0 and op_now > 0:
            turnaround = True
            op_qoq_pct = 100.0
        elif op_qoq_base > 0:
            op_qoq_pct = round((op_now - op_qoq_base) / abs(op_qoq_base) * 100.0, 1)

    # 다음 분기 컨센서스 추이 (있으면 참고 지표로 함께 제공)
    cons_k = cons_keys[0] if cons_keys else ""
    cons_op = op_map.get(cons_k) if cons_k else None
    cons_yoy_pct: Optional[float] = None
    if cons_k and cons_op is not None and len(cons_k) == 6 and cons_k.isdigit():
        cons_base_k = f"{int(cons_k[:4]) - 1}{cons_k[4:]}"
        cons_base = op_map.get(cons_base_k)
        if cons_base is not None and cons_base > 0:
            cons_yoy_pct = round((cons_op - cons_base) / abs(cons_base) * 100.0, 1)
        elif cons_base is not None and cons_base <= 0 and cons_op > 0:
            cons_yoy_pct = 100.0

    one_off_trap = bool(op_now is not None and op_now <= 0 and (net_now or 0) > 0)
    q_label = f"{latest_k[:4]}.{latest_k[4:]}" if len(latest_k) == 6 else latest_k

    return {
        "available": True,
        "quarter": q_label,
        "opProfitEok": op_now,
        "revEok": rev_now,
        "netProfitEok": net_now,
        "opYoyPct": op_yoy_pct,
        "revYoyPct": rev_yoy_pct,
        "opQoqPct": op_qoq_pct,
        "turnaround": turnaround,
        "consQuarter": f"{cons_k[:4]}.{cons_k[4:]}" if len(cons_k) == 6 else cons_k,
        "consOpEok": cons_op,
        "consOpYoyPct": cons_yoy_pct,
        "oneOffTrap": one_off_trap,
    }


def fetch_recent_disclosures(code: str, lookback_days: int = 7, now: Optional[datetime] = None) -> Dict[str, Any]:
    """네이버 종목 공시(DART/KRX 연동) + 선택적 DART OpenAPI 로 최근 실적·수주·악재 공시를 확인한다."""
    ref = now or datetime.now(KST)
    cutoff = (ref - timedelta(days=max(1, lookback_days))).strftime("%Y-%m-%d")
    url = f"https://m.stock.naver.com/api/stock/{code}/disclosure"
    items: List[Dict[str, Any]] = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            raw_list = res.json()
            if isinstance(raw_list, list):
                items = raw_list
    except Exception as e:
        logger.debug(f"공시 조회 실패({code}): {e}")

    earnings_hit: Optional[Dict[str, Any]] = None
    catalyst_hit: Optional[Dict[str, Any]] = None
    toxic_hit: Optional[str] = None

    for it in items[:25]:
        dt_str = str(it.get("datetime") or "")
        date_part = dt_str[:10]
        if date_part and date_part < cutoff:
            continue
        title = str(it.get("title") or "").strip()
        time_part = dt_str[11:16] if len(dt_str) >= 16 else ""
        after_hours = bool(time_part and time_part >= "15:30")

        if any(bad in title for bad in TOXIC_KEYWORDS):
            toxic_hit = title
            break
        if not earnings_hit and any(kw in title for kw in EARNINGS_KEYWORDS):
            earnings_hit = {"title": title, "date": date_part, "time": time_part, "afterHours": after_hours}
        elif not catalyst_hit and any(kw in title for kw in CATALYST_KEYWORDS):
            catalyst_hit = {"title": title, "date": date_part, "time": time_part, "afterHours": after_hours}

    return {
        "earnings": earnings_hit,
        "catalyst": catalyst_hit,
        "toxic": toxic_hit,
    }


def fetch_dart_today_earnings(now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """환경변수 DART_API_KEY 가 설정돼 있으면 오늘/최근 DART 공정공시(잠정실적) 목록을 한 번에 가져온다."""
    api_key = (os.getenv("DART_API_KEY") or "").strip()
    if not api_key:
        return {}
    ref = now or datetime.now(KST)
    bgn = (ref - timedelta(days=5)).strftime("%Y%m%d")
    end = ref.strftime("%Y%m%d")
    out: Dict[str, Dict[str, Any]] = {}
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": api_key, "bgn_de": bgn, "end_de": end, "pblntf_ty": "I", "page_count": "100"},
            timeout=8,
        )
        data = res.json() if res.ok else {}
        for row in data.get("list") or []:
            sc = str(row.get("stock_code") or "").strip()
            nm = str(row.get("report_nm") or "").strip()
            rcept_dt = str(row.get("rcept_dt") or "")
            if len(sc) == 6 and any(kw in nm for kw in EARNINGS_KEYWORDS):
                dt_fmt = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}" if len(rcept_dt) == 8 else rcept_dt
                out.setdefault(sc, {"title": nm, "date": dt_fmt, "time": "", "afterHours": False, "source": "DART"})
    except Exception as e:
        logger.warning(f"DART 잠정실적 목록 조회 실패: {e}")
    return out


# ───────────────────── 지표 계산 & 4단계 전략 판정 ─────────────────────


def analyze_bars(bars: List[Dict[str, Any]], sma_long: int = 60, sma_exit: int = 20) -> Optional[Dict[str, Any]]:
    """일봉 배열에서 기준 이평선(기본 60일 수급선) · 20일선 · 20일 평균거래량 배수 · 윗꼬리 비율 · 사전 20일 등락률을 계산한다."""
    need = max(sma_long, sma_exit + 22, 25)
    if len(bars) < need:
        return None
    last = bars[-1]
    prev = bars[-2]
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]

    price = last["close"]
    prev_close = prev["close"]
    if price <= 0 or prev_close <= 0:
        return None

    sma200 = sum(closes[-sma_long:]) / float(sma_long)
    sma20 = sum(closes[-sma_exit:]) / float(sma_exit)
    prior_vols = vols[-21:-1]
    vol_avg20 = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    vol_ratio = (last["volume"] / vol_avg20) if vol_avg20 > 0 else 0.0

    day_gain_pct = (price / prev_close - 1.0) * 100.0
    base_20d = closes[-22] if len(closes) >= 22 and closes[-22] > 0 else prev_close
    prior_20d_gain_pct = (prev_close / base_20d - 1.0) * 100.0

    candle_range = last["high"] - last["low"]
    upper_wick_ratio = ((last["high"] - price) / candle_range) if candle_range > 0 else 0.0

    return {
        "date": last["date"],
        "price": price,
        "open": last["open"],
        "high": last["high"],
        "low": last["low"],
        "prevClose": prev_close,
        "volume": last["volume"],
        "sma200": round(sma200, 2),
        "sma200DiffPct": round((price / sma200 - 1.0) * 100.0, 2) if sma200 > 0 else 0.0,
        "above200": price >= sma200,
        "sma20": round(sma20, 2),
        "sma20DiffPct": round((price / sma20 - 1.0) * 100.0, 2) if sma20 > 0 else 0.0,
        "above20": price >= sma20,
        "volAvg20": round(vol_avg20, 1),
        "volRatio": round(vol_ratio, 2),
        "dayGainPct": round(day_gain_pct, 2),
        "prior20dGainPct": round(prior_20d_gain_pct, 2),
        "upperWickRatio": round(upper_wick_ratio, 3),
        "isBullCandle": price > last["open"],
    }


def compute_market_regime(params: PeadParams) -> Dict[str, Any]:
    """코스피 · 코스닥 기준 이동평균선(기본 60일 수급선) 국면 및 KOFR 파킹 ETF 시세를 계산한다."""
    n_ma = int(params.indexSmaPeriod or 60)
    out: Dict[str, Any] = {
        "updatedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "smaPeriod": n_ma,
        "kospi": None,
        "kosdaq": None,
        "kofr": None,
        "gate": "UNKNOWN",
        "gateLabel": "지수 확인 전",
        "degraded": False,
    }
    errors = []
    for key, sym, label in (("kospi", "KOSPI", "코스피"), ("kosdaq", "KOSDAQ", "코스닥")):
        try:
            bars = fetch_daily_bars(sym, max(120, n_ma + 15))
            if len(bars) < n_ma:
                raise ValueError(f"{label} 일봉 부족 ({len(bars)}개)")
            closes = [b["close"] for b in bars]
            px = closes[-1]
            prev_px = closes[-2] if len(closes) >= 2 else px
            sma = sum(closes[-n_ma:]) / float(n_ma)
            diff_pct = (px / sma - 1.0) * 100.0 if sma > 0 else 0.0
            bull = diff_pct >= params.indexBearBufferPct
            out[key] = {
                "label": label,
                "date": bars[-1]["date"],
                "price": round(px, 2),
                "changePct": round((px / prev_px - 1.0) * 100.0, 2) if prev_px > 0 else 0.0,
                "sma200": round(sma, 2),
                "smaPeriod": n_ma,
                "diffPct": round(diff_pct, 2),
                "bull": bull,
            }
        except Exception as e:
            errors.append(f"{label}: {e}")

    try:
        kbars = fetch_daily_bars(KOFR_CODE, 10)
        if kbars:
            kp = kbars[-1]["close"]
            kprev = kbars[-2]["close"] if len(kbars) >= 2 else kp
            out["kofr"] = {
                "code": KOFR_CODE,
                "name": "KODEX KOFR금리액티브(합성)",
                "price": int(round(kp)),
                "dailyPct": round((kp / kprev - 1.0) * 100.0, 4) if kprev > 0 else 0.0,
            }
    except Exception:
        pass

    kp_bull = (out["kospi"] or {}).get("bull")
    kq_bull = (out["kosdaq"] or {}).get("bull")
    if kp_bull is None and kq_bull is None:
        out["gate"] = "UNKNOWN"
        out["gateLabel"] = "⚠️ 지수 수신 실패 (신규 진입 보류)"
        out["degraded"] = True
        out["error"] = " · ".join(errors)
    elif kp_bull and kq_bull:
        out["gate"] = "BULL"
        out["gateLabel"] = f"🟢 양대 시장 {n_ma}일선 상회 (전면 강세 · 진입 허용)"
    elif kp_bull or kq_bull:
        live_mkt = "코스피" if kp_bull else "코스닥"
        dead_mkt = "코스닥" if kp_bull else "코스피"
        out["gate"] = "SPLIT"
        out["gateLabel"] = f"🟡 차별화 장세 ({live_mkt} {n_ma}일선 위 진입 허용 · {dead_mkt} 현금 파킹)"
    else:
        out["gate"] = "BEAR"
        out["gateLabel"] = f"🛡️ 양대 시장 {n_ma}일선 하회 (하락장 방어 · 전 슬롯 KOFR 파킹)"
    return out


def market_allows_entry(regime: Dict[str, Any], market: str) -> Tuple[bool, str]:
    """종목 소속 시장(kospi/kosdaq)의 기준 이평선 신호등이 초록불인지 판정한다."""
    mkey = "kosdaq" if str(market).lower() == "kosdaq" else "kospi"
    info = regime.get(mkey)
    n_ma = int((info or {}).get("smaPeriod") or regime.get("smaPeriod") or 60)
    if not info:
        return False, f"{mkey.upper()} {n_ma}일선 지표 미수신 (보수적 진입 보류)"
    if not info.get("bull"):
        return False, f"{info['label']} 지수 {n_ma}일선 하회 ({info['diffPct']:+.2f}%) — 하락장 방어"
    return True, f"{info['label']} {n_ma}일선 상회 ({info['diffPct']:+.2f}%)"


def compute_stop_price(open_px: float, entry_px: float) -> float:
    """실적 돌파 양봉의 시가를 기본 손절선으로 잡되, 손절폭이 너무 좁거나(-2% 미만) 과도(-10% 초과)하면 보정한다."""
    if entry_px <= 0:
        return 0.0
    raw = open_px if open_px > 0 else entry_px * 0.94
    min_stop = entry_px * 0.90   # 최대 손절폭 -10% 캡
    max_stop = entry_px * 0.975  # 최소 여유폭 -2.5%
    return float(round(max(min_stop, min(max_stop, raw))))


def evaluate_candidate(
    code: str,
    name: str,
    market: str,
    tech: Dict[str, Any],
    trend: Dict[str, Any],
    fin: Dict[str, Any],
    disc: Dict[str, Any],
    regime: Dict[str, Any],
    params: PeadParams,
) -> Dict[str, Any]:
    """단일 종목에 대해 4단계 [시장/종목 기준이평선(60MA) -> PEAD 실적 -> 거래량/양봉/수급] 통과 여부와 점수를 매긴다."""
    reasons: List[str] = []
    fails: List[str] = []
    n_ma = int(params.indexSmaPeriod or 60)

    # 1) 시장 기준선 신호등
    mkt_ok, mkt_msg = market_allows_entry(regime, market)
    if mkt_ok:
        reasons.append(mkt_msg)
    else:
        fails.append(mkt_msg)

    # 2) 개별 종목 기준선(기본 60일 수급선) 정배열 & 사전 과열(선반영) 체크
    above_200 = bool(tech.get("above200"))
    if above_200:
        reasons.append(f"주가 {n_ma}일선 위 ({tech['sma200DiffPct']:+.1f}%)")
    else:
        fails.append(f"주가 {n_ma}일선 아래 ({tech['sma200DiffPct']:+.1f}%)")

    prior_ok = tech["prior20dGainPct"] <= params.maxPrior20dGainPct
    if not prior_ok:
        fails.append(f"발표 전 20일 과열 (+{tech['prior20dGainPct']:.1f}% > +{params.maxPrior20dGainPct:.0f}% 선반영 위험)")

    # 3) 악재 공시 차단 & 실적 서프라이즈(PEAD) 판독
    toxic = disc.get("toxic")
    if toxic:
        fails.append(f"희석·위험 공시 감지 ({toxic[:28]})")

    earn_disc = disc.get("earnings")
    cat_disc = disc.get("catalyst")

    op_eok = fin.get("opProfitEok")
    op_yoy = fin.get("opYoyPct")
    rev_yoy = fin.get("revYoyPct")
    op_qoq = fin.get("opQoqPct")
    cons_yoy = fin.get("consOpYoyPct")
    turnaround = bool(fin.get("turnaround"))
    one_off = bool(fin.get("oneOffTrap"))

    pead_ok = False
    pead_badge = "실적 확인 중"
    if one_off:
        fails.append("영업이익 적자 · 당기순이익만 흑자 (일회성 영업외이익 착시 의심)")
        pead_badge = "⚠️ 일회성 순이익 의심"
    elif op_eok is not None and op_eok <= 0:
        fails.append(f"최근 분기({fin.get('quarter')}) 영업손실 ({op_eok:,.0f}억)")
        pead_badge = f"영업적자 ({op_eok:,.0f}억)"
    else:
        # 정량 분기 실적 성장성 확인
        strong_yoy = turnaround or (op_yoy is not None and op_yoy >= params.minOpYoyPct)
        strong_qoq = (op_qoq is not None and op_qoq >= max(30.0, params.minOpYoyPct))
        strong_cons = (cons_yoy is not None and cons_yoy >= params.minOpYoyPct and (fin.get("consOpEok") or 0) > 0)
        rev_ok = (rev_yoy is None) or (rev_yoy >= params.minRevYoyPct) or turnaround

        if turnaround:
            pead_ok = True
            pead_badge = f"{fin.get('quarter')} 영업이익 흑자전환 ({op_eok:,.0f}억)"
        elif strong_yoy and rev_ok:
            pead_ok = True
            rev_txt = f" · 매출 {rev_yoy:+.0f}%" if rev_yoy is not None else ""
            pead_badge = f"{fin.get('quarter')} 영업이익 YoY {op_yoy:+.1f}%{rev_txt}"
        elif strong_qoq and rev_ok:
            pead_ok = True
            pead_badge = f"{fin.get('quarter')} 영업이익 QoQ {op_qoq:+.1f}%"
        elif earn_disc and (op_eok is None or op_eok > 0):
            pead_ok = True
            pead_badge = f"실적공시 포착 ({earn_disc['date'][5:]})"
        elif cat_disc and strong_cons:
            pead_ok = True
            pead_badge = f"수주공시 + 컨센 YoY {cons_yoy:+.0f}%"
        else:
            yoy_txt = f"영업이익 YoY {op_yoy:+.1f}%" if op_yoy is not None else "실적 증가율 미달"
            pead_badge = yoy_txt
            fails.append(f"실적 모멘텀 기준 미달 ({yoy_txt}, 기준 +{params.minOpYoyPct:.0f}%)")

    if earn_disc:
        reasons.append(f"공시: {earn_disc['title'][:26]}")
    elif cat_disc:
        reasons.append(f"수주: {cat_disc['title'][:26]}")
    if pead_ok:
        reasons.append(pead_badge)

    # 4) 거래량 폭발 & 장대양봉(윗꼬리 설거지 방지) & 외인·기관 수급
    vol_ok = tech["volRatio"] >= params.minVolRatio
    if vol_ok:
        reasons.append(f"거래량 {tech['volRatio']:.1f}배 폭발")
    else:
        fails.append(f"거래량 {tech['volRatio']:.1f}배 (기준 {params.minVolRatio:.1f}배 미달)")

    gain = tech["dayGainPct"]
    candle_ok = (
        tech["isBullCandle"]
        and params.minDayGainPct <= gain <= params.maxDayGainPct
        and tech["upperWickRatio"] <= params.maxWickRatio
    )
    if candle_ok:
        reasons.append(f"양봉 +{gain:.1f}% (윗꼬리 {int(tech['upperWickRatio'] * 100)}%)")
    else:
        if not tech["isBullCandle"]:
            fails.append("음봉/도지 마감 (시가 위 양봉 아님)")
        elif gain < params.minDayGainPct:
            fails.append(f"당일 등락률 {gain:+.1f}% (기준 +{params.minDayGainPct:.1f}% 미달)")
        elif gain > params.maxDayGainPct:
            fails.append(f"당일 과열 +{gain:.1f}% (상한가·과열 추격 제한 +{params.maxDayGainPct:.0f}%)")
        else:
            fails.append(f"윗꼬리 과다 {int(tech['upperWickRatio'] * 100)}% (고점 매도 설거지 의심)")

    f_net = int(trend.get("foreignNet") or 0)
    i_net = int(trend.get("instNet") or 0)
    smart_sum = f_net + i_net
    if not params.requireSmartMoney:
        flow_ok = True
    else:
        flow_ok = (f_net > 0 or i_net > 0) and smart_sum > 0

    if flow_ok:
        reasons.append(f"수급 합격 (외인 {f_net:+,}주 · 기관 {i_net:+,}주)")
    else:
        fails.append(f"외인·기관 순매수 부재 (외인 {f_net:+,}주 · 기관 {i_net:+,}주)")

    passed = bool(
        mkt_ok and above_200 and prior_ok and (not toxic)
        and pead_ok and vol_ok and candle_ok and flow_ok
    )

    # 종합 스코어 (100점 만점 — 아깝게 탈락한 상위 관심주 정렬용)
    score = 0.0
    if mkt_ok:
        score += 12.0
    if above_200:
        score += 18.0
    if prior_ok and not toxic:
        score += 10.0
    if pead_ok:
        score += 25.0 + min(10.0, max(0.0, (op_yoy or 30.0) / 20.0))
    if vol_ok:
        score += 15.0 + min(5.0, max(0.0, tech["volRatio"] - params.minVolRatio))
    else:
        score += min(10.0, tech["volRatio"] / max(1.0, params.minVolRatio) * 10.0)
    if candle_ok:
        score += 10.0
    if flow_ok:
        score += 10.0

    stop_px = compute_stop_price(tech["open"], tech["price"])
    stop_pct = round((stop_px / tech["price"] - 1.0) * 100.0, 2) if tech["price"] > 0 else 0.0

    return {
        "code": code,
        "name": name,
        "market": market,
        "date": tech["date"],
        "price": int(round(tech["price"])),
        "open": int(round(tech["open"])),
        "dayGainPct": tech["dayGainPct"],
        "sma200": tech["sma200"],
        "sma200DiffPct": tech["sma200DiffPct"],
        "sma20": tech["sma20"],
        "sma20DiffPct": tech["sma20DiffPct"],
        "volRatio": tech["volRatio"],
        "prior20dGainPct": tech["prior20dGainPct"],
        "upperWickRatio": tech["upperWickRatio"],
        "foreignNet": f_net,
        "instNet": i_net,
        "smartNet3d": int(trend.get("smartNet3d") or 0),
        "peadBadge": pead_badge,
        "quarter": fin.get("quarter"),
        "opProfitEok": op_eok,
        "opYoyPct": op_yoy,
        "revYoyPct": rev_yoy,
        "turnaround": turnaround,
        "disclosure": (earn_disc or cat_disc or {}).get("title") or "",
        "afterHoursDisclosure": bool((earn_disc or {}).get("afterHours")),
        "stopPrice": int(stop_px),
        "stopPct": stop_pct,
        "passed": passed,
        "score": round(min(100.0, score), 1),
        "checks": {
            "market200": mkt_ok,
            "stock200": above_200 and prior_ok,
            "pead": pead_ok and not toxic,
            "volume": vol_ok and candle_ok,
            "smartMoney": flow_ok,
        },
        "reasons": reasons,
        "failReasons": fails,
    }


def decide_pead_exit(
    pos: Dict[str, Any],
    price: float,
    sma20: Optional[float],
    hold_days: int,
    is_close_window: bool,
    params: PeadParams,
) -> Optional[Dict[str, Any]]:
    """보유 포지션의 기계적 청산 조건 판정.

    1) 장중·종가 불문: 실적 기준봉 시가(stopPrice) 하향 이탈 → 즉시 전량 손절 (STOP_LOSS)
    2) 종가 무렵(15:10~) · 최소 1거래일 경과 후: 일봉 종가 < 20일선 → PEAD 추세 종료 익절/청산 (TRAIL_MA20)
    3) 최대 보유 거래일(기본 60일) 도달 → 슬롯 회전 청산 (TIME_EXIT)
    """
    if price <= 0:
        return None
    stop_px = float(pos.get("stopPrice") or 0)
    if stop_px > 0 and price < stop_px:
        return {
            "kind": "STOP_LOSS",
            "reason": f"실적 장대양봉 시가({stop_px:,.0f}원) 이탈 — 가짜 서프라이즈 방어 즉시 손절",
        }
    if is_close_window and hold_days >= 1 and sma20 and sma20 > 0 and price < sma20:
        return {
            "kind": "TRAIL_MA20",
            "reason": f"20일 이동평균선({sma20:,.0f}원) 종가 이탈 ({hold_days}일 보유) — PEAD 추세 추종 청산",
        }
    if is_close_window and hold_days >= params.maxHoldDays:
        return {
            "kind": "TIME_EXIT",
            "reason": f"최대 보유 기한({params.maxHoldDays}거래일) 도달 — 슬롯 회전 청산",
        }
    return None


# ───────────────────── 스윙 포트폴리오 엔진 ─────────────────────


class PeadEngine:
    def __init__(self, namuh_account=None):
        self.acc = namuh_account
        self.params = PeadParams()
        self.enabled = True
        self.lock = threading.Lock()

        self.positions: Dict[str, Dict[str, Any]] = {}
        self.candidates_pool: List[Dict[str, Any]] = []  # 스캔 평가 풀 (제외·슬롯 변동 시 재조회 없이 2배수 슬라이싱)
        self.dismissed: List[str] = []                   # 사용자가 [제외]를 누른 종목 코드
        self.pending: List[Dict[str, Any]] = []          # SEMI 모드 승인 대기 종목 (남은 슬롯 × 2배수 이하)
        self.watchlist: List[Dict[str, Any]] = []        # 최근 스캔 추천 종목 (남은 슬롯 × 2배수 엄선)
        self.after_hours_queue: List[Dict[str, Any]] = []  # 장후 실적공시 → 익일 감시 대기열
        self.trades: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, Any]] = []

        self.regime: Dict[str, Any] = {
            "gate": "UNKNOWN",
            "gateLabel": "스캔 전 — [지금 전종목 스캔] 버튼을 누르면 즉시 판독합니다",
            "kospi": None,
            "kosdaq": None,
            "kofr": None,
        }
        self.parking: Dict[str, Any] = {
            "accruedKrw": 0.0,
            "lastDate": "",
            "daysCounted": 0,
        }
        self.scan_meta: Dict[str, Any] = {
            "lastScanAt": "",
            "lastScanDate": "",
            "lastCloseCheckDate": "",
            "lastAfterHoursDate": "",
            "scanning": False,
            "done": 0,
            "total": 0,
            "stage1Passed": 0,
            "passedCount": 0,
        }
        self.last_error: str = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._trading_day: Dict[str, bool] = {}       # {날짜: 장이 열렸나} — 하루 한 번만 본다

    def is_trading_day(self, today: str) -> bool:
        """오늘 코스피 일봉이 생겼나 — 평일 휴장일(설 · 추석 · 선거일 등)에는 어제 봉이 마지막이다.
        모르면(조회 실패) 연 날로 본다 (평소처럼 돈다)."""
        if today not in self._trading_day:
            try:
                bars = fetch_daily_bars("KOSPI", 3)
                self._trading_day[today] = bool(bars) and bars[-1]["date"] == today
            except Exception as e:
                logger.debug(f"휴장일 확인 실패 (연 날로 봄): {e}")
                return True
        return self._trading_day[today]

    def log(self, level: str, msg: str) -> None:
        self.logs.append({
            "time": datetime.now(KST).strftime("%m-%d %H:%M:%S"),
            "level": level,
            "message": msg,
        })
        if len(self.logs) > MAX_LOGS:
            self.logs = self.logs[-MAX_LOGS:]

    # ── 저장 · 복원 ──
    def load(self) -> None:
        recs = _active_store().load()
        if not recs:
            return
        s = recs[0]
        self.enabled = bool(s.get("enabled", True))
        self.params = PeadParams.from_dict(s.get("params"))
        self.positions = dict(s.get("positions") or {})
        self.candidates_pool = list(s.get("candidatesPool") or s.get("watchlist") or [])
        self.dismissed = list(s.get("dismissed") or [])
        self.pending = list(s.get("pending") or [])
        self.watchlist = list(s.get("watchlist") or [])
        self.after_hours_queue = list(s.get("afterHoursQueue") or [])
        self.trades = list(s.get("trades") or [])[-MAX_TRADES:]
        self.logs = list(s.get("logs") or [])[-MAX_LOGS:]
        if s.get("regime"):
            self.regime = dict(s["regime"])
        if s.get("parking"):
            self.parking.update(s["parking"])
        if s.get("scanMeta"):
            self.scan_meta.update(s["scanMeta"])
            self.scan_meta["scanning"] = False

    def _sync_recommendations_locked(self) -> List[Dict[str, Any]]:
        """보유 슬롯을 뺀 '남은 빈 슬롯 × 배수(기본 2배수)' 만큼만 추천 목록을 엄선한다."""
        idle_slots = max(0, self.params.maxSlots - len(self.positions))
        limit = idle_slots * self.params.candidateMultiplier
        blocked = set(self.positions.keys()) | set(self.dismissed)
        pool = self.candidates_pool if self.candidates_pool else self.watchlist
        filtered = [dict(c) for c in pool if c.get("code") not in blocked]
        selected = filtered[:limit] if limit > 0 else []

        slot_budget = self.params.slot_budget()
        enriched: List[Dict[str, Any]] = []
        for idx, item in enumerate(selected, start=1):
            px = float(item.get("price") or 0)
            checks = item.get("checks") or {}
            enriched.append({
                **item,
                "rank": idx,
                "slotBudgetKrw": round(slot_budget),
                "estQty": max(1, int(slot_budget // px)) if px > 0 and slot_budget >= px else 0,
                "passedChecks": sum(1 for v in checks.values() if v),
                "totalChecks": max(5, len(checks)),
            })

        self.watchlist = enriched
        self.pending = [c for c in enriched if c.get("passed")]
        return enriched

    def save(self) -> None:
        with self.lock:
            self._sync_recommendations_locked()
            snap = {
                "enabled": self.enabled,
                "params": asdict(self.params),
                "positions": dict(self.positions),
                "candidatesPool": list(self.candidates_pool[:30]),
                "dismissed": list(self.dismissed[:50]),
                "pending": list(self.pending[:20]),
                "watchlist": list(self.watchlist[:20]),
                "afterHoursQueue": list(self.after_hours_queue[:30]),
                "trades": list(self.trades[-MAX_TRADES:]),
                "logs": list(self.logs[-MAX_LOGS:]),
                "regime": dict(self.regime),
                "parking": dict(self.parking),
                "scanMeta": {**self.scan_meta, "scanning": False},
            }
        _active_store().save([snap])

    # ── 호가 / 현재가 체결 도우미 ──
    def _live_quote_or_fallback(self, code: str, fallback_px: float) -> Dict[str, Any]:
        """나무증권 계좌가 연결돼 있으면 10단계 호가를 읽고, 없거나 장외면 일봉 종가로 대체한다."""
        if self.acc is not None and getattr(self.acc, "configured", False):
            try:
                q = krx.quote(self.acc, code)
                if q and q.get("price", 0) > 0:
                    return q
            except NamuhError as e:
                logger.debug(f"PEAD 나무 호가 조회 생략({code}): {e}")
            except Exception as e:
                logger.debug(f"PEAD 호가 예외({code}): {e}")
        # 나무 미연동 · 장외 시간에도 모의 매수/청산 시험이 가능하도록 1틱(약 0.1%) 슬리피지 반영
        ask = round(fallback_px * 1.001, 2) if fallback_px > 0 else 0.0
        bid = round(fallback_px * 0.999, 2) if fallback_px > 0 else 0.0
        return {"code": code, "price": fallback_px, "ask": ask, "bid": bid}

    # ── 유휴 현금 KOFR 파킹 일복리 적립 ──
    def accrue_parking(self, today: str) -> None:
        """평일마다 비어 있는 슬롯 현금에 KOFR 파킹 이자(연 parkingRatePct% ÷ 250거래일)를 적립한다."""
        if not today or self.parking.get("lastDate") == today:
            return
        invested = sum(float(p.get("cost") or 0) for p in self.positions.values())
        idle_cash = max(0.0, self.params.seedKrw - invested)
        daily_rate = (self.params.parkingRatePct / 100.0) / 250.0
        # KOFR 일봉 변동률이 양수로 잡히면 실제 변동률을 우선 사용 (배당락일 음수 제외)
        kofr_daily = ((self.regime.get("kofr") or {}).get("dailyPct") or 0.0) / 100.0
        if 0.00003 <= kofr_daily <= 0.0005:
            daily_rate = kofr_daily
        interest = round(idle_cash * daily_rate, 1)
        self.parking["accruedKrw"] = round(float(self.parking.get("accruedKrw") or 0.0) + interest, 1)
        self.parking["lastDate"] = today
        self.parking["daysCounted"] = int(self.parking.get("daysCounted") or 0) + 1
        if interest >= 1.0:
            self.log("INFO", f"KOFR 파킹 이자 +{interest:,.0f}원 적립 (유휴 현금 {idle_cash:,.0f}원)")

    # ── 전종목 2단계 퍼널 스캔 ──
    def run_scan(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """코스피200 ∪ 코스닥150 전종목을 훑어 시장 신호등 · 승인 대기 시그널 · 상위 관심주를 갱신한다."""
        ref = now or datetime.now(KST)
        with self.lock:
            if self.scan_meta.get("scanning"):
                return self.status()
            self.scan_meta["scanning"] = True
            self.scan_meta["done"] = 0
        self.last_error = ""
        try:
            regime = compute_market_regime(self.params)
            with self.lock:
                self.regime = regime

            rows = krx_master.candidates(krx_master.load())
            # 장후 공시 대기열에 있던 종목이 마스터 후보에 있으면 우선 포함
            total = len(rows)
            self.scan_meta["total"] = total

            # 1차 기술적 퍼널: 일봉으로 기준 이평선(기본 60일 수급선) · 거래량 · 양봉 여부를 빠르게 계산
            stage1_primary: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            stage1_backup: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

            def _fetch_one(r: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
                bars = fetch_daily_bars(r["code"], max(120, self.params.indexSmaPeriod + 22))
                tech = analyze_bars(bars, self.params.indexSmaPeriod, self.params.exitMaPeriod)
                if not tech:
                    return None
                return r, tech

            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(_fetch_one, r): r for r in rows}
                for fut in as_completed(futs):
                    self.scan_meta["done"] += 1
                    try:
                        res = fut.result()
                    except Exception:
                        continue
                    if not res:
                        continue
                    r, tech = res
                    # 보유 중인 종목이면 최신 가격·20일선을 함께 갱신
                    if r["code"] in self.positions:
                        with self.lock:
                            pos = self.positions.get(r["code"])
                            if pos:
                                pos["lastPrice"] = tech["price"]
                                pos["sma20"] = tech["sma20"]
                                pos["peak"] = max(float(pos.get("peak") or tech["price"]), tech["price"])
                                pos["low"] = min(float(pos.get("low") or tech["price"]), tech["price"])
                    # 2차 심층 판독 대상: 1순위(200일선 정배열 + 거래량/상승률 활성) 및 조용한 장세 대비 2순위 백업 풀
                    queued_codes = {x["code"] for x in self.after_hours_queue}
                    if r["code"] in queued_codes or (
                        tech["above200"]
                        and (tech["volRatio"] >= max(1.3, self.params.minVolRatio * 0.45)
                             or tech["dayGainPct"] >= max(2.0, self.params.minDayGainPct * 0.6))
                    ):
                        stage1_primary.append((r, tech))
                    else:
                        stage1_backup.append((r, tech))

            # 남은 빈 슬롯의 배수(기본 2배수) + 제외 대비 예비분만큼 2차 심층(수급+재무+공시) 판독
            stage1_primary.sort(key=lambda x: (x[1]["volRatio"] * 2.0 + max(0.0, x[1]["dayGainPct"])), reverse=True)
            stage1_backup.sort(
                key=lambda x: (
                    1 if x[1]["above200"] else 0,
                    x[1]["volRatio"] * 1.5 + max(0.0, x[1]["dayGainPct"]),
                    x[1]["sma200DiffPct"],
                ),
                reverse=True,
            )
            with self.lock:
                idle_now = max(0, self.params.maxSlots - len(self.positions))
                rec_want = idle_now * self.params.candidateMultiplier
            deep_cap = max(12, min(30, rec_want + 8))
            deep_targets = (stage1_primary + stage1_backup)[:deep_cap]
            self.scan_meta["stage1Passed"] = len(stage1_primary)

            dart_map = fetch_dart_today_earnings(ref) if deep_targets else {}
            evaluated: List[Dict[str, Any]] = []

            def _deep_one(item: Tuple[Dict[str, Any], Dict[str, Any]]) -> Dict[str, Any]:
                r, tech = item
                code = r["code"]
                try:
                    trend = fetch_investor_trend(code)
                except Exception:
                    trend = {}
                try:
                    fin = fetch_financial_surprise(code)
                except Exception:
                    fin = {"available": False}
                try:
                    disc = fetch_recent_disclosures(code, self.params.lookbackDays, ref)
                except Exception:
                    disc = {}
                if code in dart_map and not disc.get("earnings"):
                    disc["earnings"] = dart_map[code]
                return evaluate_candidate(code, r["name"], r["market"], tech, trend, fin, disc, regime, self.params)

            if deep_targets:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for fut in as_completed([pool.submit(_deep_one, it) for it in deep_targets]):
                        try:
                            evaluated.append(fut.result())
                        except Exception as e:
                            logger.debug(f"심층 분석 오류: {e}")

            evaluated.sort(key=lambda c: (1 if c["passed"] else 0, c["score"], c["volRatio"]), reverse=True)
            passed_all = [c for c in evaluated if c["passed"] and c["code"] not in self.positions]

            with self.lock:
                self.dismissed = []                  # 새 스캔 시 제외 목록 초기화
                self.candidates_pool = evaluated[:30]
                recs = self._sync_recommendations_locked()
                self.scan_meta.update({
                    "lastScanAt": ref.strftime("%Y-%m-%d %H:%M:%S"),
                    "lastScanDate": ref.strftime("%Y-%m-%d"),
                    "passedCount": len(passed_all),
                })

            self.log(
                "INFO",
                f"전종목 스캔 완료: {total}종목 → 추천 {len(recs)}종목 엄선 "
                f"(남은 슬롯 {idle_now}개 × {self.params.candidateMultiplier}배수 · 전관문 통과 {len(passed_all)}종목)",
            )
            today_str = ref.strftime("%Y-%m-%d")
            if ref.weekday() < 5 and self.is_trading_day(today_str):
                self.accrue_parking(today_str)
            self.save()
        except Exception as e:
            self.last_error = f"스캔 실패: {e}"
            self.log("ERROR", self.last_error)
            logger.warning(f"PEAD 스캔 오류: {e}")
        finally:
            with self.lock:
                self.scan_meta["scanning"] = False
        return self.status()

    def start_scan_async(self) -> Dict[str, Any]:
        """화면에서 [지금 전종목 스캔] 클릭 시 백그라운드 스레드로 즉시 실행한다."""
        with self.lock:
            if self.scan_meta.get("scanning") or (self._scan_thread and self._scan_thread.is_alive()):
                return self.status()
        self._scan_thread = threading.Thread(target=self.run_scan, name="pead-scan", daemon=True)
        self._scan_thread.start()
        time.sleep(0.05)
        return self.status()

    # ── 매수 실행 (승인 또는 AUTO 모드) ──
    def buy_candidate(self, code: str, now: Optional[datetime] = None, force: bool = False) -> Tuple[bool, str]:
        """승인 대기(또는 관심 목록) 종목을 빈 슬롯에 모의 매수한다."""
        ref = now or datetime.now(KST)
        code = str(code or "").strip()
        with self.lock:
            if code in self.positions:
                return False, "이미 보유 중인 종목입니다."
            if len(self.positions) >= self.params.maxSlots:
                return False, f"슬롯이 모두 찼습니다 ({len(self.positions)}/{self.params.maxSlots}슬롯)."
            cand = next((x for x in self.pending if x["code"] == code), None)
            if not cand:
                cand = next((x for x in self.watchlist if x["code"] == code), None)
            if not cand:
                cand = next((x for x in self.candidates_pool if x["code"] == code), None)
            if not cand:
                return False, f"최근 스캔 목록에 없는 종목입니다 ({code}). 먼저 스캔을 실행하세요."
            if not force:
                mkt_ok, mkt_msg = market_allows_entry(self.regime, cand.get("market", "kospi"))
                if not mkt_ok:
                    return False, f"시장 신호등 방어 중: {mkt_msg}"

            invested = sum(float(p.get("cost") or 0) for p in self.positions.values())
            budget = min(self.params.slot_budget(), max(0.0, self.params.seedKrw - invested))

        q = self._live_quote_or_fallback(code, float(cand["price"]))
        fill = walk_buy(q, budget)
        if not fill:
            px_fallback = float(q.get("ask") or q.get("price") or cand["price"])
            qty = int(budget // px_fallback) if px_fallback > 0 else 0
            if qty <= 0:
                return False, f"슬롯 예산({budget:,.0f}원)으로 1주도 살 수 없습니다."
            fill = (qty, px_fallback)

        qty, avg_px = fill
        cost = round(qty * avg_px, 2)
        stop_px = compute_stop_price(float(cand.get("open") or avg_px * 0.94), avg_px)

        pos = {
            "code": code,
            "name": cand["name"],
            "market": cand.get("market", "kospi"),
            "entryDate": ref.strftime("%Y-%m-%d"),
            "entryAt": ref.strftime("%Y-%m-%d %H:%M:%S"),
            "entry": round(avg_px, 2),
            "qty": int(qty),
            "cost": cost,
            "stopPrice": stop_px,
            "breakoutOpen": int(cand.get("open") or round(avg_px)),
            "sma20": float(cand.get("sma20") or 0.0),
            "lastPrice": round(avg_px, 2),
            "peak": round(avg_px, 2),
            "low": round(avg_px, 2),
            "holdDays": 0,
            "lastCountedDate": ref.strftime("%Y-%m-%d"),
            "peadBadge": cand.get("peadBadge") or "",
            "volRatio": cand.get("volRatio") or 0.0,
        }
        reason = f"{cand.get('peadBadge', 'PEAD')} · 거래량 {cand.get('volRatio', 0)}배 · 손절선 {stop_px:,.0f}원"
        with self.lock:
            self.positions[code] = pos
            self._sync_recommendations_locked()
            self._record_trade(code, cand["name"], "BUY", round(avg_px, 2), int(qty), 0.0, 0.0, 0, reason, ref)
        self.log("BUY", f"슬롯 모의매수 {cand['name']}({code}) {qty:,}주 @ {avg_px:,.0f}원 (손절선 {stop_px:,.0f}원)")
        self.save()
        return True, f"{cand['name']} {qty:,}주 모의매수 완료"

    def reject_candidate(self, code: str) -> None:
        code = str(code or "").strip()
        if not code:
            return
        target_name = code
        with self.lock:
            hit = next((x for x in (self.watchlist + self.candidates_pool) if x.get("code") == code), None)
            if hit:
                target_name = hit.get("name") or code
            if code not in self.dismissed:
                self.dismissed.append(code)
            self._sync_recommendations_locked()
        self.log("INFO", f"추천 목록 제외: {target_name}({code}) — 다음 순위 후보로 대체")
        self.save()

    # ── 포지션 청산 ──
    def close_position(
        self, code: str, kind: str = "MANUAL", reason: str = "사용자 수동 청산", now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        ref = now or datetime.now(KST)
        code = str(code or "").strip()
        with self.lock:
            pos = self.positions.get(code)
            if not pos:
                return False, f"보유 중이지 않은 종목입니다 ({code})."

        q = self._live_quote_or_fallback(code, float(pos.get("lastPrice") or pos["entry"]))
        qty = int(pos["qty"])
        sell_px = walk_sell(q, qty)
        gross = (sell_px - pos["entry"]) * qty
        fee = sell_px * qty * (self.params.costPct / 100.0)
        pnl = round(gross - fee, 2)
        pct = round(net_pct(pos["entry"], sell_px, self.params.costPct), 2)
        peak = max(float(pos.get("peak") or sell_px), sell_px)
        low = min(float(pos.get("low") or sell_px), sell_px)
        mfe = round((peak / pos["entry"] - 1.0) * 100.0, 2) if pos["entry"] > 0 else 0.0
        mae = round((low / pos["entry"] - 1.0) * 100.0, 2) if pos["entry"] > 0 else 0.0
        hold_days = int(pos.get("holdDays") or 0)

        with self.lock:
            self.positions.pop(code, None)
            self._record_trade(
                code, pos["name"], kind, round(sell_px, 2), qty, pnl, pct, hold_days, reason, ref,
                extra={"mfePct": mfe, "maePct": mae},
            )
        lv = "SELL" if pnl < 0 else "BUY"
        self.log(lv, f"모의청산[{kind}] {pos['name']}({code}) {qty:,}주 @ {sell_px:,.0f}원 ({pct:+.2f}%, {pnl:+,.0f}원) — {reason}")
        self.save()
        return True, f"{pos['name']} 청산 완료 ({pct:+.2f}%, {pnl:+,.0f}원)"

    def _record_trade(
        self, code: str, name: str, kind: str, price: float, qty: int,
        pnl: float, pct: float, hold_days: int, reason: str, now: datetime,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        row = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "code": code,
            "name": name,
            "kind": kind,
            "price": price,
            "qty": qty,
            "pnl": pnl,
            "pct": pct,
            "holdDays": hold_days,
            "reason": reason,
            **(extra or {}),
        }
        self.trades.append(row)
        if len(self.trades) > MAX_TRADES:
            self.trades = self.trades[-MAX_TRADES:]

    # ── 보유 종목 손절·익절 점검 ──
    def check_positions(self, now: Optional[datetime] = None, is_close_window: bool = False) -> None:
        ref = now or datetime.now(KST)
        today = ref.strftime("%Y-%m-%d")
        for code in list(self.positions.keys()):
            pos = self.positions.get(code)
            if not pos:
                continue
            sma20 = float(pos.get("sma20") or 0.0)
            px = 0.0
            if self.acc is not None and getattr(self.acc, "configured", False) and not is_close_window:
                try:
                    q = krx.quote(self.acc, code)
                    px = float(q.get("price") or 0.0)
                except Exception:
                    px = 0.0
            if px <= 0 or is_close_window:
                try:
                    bars = fetch_daily_bars(code, max(30, self.params.exitMaPeriod + 5))
                    if bars:
                        px = bars[-1]["close"]
                        if len(bars) >= self.params.exitMaPeriod:
                            sma20 = round(
                                sum(b["close"] for b in bars[-self.params.exitMaPeriod:]) / self.params.exitMaPeriod, 2
                            )
                except Exception as e:
                    logger.debug(f"보유종목 일봉 확인 실패({code}): {e}")
            if px <= 0:
                continue

            with self.lock:
                cur = self.positions.get(code)
                if not cur:
                    continue
                cur["lastPrice"] = px
                cur["sma20"] = sma20
                cur["peak"] = max(float(cur.get("peak") or px), px)
                cur["low"] = min(float(cur.get("low") or px), px)
                if cur.get("lastCountedDate") != today and ref.weekday() < 5:
                    cur["holdDays"] = int(cur.get("holdDays") or 0) + 1
                    cur["lastCountedDate"] = today
                hold_days = int(cur.get("holdDays") or 0)
                snap_pos = dict(cur)

            act = decide_pead_exit(snap_pos, px, sma20, hold_days, is_close_window, self.params)
            if act:
                self.close_position(code, act["kind"], act["reason"], ref)

    # ── 장후(16:35) 잠정실적 공시 스윕 ──
    def sweep_after_hours(self, now: Optional[datetime] = None) -> None:
        ref = now or datetime.now(KST)
        dart_map = fetch_dart_today_earnings(ref)
        if not dart_map:
            return
        try:
            rows = {r["code"]: r for r in krx_master.candidates(krx_master.load())}
        except Exception:
            rows = {}
        queued = []
        for code, info in dart_map.items():
            if code in rows and code not in self.positions:
                queued.append({
                    "code": code,
                    "name": rows[code]["name"],
                    "title": info["title"],
                    "date": info["date"],
                })
        if queued:
            with self.lock:
                self.after_hours_queue = queued[:30]
            self.log("INFO", f"장후 잠정실적 공시 {len(queued)}건 → 익일 감시 대기열 등록")
            self.save()

    # ── 스케줄 루프 (평일 09:05~15:25 손절 감시 · 15:10 스캔 · 15:18 종가청산/자동매수) ──
    def step(self, now: datetime) -> float:
        if not self.enabled:
            return 30.0
        if now.weekday() >= 5:
            return 600.0

        today = now.strftime("%Y-%m-%d")
        t_open = now.replace(hour=OPEN_AT[0], minute=OPEN_AT[1], second=0, microsecond=0)
        t_scan = now.replace(hour=SCAN_AT[0], minute=SCAN_AT[1], second=0, microsecond=0)
        t_close_chk = now.replace(hour=CLOSE_CHECK_AT[0], minute=CLOSE_CHECK_AT[1], second=0, microsecond=0)
        t_close = now.replace(hour=CLOSE_AT[0], minute=CLOSE_AT[1], second=0, microsecond=0)
        t_after = now.replace(hour=AFTER_HOURS_AT[0], minute=AFTER_HOURS_AT[1], second=0, microsecond=0)

        # 장 마감 뒤에 처음 뜬 프로세스는 기동 직후 무거운 자동 스캔을 돌리지 않는다 (수동 스캔은 언제나 가능)
        if now > t_close and not self.scan_meta.get("lastScanDate"):
            self.scan_meta["lastScanDate"] = today
            self.scan_meta["lastCloseCheckDate"] = today

        # 0) 평일 휴장일 — 어제 일봉으로 스캔 · 종가 청산 · 자동 매수 · 보유일 · 파킹 이자를 세지 않는다
        if t_scan <= now <= t_close and (self.scan_meta.get("lastScanDate") != today
                                         or self.scan_meta.get("lastCloseCheckDate") != today) \
                and not self.is_trading_day(today):
            self.scan_meta["lastScanDate"] = self.scan_meta["lastCloseCheckDate"] = today
            self.log("INFO", f"{today} 휴장일 — 오늘은 스캔 · 종가 점검을 쉽니다")
            self.save()
            return 300.0

        # 1) 15:10~15:25 정규장 마감 직전 전종목 스캔 (하루 1회)
        if t_scan <= now <= t_close and self.scan_meta.get("lastScanDate") != today:
            self.run_scan(now)
            return 10.0

        # 2) 15:18~15:29 종가 기준 20일선 이탈 점검 + AUTO 모드 자동 슬롯 매수 (하루 1회)
        if t_close_chk <= now <= t_close and self.scan_meta.get("lastCloseCheckDate") != today:
            self.scan_meta["lastCloseCheckDate"] = today
            self.accrue_parking(today)
            if self.positions:
                self.check_positions(now, is_close_window=True)
            if self.params.approvalMode == "AUTO":
                for cand in list(self.pending):
                    if len(self.positions) >= self.params.maxSlots:
                        break
                    self.buy_candidate(cand["code"], now=now)
            self.save()
            return 30.0

        # 3) 16:35 이후 장후 공시 스윕 (하루 1회)
        if now >= t_after and self.scan_meta.get("lastAfterHoursDate") != today:
            self.scan_meta["lastAfterHoursDate"] = today
            self.sweep_after_hours(now)
            return 300.0

        # 4) 정규장 중(09:05 ~ 15:18) 보유 종목 시가 손절선 실시간 방어 (3분 간격 — 나무 호출 부담 최소화)
        if t_open <= now < t_close_chk and self.positions and self.is_trading_day(today):
            self.check_positions(now, is_close_window=False)
            return 180.0

        if now < t_open:
            return min(300.0, max(10.0, (t_open - now).total_seconds()))
        if now < t_scan:
            return min(300.0, max(10.0, (t_scan - now).total_seconds()))
        return 300.0

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.step(datetime.now(KST))
            except Exception as e:
                logger.exception("국내 PEAD 루프 오류")
                self.last_error = str(e)
                wait = 60.0
            self._stop.wait(wait)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self.load()
        except Exception as e:
            self.log("ERROR", f"저장된 PEAD 상태를 읽지 못했습니다: {e}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kr-pead", daemon=True)
        self._thread.start()
        logger.info("국내 200일선·PEAD 엔진(모의) 시작")

    def stop(self) -> None:
        self._stop.set()

    def set_enabled(self, on: bool) -> None:
        with self.lock:
            self.enabled = bool(on)
            self.log("INFO", "PEAD 엔진을 켰습니다" if on else "PEAD 엔진을 껐습니다")
        self.save()

    def update_params(self, updates: Dict[str, Any]) -> None:
        with self.lock:
            merged = {**asdict(self.params), **{k: v for k, v in (updates or {}).items() if v is not None}}
            self.params = PeadParams.from_dict(merged)
            self.log(
                "INFO",
                f"설정 변경: 모드={self.params.approvalMode} · 기준선={self.params.indexSmaPeriod}일선 · "
                f"슬롯={self.params.maxSlots}개 · 거래량≥{self.params.minVolRatio}배 · 영업이익YoY≥{self.params.minOpYoyPct}%",
            )
        self.save()

    def status(self) -> Dict[str, Any]:
        with self.lock:
            held = []
            unrealized_total = 0.0
            invested_total = 0.0
            for code, pos in self.positions.items():
                px = float(pos.get("lastPrice") or pos["entry"])
                qty = int(pos["qty"])
                cost = float(pos.get("cost") or pos["entry"] * qty)
                invested_total += cost
                npct = round(net_pct(pos["entry"], px, self.params.costPct), 2)
                gross = (px - pos["entry"]) * qty
                fee = px * qty * (self.params.costPct / 100.0)
                upnl = round(gross - fee)
                unrealized_total += upnl
                sma20 = float(pos.get("sma20") or 0.0)
                sma20_gap = round((px / sma20 - 1.0) * 100.0, 2) if sma20 > 0 else None
                stop_gap = round((px / pos["stopPrice"] - 1.0) * 100.0, 2) if pos.get("stopPrice") else None
                held.append({
                    **pos,
                    "price": int(round(px)),
                    "netPct": npct,
                    "unrealizedPnl": upnl,
                    "sma20GapPct": sma20_gap,
                    "stopGapPct": stop_gap,
                })

            exits = [t for t in self.trades if t["kind"] != "BUY"]
            realized_pnl = round(sum(float(t.get("pnl") or 0.0) for t in exits))
            wins = sum(1 for t in exits if float(t.get("pnl") or 0.0) > 0)
            parking_krw = round(float(self.parking.get("accruedKrw") or 0.0))
            idle_cash = max(0.0, self.params.seedKrw - invested_total)
            idle_slots = max(0, self.params.maxSlots - len(held))
            rec_limit = idle_slots * self.params.candidateMultiplier
            recs = self._sync_recommendations_locked()
            total_equity = round(self.params.seedKrw + realized_pnl + unrealized_total + parking_krw)
            total_return_pct = round((total_equity / self.params.seedKrw - 1.0) * 100.0, 2) if self.params.seedKrw else 0.0

            return {
                "enabled": self.enabled,
                "running": bool(self._thread and self._thread.is_alive()),
                "paper": True,
                "namuhConnected": bool(self.acc is not None and getattr(self.acc, "configured", False)),
                "dartConfigured": bool((os.getenv("DART_API_KEY") or "").strip()),
                "params": asdict(self.params),
                "regime": dict(self.regime),
                "scanMeta": dict(self.scan_meta),
                "summary": {
                    "seedKrw": round(self.params.seedKrw),
                    "slotBudgetKrw": round(self.params.slot_budget()),
                    "totalEquityKrw": total_equity,
                    "totalReturnPct": total_return_pct,
                    "realizedPnlKrw": realized_pnl,
                    "unrealizedPnlKrw": round(unrealized_total),
                    "parkingInterestKrw": parking_krw,
                    "usedSlots": len(held),
                    "maxSlots": self.params.maxSlots,
                    "idleSlots": idle_slots,
                    "recommendationLimit": rec_limit,
                    "idleCashKrw": round(idle_cash),
                    "closedTrades": len(exits),
                    "wins": wins,
                    "winRate": round(wins / len(exits) * 100.0, 1) if exits else None,
                },
                "positions": held,
                "recommendations": recs,
                "pending": list(self.pending),
                "watchlist": list(self.watchlist),
                "afterHoursQueue": list(self.after_hours_queue),
                "trades": list(self.trades[-80:])[::-1],
                "logs": list(self.logs[-50:])[::-1],
                "lastError": self.last_error,
            }


_engine: Optional[PeadEngine] = None


def get(namuh_account=None) -> PeadEngine:
    global _engine
    if _engine is None:
        _engine = PeadEngine(namuh_account)
    elif namuh_account is not None:
        _engine.acc = namuh_account
    return _engine
