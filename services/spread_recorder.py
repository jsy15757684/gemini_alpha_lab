"""거래소 간 괴리를 계속 기록한다 — 무전송 양방향을 만들지 말지 판단하려고.

지금까지의 근거는 스냅샷이었다. 1분봉·1시간봉 각 200표본 × 4종목,
약 1,600관측 중 손익분기를 넘은 시점이 2회. 그 2회로 세 전략 중 구현
난도가 제일 높은 것을 만들 수는 없다. 조용한 구간이었을 수도 있다.

그래서 자금 0원으로 먼저 분포를 모은다. 몇 주 뒤 "손익분기를 넘는 일이
실제로 얼마나 자주 있나" 에 답이 나오면 그때 구현을 판단한다.

**마지막 체결가가 아니라 호가(best bid/ask)를 기록한다.** 레이더가 쓰는
체결가 기준 괴리는 '보이는 값' 이고, 실제로 먹을 수 있는 값은 파는 쪽
매수호가와 사는 쪽 매도호가로 계산해야 한다. 둘을 모두 남겨 나중에
얼마나 차이 나는지도 볼 수 있게 한다.

수수료는 시뮬레이터와 같은 상수를 쓴다. 호가 스프레드는 별도로 빼지
않는다 — 호가로 계산한 순간 이미 반영돼 있다.

data/spread_log.csv 에 append 한다. 1분 간격이면 하루 5,760행(4종목),
한 달에 약 15MB 다. 상한을 넘으면 오래된 쪽부터 버린다.
"""

import os
import csv
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import requests

from services import bithumb
from services.envconf import env_float, env_int

logger = logging.getLogger(__name__)

LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "spread_log.csv")

# 시뮬레이터(_step_spatial_dual)와 같은 값을 쓴다. 다르면 기록과 판단이 어긋난다.
FEE_DOMESTIC = 0.0004     # 빗썸 0.04%
FEE_FOREIGN = 0.0010      # 바이낸스 현물 taker 0.10%
ROUND_TRIP_FEE_PCT = (FEE_DOMESTIC + FEE_FOREIGN) * 100.0   # 0.14%

SAMPLE_SEC = env_float("APP_SPREAD_SAMPLE_SEC", 60.0)
MAX_ROWS = env_int("APP_SPREAD_MAX_ROWS", 500_000)          # 약 90일치(4종목/분)

COLUMNS = [
    "ts", "coin",
    "krwBid", "krwAsk", "krwLast",
    "usdBid", "usdAsk", "usdLast",
    "usdtKrw",
    "grossSpreadPct",     # 체결가 기준 (레이더가 보여주는 값)
    "execSellDomPct",     # 국내 매도(bid) · 해외 매수(ask) — 실제로 먹는 괴리
    "execSellForPct",     # 해외 매도(bid) · 국내 매수(ask)
    "netSellDomPct",      # 위에서 왕복 수수료 차감
    "netSellForPct",
]

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()
_last_error = ""
_samples = 0


def _seoul_now() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")


def _bithumb_book(coin: str) -> Optional[Dict[str, float]]:
    """빗썸 최우선 호가. 실패하면 None — 추정하지 않는다."""
    try:
        r = requests.get(f"https://api.bithumb.com/public/orderbook/{coin}_KRW",
                         params={"count": 1}, timeout=5)
        d = r.json()
        if d.get("status") != "0000":
            return None
        data = d["data"]
        return {"bid": float(data["bids"][0]["price"]),
                "ask": float(data["asks"][0]["price"])}
    except Exception:
        return None


def _binance_book(coins: List[str]) -> Dict[str, Dict[str, float]]:
    """바이낸스 최우선 호가. 받지 못한 종목은 결과에 넣지 않는다."""
    out: Dict[str, Dict[str, float]] = {}
    try:
        symbols = "[" + ",".join(f'"{c}USDT"' for c in coins) + "]"
        r = requests.get("https://api.binance.com/api/v3/ticker/bookTicker",
                         params={"symbols": symbols}, timeout=8)
        for it in r.json():
            sym = it.get("symbol", "")
            if sym.endswith("USDT"):
                out[sym[:-4]] = {"bid": float(it["bidPrice"]),
                                 "ask": float(it["askPrice"])}
    except Exception:
        pass
    return out


def _rows_for_sample() -> List[List[Any]]:
    """한 번의 관측. 값을 하나라도 못 받은 종목은 건너뛴다."""
    coins = list(bithumb.COINS.keys())
    usdt_book = _bithumb_book("USDT")
    if not usdt_book:
        raise RuntimeError("USDT 호가를 받지 못했습니다")
    # 해외 자산을 원화로 환산하는 기준은 USDT/KRW 다 (바이낸스 잔고가 USDT 라서).
    usdt_krw = (usdt_book["bid"] + usdt_book["ask"]) / 2.0

    foreign = _binance_book(coins)
    ts = _seoul_now()
    rows: List[List[Any]] = []

    for c in coins:
        dom = _bithumb_book(c)
        fo = foreign.get(c)
        if not dom or not fo:
            continue
        try:
            krw_last = bithumb.get_price(c)
        except Exception:
            krw_last = (dom["bid"] + dom["ask"]) / 2.0
        usd_last = (fo["bid"] + fo["ask"]) / 2.0

        gross = (krw_last - usd_last * usdt_krw) / (usd_last * usdt_krw) * 100.0
        # 국내에서 팔고(매수호가) 해외에서 산다(매도호가)
        sell_dom = (dom["bid"] - fo["ask"] * usdt_krw) / (fo["ask"] * usdt_krw) * 100.0
        # 해외에서 팔고(매수호가) 국내에서 산다(매도호가)
        sell_for = (fo["bid"] * usdt_krw - dom["ask"]) / dom["ask"] * 100.0

        rows.append([
            ts, c,
            round(dom["bid"], 2), round(dom["ask"], 2), round(krw_last, 2),
            round(fo["bid"], 6), round(fo["ask"], 6), round(usd_last, 6),
            round(usdt_krw, 2),
            round(gross, 4),
            round(sell_dom, 4), round(sell_for, 4),
            round(sell_dom - ROUND_TRIP_FEE_PCT, 4),
            round(sell_for - ROUND_TRIP_FEE_PCT, 4),
        ])
    return rows


def _append(rows: List[List[Any]]) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        w.writerows(rows)


def _trim() -> None:
    """상한을 넘으면 오래된 쪽부터 버린다. 머리글은 남긴다."""
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) - 1 <= MAX_ROWS:
            return
        head, body = lines[0], lines[1:]
        keep = body[-MAX_ROWS:]
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(head)
            f.writelines(keep)
        os.replace(tmp, LOG_FILE)
        logger.info(f"괴리 기록을 {MAX_ROWS:,}행으로 줄였습니다.")
    except Exception as e:
        logger.warning(f"괴리 기록 정리 실패: {e}")


def _loop() -> None:
    global _last_error, _samples
    n = 0
    while not _stop.is_set():
        try:
            rows = _rows_for_sample()
            if rows:
                with _lock:
                    _append(rows)
                    _samples += 1
                _last_error = ""
            n += 1
            if n % 60 == 0:          # 1분 간격이면 1시간마다
                with _lock:
                    _trim()
        except Exception as e:
            _last_error = str(e)
            # 매 실패마다 로그를 쏟지 않는다. 지표는 status() 로 본다.
            if n % 30 == 0:
                logger.warning(f"괴리 기록 실패: {e}")
            n += 1
        _stop.wait(SAMPLE_SEC)


def status() -> Dict[str, Any]:
    with _lock:
        rows = 0
        size = 0
        try:
            size = os.path.getsize(LOG_FILE)
            with open(LOG_FILE, encoding="utf-8") as f:
                rows = max(0, sum(1 for _ in f) - 1)
        except OSError:
            pass
    return {"running": bool(_thread and _thread.is_alive()),
            "samples": _samples, "rows": rows, "bytes": size,
            "sampleSec": SAMPLE_SEC, "maxRows": MAX_ROWS,
            "file": LOG_FILE, "lastError": _last_error}


def start() -> None:
    """서버 기동 시 한 번 부른다. 실패해도 서버를 막지 않는다."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="spread-recorder", daemon=True)
    _thread.start()
    logger.info(f"괴리 기록기 시작 · {SAMPLE_SEC:.0f}초 간격 · {LOG_FILE}")


def stop() -> None:
    _stop.set()
