"""업비트 ↔ 빗썸 원화 괴리를 계속 기록한다 — 국내 양거래소 차익(인벤토리 모델)을 만들지 판단하려고.

사용자가 가져온 local_training(업비트 ↔ 빗썸 차익 봇)을 바로 붙이기 전에, 자금 0원으로
분포부터 모은다. 2026-10-21 2분 표본(상위 60종목 × 12회)에서 '순마진 0.15% 이상' 이 호가
기준 208회 나왔지만, 큰 괴리는 대개 입출금이 막혀 몇 분씩 한 방향으로 벌어져 있는 종목이다.
인벤토리 모델은 한 방향으로만 먹으면 한쪽 재고가 바닥나고, 재분배(전송)가 막혀 있으면 끝이다.
그래서 답해야 할 질문은 이것이다.

  · 순마진이 기준을 넘는 일이 종목별로 얼마나 자주 있나
  · 그것이 **양방향으로 번갈아** 나는가 (재고가 저절로 되돌아오는가)
  · 1호가 잔량이 주문 금액을 감당하나
  · 그때 빗썸 입출금이 열려 있었나 (막혀 있으면 재분배가 안 된다)

**체결가가 아니라 최우선 호가로 계산한다.** 사는 쪽 매도호가 · 파는 쪽 매수호가가 실제로 먹는 값이다
(local_training/scanner.py 는 체결가라 괴리가 실제보다 크게 보였다 — 같은 2분에 387회 vs 208회).

주문은 내지 않는다. 공개 API(키 없음)만 읽는다. 하루 한 파일(data/upbit_gap/YYYY-MM-DD.csv, 서울 날짜)로
쓰고 KEEP_DAYS 가 지난 파일은 지운다 — 큰 파일을 통째로 다시 쓰지 않는다.
분석은 scripts/upbit_gap_report.py.
"""

import csv
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from services.envconf import env_float, env_int

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "upbit_gap")

FEE_UPBIT_PCT = env_float("APP_GAP_FEE_UPBIT_PCT", 0.05)      # 업비트 원화마켓
FEE_BITHUMB_PCT = env_float("APP_GAP_FEE_BITHUMB_PCT", 0.04)  # 빗썸 원화마켓
ROUND_TRIP_FEE_PCT = FEE_UPBIT_PCT + FEE_BITHUMB_PCT          # 한 번 차익 = 양쪽 한 번씩

SAMPLE_SEC = env_float("APP_GAP_SAMPLE_SEC", 60.0)
TOP_N = env_int("APP_GAP_TOP_N", 40)                 # 빗썸 24시간 거래대금 상위 · 두 거래소 공통 상장
UNIVERSE_REFRESH_SEC = 3600.0                        # 종목 목록 · 입출금 상태는 1시간마다
KEEP_DAYS = env_int("APP_GAP_KEEP_DAYS", 45)

COLUMNS = [
    "ts", "coin",
    "uBid", "uBidSize", "uAsk", "uAskSize",
    "bBid", "bBidSize", "bAsk", "bAskSize",
    "netBuyBithumb",      # 빗썸 매수(매도호가) → 업비트 매도(매수호가), 왕복 수수료 차감 (%)
    "netBuyUpbit",        # 업비트 매수 → 빗썸 매도
    "depthBuyBithumbKrw",  # 그 방향 1호가로 바로 체결할 수 있는 금액 (양쪽 중 작은 쪽)
    "depthBuyUpbitKrw",
    "bDeposit", "bWithdraw",   # 빗썸 입금 · 출금 가능 (1/0, 모르면 빈칸)
]

_UPBIT = "https://api.upbit.com/v1"
_BITHUMB = "https://api.bithumb.com/public"

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()
_last_error = ""
_samples = 0
_universe: List[str] = []
_wallet: Dict[str, Tuple[int, int]] = {}
_universe_at = 0.0


def _seoul_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=9)


def _file_for(day: str) -> str:
    return os.path.join(DATA_DIR, f"{day}.csv")


def _get(url: str, **params) -> Any:
    r = requests.get(url, params=params or None, timeout=8)
    r.raise_for_status()
    return r.json()


def _refresh_universe() -> None:
    """두 거래소 공통 원화 상장 중 빗썸 거래대금 상위 TOP_N 과 빗썸 입출금 상태."""
    global _universe, _wallet, _universe_at
    up = {m["market"][4:] for m in _get(f"{_UPBIT}/market/all")
          if str(m.get("market", "")).startswith("KRW-")}
    bt = _get(f"{_BITHUMB}/ticker/ALL_KRW").get("data") or {}
    common = [c for c in up if isinstance(bt.get(c), dict)]
    common.sort(key=lambda c: -float(bt[c].get("acc_trade_value_24H") or 0))
    wallet: Dict[str, Tuple[int, int]] = {}
    try:
        for c, s in (_get(f"{_BITHUMB}/assetsstatus/ALL").get("data") or {}).items():
            wallet[c] = (int(s.get("deposit_status") or 0), int(s.get("withdrawal_status") or 0))
    except Exception as e:                       # 상태를 못 받아도 호가 기록은 계속한다 (빈칸으로)
        logger.warning(f"빗썸 입출금 상태 조회 실패: {e}")
    _universe, _wallet, _universe_at = common[:TOP_N], wallet, time.time()


def _upbit_books(coins: List[str]) -> Dict[str, Tuple[float, float, float, float]]:
    out: Dict[str, Tuple[float, float, float, float]] = {}
    for i in range(0, len(coins), 30):
        chunk = coins[i:i + 30]
        for it in _get(f"{_UPBIT}/orderbook", markets=",".join("KRW-" + c for c in chunk)):
            u = (it.get("orderbook_units") or [None])[0]
            if u:
                out[it["market"][4:]] = (float(u["bid_price"]), float(u["bid_size"]),
                                         float(u["ask_price"]), float(u["ask_size"]))
    return out


def _bithumb_books() -> Dict[str, Tuple[float, float, float, float]]:
    out: Dict[str, Tuple[float, float, float, float]] = {}
    data = _get(f"{_BITHUMB}/orderbook/ALL_KRW", count=1).get("data") or {}
    for c, d in data.items():
        if not isinstance(d, dict) or not d.get("bids") or not d.get("asks"):
            continue
        b, a = d["bids"][0], d["asks"][0]
        out[c] = (float(b["price"]), float(b["quantity"]), float(a["price"]), float(a["quantity"]))
    return out


def gap_row(ts: str, coin: str, u: Tuple[float, float, float, float],
            b: Tuple[float, float, float, float], wallet: Optional[Tuple[int, int]]) -> List[Any]:
    """한 종목 한 관측. 수식은 여기 한 곳에만 둔다 (분석 스크립트 · 테스트가 같은 값을 본다)."""
    u_bid, u_bs, u_ask, u_as = u
    b_bid, b_bs, b_ask, b_as = b
    net_bb = (u_bid - b_ask) / b_ask * 100.0 - ROUND_TRIP_FEE_PCT     # 빗썸에서 사서 업비트에 판다
    net_bu = (b_bid - u_ask) / u_ask * 100.0 - ROUND_TRIP_FEE_PCT     # 업비트에서 사서 빗썸에 판다
    depth_bb = min(b_as * b_ask, u_bs * u_bid)
    depth_bu = min(u_as * u_ask, b_bs * b_bid)
    dep, wd = wallet if wallet else ("", "")
    return [ts, coin, u_bid, u_bs, u_ask, u_as, b_bid, b_bs, b_ask, b_as,
            round(net_bb, 4), round(net_bu, 4), round(depth_bb), round(depth_bu), dep, wd]


def _sample() -> Tuple[str, List[List[Any]]]:
    if not _universe or time.time() - _universe_at >= UNIVERSE_REFRESH_SEC:
        _refresh_universe()
    now = _seoul_now()
    ub = _upbit_books(_universe)
    bb = _bithumb_books()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = [gap_row(ts, c, ub[c], bb[c], _wallet.get(c)) for c in _universe if c in ub and c in bb]
    return now.strftime("%Y-%m-%d"), rows


def _append(day: str, rows: List[List[Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _file_for(day)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        w.writerows(rows)


def _prune(today: str) -> None:
    """KEEP_DAYS 가 지난 날짜 파일을 지운다."""
    try:
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        for name in os.listdir(DATA_DIR):
            if name.endswith(".csv") and name[:-4] < cutoff:
                os.remove(os.path.join(DATA_DIR, name))
    except Exception as e:
        logger.warning(f"업비트 괴리 기록 정리 실패: {e}")


def _loop() -> None:
    global _last_error, _samples
    n = 0
    last_day = ""
    while not _stop.is_set():
        try:
            day, rows = _sample()
            if rows:
                with _lock:
                    _append(day, rows)
                    _samples += 1
                    if day != last_day:
                        _prune(day)
                        last_day = day
            _last_error = ""
        except Exception as e:
            _last_error = str(e)
            if n % 30 == 0:                    # 실패마다 로그를 쏟지 않는다 — status() 로 본다
                logger.warning(f"업비트 괴리 기록 실패: {e}")
        n += 1
        _stop.wait(SAMPLE_SEC)


def status() -> Dict[str, Any]:
    files, size = [], 0
    try:
        files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".csv"))
        size = sum(os.path.getsize(os.path.join(DATA_DIR, f)) for f in files)
    except OSError:
        pass
    return {"running": bool(_thread and _thread.is_alive()), "samples": _samples,
            "coins": len(_universe), "days": len(files), "bytes": size,
            "sampleSec": SAMPLE_SEC, "keepDays": KEEP_DAYS, "dir": DATA_DIR, "lastError": _last_error}


def start() -> None:
    """서버 기동 시 한 번 부른다. 실패해도 서버를 막지 않는다."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="upbit-gap-recorder", daemon=True)
    _thread.start()
    logger.info(f"업비트↔빗썸 괴리 기록기 시작 · {SAMPLE_SEC:.0f}초 간격 · 상위 {TOP_N}종목 · {DATA_DIR}")


def stop() -> None:
    _stop.set()
