#!/usr/bin/env python3
"""ORB 백테스트용 과거 시세 받기 — 나무증권 1분봉 · 일봉 (서버에서 돌린다).

  후보   코스피200 ∪ 코스닥150 (krx_master · 스캐너와 같은 후보) + 지수 ETF 2개
  1분봉  /krstock/quote/v1/period gubun 5 · xtick 1 · 한 번에 5,000봉(약 9일)
         2026-09-28 실측: 분봉은 2026-08-13 부터만 남아 있다(약 6주). 그보다
         예전은 빈 응답이다. 1분봉에는 NXT 시간외(15:30~20:00)가 섞여 오므로
         09:00~15:30 봉만 남긴다(정규장 KRX 거래량은 야후와 맞았다).
  일봉   gubun 1 · 60개 — 전일 종가 · 고가 · 저가 (갭 · 변동성 돌파 목표가)

저장  data/bt_cache/<코드>.json.gz   (이미 있으면 건너뛴다 — 끊겨도 이어 받는다)

해외 봇과 같은 앱 키를 쓴다. 호출 간격을 봇(1.1초)보다 넉넉히 1.6초로 두고,
토큰 만료가 10분 안으로 다가오면 멈춘다(만료 전 재발급 금지 — 봇이 갱신하게 둔다).

  ssh <서버> "cd /opt/gemini_alpha_lab && sudo -u bithumb env \\$(grep -E '^NAMUH_' .env | xargs) \\
             nohup venv/bin/python scripts/orb_bt_fetch.py > data/bt_cache/fetch.log 2>&1 &"
"""

import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

ROOT = os.getenv("GAL_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import krx, krx_master, namuh      # noqa: E402
from services.keystore import namuh_keystore     # noqa: E402

OUT = os.path.join(ROOT, "data", "bt_cache")
FIRST_DAY = "20260813"          # 분봉이 남아 있는 첫날 (실측)
GAP_SEC = 1.6
_last = [0.0]


def _token_ok() -> bool:
    try:
        with open(namuh.TOKEN_FILE, encoding="utf-8") as f:
            exp = float(json.load(f).get("expiresAt") or 0)
    except (OSError, ValueError):
        return False
    return exp - time.time() > 600


def _period(acc, code, gubun, xtick, cnt, edate):
    wait = GAP_SEC - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    res, b = krx._post(acc, namuh.BASE_URL, "/krstock/quote/v1/period", {
        "iem_cd": code, "market_cd": "KRX", "gubun": gubun, "xtick": xtick,
        "array_cnt": str(cnt), "edate": edate, "maxavg": "5", "today_cls_code": "0"}, read=True)
    if res.status_code != 200 or str(b.get("rsp_cd")) != "00000":
        raise RuntimeError(f"{code} 기간 시세 실패 {res.status_code} {b.get('rsp_cd')} {b.get('rsp_msg')}")
    return b.get("Output_1") or []


def _i(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def fetch(acc, code, today):
    daily = [{"d": r["bsop_date"], "o": _i(r["stck_oprc"]), "h": _i(r["stck_hgpr"]),
              "l": _i(r["stck_lwpr"]), "c": _i(r["stck_prpr"]), "v": _i(r["vol"])}
             for r in _period(acc, code, "1", "0", 60, today) if r.get("bsop_date")]
    m1 = {}
    edate = today
    for _ in range(8):
        rows = _period(acc, code, "5", "1", 5000, edate)
        if not rows:
            break
        for r in rows:
            d, t = str(r.get("bsop_date") or ""), str(r.get("bsop_time") or "")
            if not d or not ("090100" <= t <= "153000"):
                continue
            m1.setdefault(d, {})[t[:4]] = [_i(r["stck_oprc"]), _i(r["stck_hgpr"]), _i(r["stck_lwpr"]),
                                           _i(r["stck_prpr"]), _i(r["vol"]), _i(r["tr_pbmn"])]
        oldest = min(str(r.get("bsop_date")) for r in rows if r.get("bsop_date"))
        if oldest <= FIRST_DAY:
            break
        edate = (datetime.strptime(oldest, "%Y%m%d").date() - timedelta(days=1)).strftime("%Y%m%d")
    # 가장 오래된 날은 5,000봉 경계에서 잘려 있을 수 있다 → 09:01 봉이 없는 날은 버린다
    m1 = {d: v for d, v in m1.items() if "0901" in v}
    return {"code": code, "daily": sorted(daily, key=lambda x: x["d"]),
            "m1": {d: sorted([[t] + v for t, v in bars.items()]) for d, bars in sorted(m1.items())}}


def main():
    acc = namuh_keystore.account
    if not acc.configured:
        sys.exit("나무증권 키가 없습니다")
    os.makedirs(OUT, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    cands = krx_master.candidates(krx_master.load())
    meta = {c["code"]: {"name": c["name"], "market": c["market"], "capEok": c["capEok"]} for c in cands}
    for code, name in (("069500", "KODEX 200"), ("229200", "KODEX 코스닥150")):
        meta[code] = {"name": name, "market": "index", "capEok": 0}
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    codes = ["069500", "229200"] + [c["code"] for c in sorted(cands, key=lambda c: -c["capEok"])]
    if len(sys.argv) > 1:                      # 시험용: 앞에서 몇 종목만
        codes = codes[:int(sys.argv[1])]
    t0, done, fails = time.time(), 0, 0
    for i, code in enumerate(codes, 1):
        path = os.path.join(OUT, f"{code}.json.gz")
        if os.path.exists(path):
            continue
        if not _token_ok():
            print("토큰 만료가 10분 안이라 멈춥니다 — 봇이 갱신한 뒤 다시 돌리면 이어 받습니다", flush=True)
            break
        try:
            data = fetch(acc, code, today)
        except Exception as e:
            fails += 1
            print(f"[{i}/{len(codes)}] {code} 실패: {e}", flush=True)
            continue
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        done += 1
        if done % 10 == 0 or i == len(codes):
            print(f"[{i}/{len(codes)}] {code} {meta[code]['name']} · {len(data['m1'])}일 · "
                  f"{time.time() - t0:.0f}초", flush=True)
    print(f"끝 — 받음 {done} · 실패 {fails} · {time.time() - t0:.0f}초", flush=True)


if __name__ == "__main__":
    main()
