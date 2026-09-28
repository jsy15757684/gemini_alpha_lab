"""나무증권 국내주식 종목 마스터 (m_new_stock.mst) — ORB 자동 선정의 후보군.

  https://www.nhplug.com/instruments/m_new_stock.mst   (인증 없음 · 공개)
  레코드 237바이트 고정폭 · CP949 · 우측 공백 · 끝 1바이트 LF · 파일 헤더 없음
  레이아웃: https://www.nhplug.com/instruments/m_new_stock.h (nhplug-sdk instruments/)

후보 = 코스피200 ∪ 코스닥150 (2026-09-28 실측 201 + 150). 다음은 뺀다:
  관리종목 · 거래정지 · 정리매매 · 투자경고/위험(alert_gb 2~5) · 단기과열 지정(2·3)
투자주의(1)는 남긴다 — 경고 전 단계라 흔하고, 오히려 '그날 움직이는 종목' 이 많다.

하루 한 번 받아 data/ 에 둔다. 못 받으면 전날 파일을 쓴다(구성종목은 반기마다 바뀐다).
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

URL = (os.getenv("NAMUH_MASTER_URL") or "https://www.nhplug.com/instruments/m_new_stock.mst").strip()
RECORD = 237
ENC = "cp949"
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CACHE_FILE = os.path.join(_DATA_DIR, "krx_master.mst")
MAX_AGE_SEC = 20 * 3600

# (이름, 시작, 길이) — m_new_stock.h 에서 쓰는 칸만
FIELDS = {
    "code": (0, 6), "market": (6, 1), "name": (7, 41), "prevClose": (152, 7),
    "under": (160, 1), "stop": (161, 1), "k200": (168, 1), "q150": (172, 1),
    "capEok": (174, 12), "shortOver": (187, 1), "alert": (188, 1), "sltr": (189, 1),
}


def parse(raw: bytes) -> List[Dict[str, Any]]:
    if len(raw) % RECORD:
        raise ValueError(f"종목 마스터 크기({len(raw)})가 레코드({RECORD})의 배수가 아닙니다 — 형식이 바뀌었을 수 있습니다")
    rows = []
    for i in range(0, len(raw), RECORD):
        rec = raw[i:i + RECORD]
        g = {k: rec[o:o + n].decode(ENC, "replace").rstrip() for k, (o, n) in FIELDS.items()}
        name = g["name"]
        # 이름 첫 바이트가 표시다: * 코스피200 · # 코스닥150 (정렬·표시 전에 뗀다)
        mark = name[:1] if name[:1] in "*#" else ""
        rows.append({
            "code": g["code"].strip(),
            "name": (name[1:] if mark else name).strip(),
            "market": "kospi" if g["market"] == "1" else ("kosdaq" if g["market"] == "4" else g["market"]),
            "kospi200": g["k200"] not in ("", "0"),
            "kosdaq150": g["q150"] == "Y",
            "prevClose": int(g["prevClose"] or 0) if g["prevClose"].isdigit() else 0,
            "capEok": int(g["capEok"] or 0) if g["capEok"].isdigit() else 0,
            "under": g["under"] == "Y", "stop": g["stop"] == "Y", "sltr": g["sltr"] == "Y",
            "alert": g["alert"] or "0", "shortOver": g["shortOver"] or "0",
        })
    return rows


def excluded_reason(r: Dict[str, Any]) -> Optional[str]:
    if r["stop"]:
        return "거래정지"
    if r["under"]:
        return "관리종목"
    if r["sltr"]:
        return "정리매매"
    if r["alert"] in ("2", "3", "4", "5"):
        return "투자경고·위험"
    if r["shortOver"] in ("2", "3"):
        return "단기과열"
    return None


def candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if (r["kospi200"] or r["kosdaq150"]) and not excluded_reason(r)]


def load(max_age_sec: float = MAX_AGE_SEC) -> List[Dict[str, Any]]:
    """캐시가 신선하면 그것을, 아니면 받아서. 받기 실패면 오래된 캐시라도 쓴다."""
    fresh = os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < max_age_sec
    if not fresh:
        try:
            res = requests.get(URL, timeout=20)
            res.raise_for_status()
            rows = parse(res.content)          # 형식이 맞는지 먼저 본다
            os.makedirs(_DATA_DIR, exist_ok=True)
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(res.content)
            os.replace(tmp, CACHE_FILE)
            logger.info(f"종목 마스터를 받았습니다 ({len(rows):,}종목)")
            return rows
        except Exception as e:
            if not os.path.exists(CACHE_FILE):
                raise RuntimeError(f"종목 마스터를 받지 못했고 저장본도 없습니다: {e}")
            logger.warning(f"종목 마스터를 받지 못해 저장본을 씁니다: {e}")
    with open(CACHE_FILE, "rb") as f:
        return parse(f.read())
