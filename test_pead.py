#!/usr/bin/env python3
"""국내 200일선 · PEAD 스윙 엔진(모의) 단위 및 API 통합 검증.

실계좌 · 운영 장부(data/)를 건드리지 않도록 임시 폴더로 격리하고,
외부 시세 · 수급 · 재무 · 공시를 가짜 데이터로 주입해 4단계 모든 관문과
시가 이탈 손절 · 20일선 추세 익절 · KOFR 파킹 이자를 검증한다.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

_SANDBOX = tempfile.mkdtemp(prefix="pead-test-")
os.environ["APP_SKIP_DOTENV"] = "1"
os.environ["APP_ACCESS_PASSWORD"] = "pead-test-password-12345"
os.environ["APP_SPREAD_RECORDER"] = "0"
os.environ["APP_SCALPER"] = "0"
os.environ["APP_PEAD"] = "0"

from services import botstore, tradelog  # noqa: E402
botstore._DATA_DIR = _SANDBOX
botstore.STORE_FILE = os.path.join(_SANDBOX, "bots.json")
botstore.arb_store.path = os.path.join(_SANDBOX, "arb_bots.json")
tradelog.LOG_FILE = os.path.join(_SANDBOX, "trades.json")

from services import krx_master, pead as P  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}" + (f"  — {detail}" if detail else ""))


def make_bars(
    base_px: float = 10_000.0,
    n: int = 230,
    last_open: float = 11_200.0,
    last_high: float = 12_100.0,
    last_low: float = 11_150.0,
    last_close: float = 12_000.0,
    prev_close: float = 11_000.0,
    avg_vol: float = 100_000.0,
    last_vol: float = 400_000.0,
    base_20d_ago: float = 10_500.0,
):
    """원하는 200일선 · 20일선 · 당일 캔들 · 거래량 배수를 정확히 만드는 일봉 생성기."""
    bars = []
    start = datetime(2026, 1, 1)
    for i in range(n - 2):
        dt = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        px = base_20d_ago if i == (n - 22) else base_px
        bars.append({"date": dt, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": avg_vol})
    bars.append({
        "date": "2026-11-03", "open": prev_close, "high": prev_close * 1.01,
        "low": prev_close * 0.99, "close": prev_close, "volume": avg_vol,
    })
    bars.append({
        "date": "2026-11-04", "open": last_open, "high": last_high,
        "low": last_low, "close": last_close, "volume": last_vol,
    })
    return bars


# ── 가짜 시장 데이터 주입 ──
BARS_DB = {}
TREND_DB = {}
FIN_DB = {}
DISC_DB = {}

P.fetch_daily_bars = lambda sym, count=230: list(BARS_DB[sym])
P.fetch_investor_trend = lambda code: dict(TREND_DB.get(code, {}))
P.fetch_financial_surprise = lambda code: dict(FIN_DB.get(code, {}))
P.fetch_recent_disclosures = lambda code, lookback_days=7, now=None: dict(DISC_DB.get(code, {}))
P.fetch_dart_today_earnings = lambda now=None: {}

# 기본 강세 지수 (코스피 > 200일선, 코스닥 < 200일선 → 차별화 장세 검증)
BARS_DB["KOSPI"] = make_bars(base_px=5000, prev_close=5600, last_open=5620, last_high=5700, last_low=5600, last_close=5680)
BARS_DB["KOSDAQ"] = make_bars(base_px=800, prev_close=710, last_open=710, last_high=718, last_low=705, last_close=714)
BARS_DB["423160"] = make_bars(base_px=110000, prev_close=110685, last_open=110700, last_high=110705, last_low=110700, last_close=110700, n=30)

print("── 1단계: 시장 200일선 신호등 & 종목 추세 방패 ──")
params = P.PeadParams()
reg = P.compute_market_regime(params)
check("코스피 200일선 상회 · 코스닥 200일선 하회 → 차별화 장세(SPLIT) 판정",
      reg["gate"] == "SPLIT" and reg["kospi"]["bull"] and not reg["kosdaq"]["bull"], reg["gateLabel"])
check("코스피 종목은 진입 허용, 코스닥 종목은 하락장 방어로 차단",
      P.market_allows_entry(reg, "kospi")[0] and not P.market_allows_entry(reg, "kosdaq")[0], "")

good_tech = P.analyze_bars(make_bars())
good_trend = {"foreignNet": 80_000, "instNet": 50_000, "smartNet3d": 200_000}
good_fin = {"available": True, "quarter": "2026.09", "opProfitEok": 850, "revEok": 5200,
            "netProfitEok": 680, "opYoyPct": 62.5, "revYoyPct": 24.0, "opQoqPct": 18.0,
            "turnaround": False, "oneOffTrap": False}
clean_disc = {"earnings": {"title": "영업(잠정)실적(공정공시)", "date": "2026-11-04", "time": "14:10", "afterHours": False},
              "catalyst": None, "toxic": None}

c_pass = P.evaluate_candidate("000100", "우량반도체", "kospi", good_tech, good_trend, good_fin, clean_disc, reg, params)
check("4단계 전 조건 충족 시 최종 통과(passed=True)", c_pass["passed"] and c_pass["score"] >= 90, f"점수 {c_pass['score']}")

c_kosdaq_block = P.evaluate_candidate("000200", "코스닥성장", "kosdaq", good_tech, good_trend, good_fin, clean_disc, reg, params)
check("개별 종목이 완벽해도 소속 지수(코스닥)가 200일선 아래면 매수 차단", not c_kosdaq_block["passed"], c_kosdaq_block["failReasons"][0])

below200_tech = P.analyze_bars(make_bars(base_px=15_000, prev_close=11_000, last_open=11_200, last_high=12_100, last_low=11_150, last_close=12_000))
c_below200 = P.evaluate_candidate("000101", "역배열주", "kospi", below200_tech, good_trend, good_fin, clean_disc, reg, params)
check("개별 주가가 200일선 아래(역배열)면 실적·거래량이 터져도 차단", not c_below200["passed"], c_below200["failReasons"][0])

overheated_tech = P.analyze_bars(make_bars(base_20d_ago=8_000, prev_close=11_000))   # 사전 20일 +37.5% 급등
c_overheat = P.evaluate_candidate("000102", "선반영급등주", "kospi", overheated_tech, good_trend, good_fin, clean_disc, reg, params)
check("발표 전 20일간 이미 +25% 초과 급등한 종목은 '뉴스에 팔아라' 함정으로 차단", not c_overheat["passed"], c_overheat["failReasons"][0])

print("── 2단계 & 3단계: 가짜 서프라이즈 · 유상증자 · 피뢰침 설거지 · 개인 단독매수 차단 ──")
trap_fin = dict(good_fin, opProfitEok=-50, netProfitEok=400, oneOffTrap=True)
c_trap = P.evaluate_candidate("000103", "일회성착시주", "kospi", good_tech, good_trend, trap_fin, clean_disc, reg, params)
check("영업적자인데 당기순이익만 흑자인 종목(일회성 처분이익 착시) 차단", not c_trap["passed"], c_trap["failReasons"][0])

toxic_disc = dict(clean_disc, toxic="타법인 증권 취득자금 조달을 위한 유상증자 결정")
c_toxic = P.evaluate_candidate("000104", "유상증자주", "kospi", good_tech, good_trend, good_fin, toxic_disc, reg, params)
check("유상증자·전환사채 등 악성 희석 공시 종목 자동 차단", not c_toxic["passed"], c_toxic["failReasons"][0])

wick_tech = P.analyze_bars(make_bars(last_open=11_200, last_high=13_200, last_low=11_100, last_close=11_800))  # 윗꼬리 66%
c_wick = P.evaluate_candidate("000105", "윗꼬리설거지", "kospi", wick_tech, good_trend, good_fin, clean_disc, reg, params)
check("윗꼬리가 긴 피뢰침 캔들(고점 물량 떠넘기기) 차단", not c_wick["passed"], c_wick["failReasons"][0])

retail_trend = {"foreignNet": -60_000, "instNet": -40_000, "retailNet": 100_000}
c_retail = P.evaluate_candidate("000106", "개미단독매수", "kospi", good_tech, retail_trend, good_fin, clean_disc, reg, params)
check("외인·기관 동반 순매도(개인 단독 매수) 종목 차단", not c_retail["passed"], c_retail["failReasons"][0])

print("── 4단계: 슬롯 포트폴리오 · KOFR 파킹 · 시가 손절 · 20일선 추세 익절 ──")
UNIVERSE = [
    {"code": "000100", "name": "우량반도체", "market": "kospi", "kospi200": True, "kosdaq150": False},
    {"code": "000105", "name": "윗꼬리설거지", "market": "kospi", "kospi200": True, "kosdaq150": False},
]
krx_master.load = lambda: UNIVERSE
krx_master.candidates = lambda rows: rows
BARS_DB["000100"] = make_bars()
TREND_DB["000100"] = good_trend
FIN_DB["000100"] = good_fin
DISC_DB["000100"] = clean_disc
BARS_DB["000105"] = make_bars(last_open=11_200, last_high=13_200, last_low=11_100, last_close=11_800)
TREND_DB["000105"] = good_trend
FIN_DB["000105"] = good_fin
DISC_DB["000105"] = clean_disc

eng = P.PeadEngine(namuh_account=None)
t_scan = datetime(2026, 11, 4, 15, 11, 0, tzinfo=P.KST)
eng.step(t_scan)
check("15:10 스캔 후 통과 종목이 SEMI 승인 대기열(pending)에 등록된다",
      len(eng.pending) == 1 and eng.pending[0]["code"] == "000100", f"대기 {len(eng.pending)}건")
check("SEMI 모드에서는 사용자 승인 전까지 자동으로 사지 않는다", len(eng.positions) == 0, "")
check("미달 종목도 사유와 함께 상위 관심주(watchlist)에 표시된다", len(eng.watchlist) == 2, "")

ok, msg = eng.buy_candidate("000100", now=datetime(2026, 11, 4, 15, 15, 0, tzinfo=P.KST))
pos = eng.positions.get("000100")
check("1클릭 승인 시 시드 20%(200만원) 슬롯 매수 & 기준봉 시가(11,200원)로 손절선 설정",
      ok and pos is not None and pos["stopPrice"] == 11_200 and 1_900_000 <= pos["cost"] <= 2_000_000,
      f"수량 {pos['qty']}주 · 손절선 {pos['stopPrice']:,}원")

# 다음 날 유휴 4슬롯(800만원)에 대한 KOFR 파킹 이자 적립 확인
eng.accrue_parking("2026-11-05")
check("남은 빈 슬롯(약 800만원)에 KOFR 파킹 이자가 적립된다",
      eng.parking["accruedKrw"] > 0, f"+{eng.parking['accruedKrw']:,.1f}원")

# 보유 중 20일선 위 상승 흐름에서는 팔지 않고 추세를 끝까지 탄다
BARS_DB["000100"] = make_bars(base_px=11_500, prev_close=12_800, last_open=13_000, last_high=13_600, last_low=12_900, last_close=13_500)
eng.check_positions(datetime(2026, 11, 10, 15, 20, 0, tzinfo=P.KST), is_close_window=True)
check("주가가 20일선 위에서 상승 중일 때는 성급히 팔지 않고 추세를 유지한다", "000100" in eng.positions, f"보유 {eng.positions['000100']['holdDays']}일")

# 20일선(약 12,800원) 아래로 종가가 꺾이면(12,100원) 추세 익절(TRAIL_MA20)
BARS_DB["000100"] = make_bars(base_px=13_000, prev_close=13_200, last_open=12_500, last_high=12_600, last_low=12_000, last_close=12_200)
eng.check_positions(datetime(2026, 11, 25, 15, 20, 0, tzinfo=P.KST), is_close_window=True)
last_trade = eng.trades[-1]
check("일봉 종가가 20일 이동평균선을 이탈하면 전량 추세 익절(TRAIL_MA20)한다",
      "000100" not in eng.positions and last_trade["kind"] == "TRAIL_MA20" and last_trade["pnl"] > 0,
      f"{last_trade['kind']} · 손익 {last_trade['pnl']:+,.0f}원 ({last_trade['pct']:+.2f}%)")

# 기준봉 시가 이탈 즉시 손절(STOP_LOSS) 검증 (AUTO 모드 함께 검증)
# 평일 휴장일: 코스피 마지막 일봉이 오늘이 아니면 스캔 · 종가 점검 · 자동 매수를 쉰다
eng_h = P.PeadEngine(namuh_account=None)
eng_h.update_params({"approvalMode": "AUTO"})
eng_h.step(datetime(2026, 11, 5, 15, 11, 0, tzinfo=P.KST))      # 가짜 일봉은 11/04 가 마지막
eng_h.step(datetime(2026, 11, 5, 15, 19, 0, tzinfo=P.KST))
check("평일 휴장일(오늘 일봉 없음)에는 어제 봉으로 스캔 · 자동 매수하지 않는다",
      not eng_h.positions and not eng_h.pending and eng_h.scan_meta.get("lastScanDate") == "2026-11-05"
      and any("휴장일" in l["message"] for l in eng_h.logs), "")
check("일봉을 못 받으면 연 날로 본다 (평소처럼 돈다)",
      (lambda e: (setattr(P, "fetch_daily_bars", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))),
                  e.is_trading_day("2026-11-05"))[1])(P.PeadEngine(namuh_account=None)), "")
P.fetch_daily_bars = lambda sym, count=230: list(BARS_DB[sym])

eng2 = P.PeadEngine(namuh_account=None)
eng2.update_params({"approvalMode": "AUTO"})
eng2._trading_day = {"2026-11-06": True, "2026-11-09": True}   # 가짜 일봉 날짜와 상관없이 장이 열린 날로
BARS_DB["000100"] = make_bars()  # 시가 11,200 / 종가 12,000
eng2.step(datetime(2026, 11, 6, 15, 11, 0, tzinfo=P.KST))
eng2.step(datetime(2026, 11, 6, 15, 19, 0, tzinfo=P.KST))
check("AUTO 모드는 15:18 에 승인 대기 시그널을 빈 슬롯에 자동 매수한다", "000100" in eng2.positions, "")

# 다음 거래일 장중(10:30)에 시가 손절선(11,200원) 아래인 11,050원으로 급락
BARS_DB["000100"] = make_bars(last_open=11_400, last_high=11_450, last_low=11_000, last_close=11_050)
eng2.step(datetime(2026, 11, 9, 10, 30, 0, tzinfo=P.KST))
check("장중이라도 실적 기준봉 시가(손절선)를 깨면 지체 없이 STOP_LOSS 손절한다",
      "000100" not in eng2.positions and eng2.trades[-1]["kind"] == "STOP_LOSS",
      f"{eng2.trades[-1]['kind']} @ {eng2.trades[-1]['price']:,.0f}원")

# 영속화 복원 검증
eng.save()
restored = P.PeadEngine(namuh_account=None)
restored.load()
check("재시작 후에도 체결 기록과 KOFR 파킹 이자가 그대로 복원된다",
      len(restored.trades) == len(eng.trades) and restored.parking["accruedKrw"] == eng.parking["accruedKrw"], "")

print("── 4-B단계: 남은 빈 슬롯 × 2배수 추천 상한 & [제외] 후보 승격 검증 ──")
UNIVERSE_15 = []
for idx in range(1, 16):
    cd = f"001{idx:03d}"
    UNIVERSE_15.append({"code": cd, "name": f"실적우량{idx:02d}", "market": "kospi", "kospi200": True, "kosdaq150": False})
    BARS_DB[cd] = make_bars(last_vol=350_000 + idx * 10_000)
    TREND_DB[cd] = good_trend
    FIN_DB[cd] = dict(good_fin, opYoyPct=30.0 + idx * 3.0)
    DISC_DB[cd] = clean_disc

krx_master.load = lambda: UNIVERSE_15
eng_cap = P.PeadEngine(namuh_account=None)
st0 = eng_cap.run_scan(datetime(2026, 11, 4, 15, 11, 0, tzinfo=P.KST))
check("빈 슬롯 5칸일 때 전종목 후보가 15개여도 정확히 10개(5칸 × 2배수)만 조회된다",
      st0["summary"]["idleSlots"] == 5 and len(st0["recommendations"]) == 10 and st0["summary"]["recommendationLimit"] == 10,
      f"조회 {len(st0['recommendations'])}개 / 한도 {st0['summary']['recommendationLimit']}개")

for pick in [r["code"] for r in st0["recommendations"][:3]]:
    eng_cap.buy_candidate(pick, now=datetime(2026, 11, 4, 15, 13, 0, tzinfo=P.KST))
st3 = eng_cap.status()
check("3개 슬롯을 채우면(남은 슬롯 2칸) 추천 기업 수가 자동으로 4개(2칸 × 2배수)로 줄어든다",
      st3["summary"]["usedSlots"] == 3 and st3["summary"]["idleSlots"] == 2 and len(st3["recommendations"]) == 4,
      f"보유 {st3['summary']['usedSlots']}칸 · 남은 {st3['summary']['idleSlots']}칸 → 추천 {len(st3['recommendations'])}개")

drop_code = st3["recommendations"][0]["code"]
eng_cap.reject_candidate(drop_code)
st_rej = eng_cap.status()
check("[제외] 클릭 시 해당 기업이 빠지고 예비 풀의 다음 순위 후보가 올라와 4개를 유지한다",
      len(st_rej["recommendations"]) == 4 and all(r["code"] != drop_code for r in st_rej["recommendations"]),
      f"제외 {drop_code} → 새 1순위 {st_rej['recommendations'][0]['code']}")

for pick in [r["code"] for r in st_rej["recommendations"][:2]]:
    eng_cap.buy_candidate(pick, now=datetime(2026, 11, 4, 15, 15, 0, tzinfo=P.KST))
st5 = eng_cap.status()
check("5개 슬롯이 모두 차면(남은 슬롯 0칸) 추천 기업 조회 수는 0개가 된다",
      st5["summary"]["idleSlots"] == 0 and len(st5["recommendations"]) == 0,
      f"보유 {st5['summary']['usedSlots']}/5칸 → 추천 {len(st5['recommendations'])}개")

one_held = st5["positions"][0]["code"]
eng_cap.close_position(one_held, now=datetime(2026, 11, 4, 15, 17, 0, tzinfo=P.KST))
st4 = eng_cap.status()
check("보유 종목 1개를 청산해 빈 슬롯 1칸이 생기면 재스캔 없이도 즉시 2개(1칸 × 2배수)가 다시 열린다",
      st4["summary"]["idleSlots"] == 1 and len(st4["recommendations"]) == 2,
      f"남은 {st4['summary']['idleSlots']}칸 → 추천 {len(st4['recommendations'])}개")

# 조용한 장세(전 종목 거래량 1.0배 · 등락 +0.5%)에서도 2순위 백업 풀로 '남은 슬롯 × 2배수(10개)' 추천이 끊기지 않는지 검증
UNIVERSE_CALM = []
for idx in range(1, 13):
    cd = f"002{idx:03d}"
    UNIVERSE_CALM.append({"code": cd, "name": f"정배열안정{idx:02d}", "market": "kospi", "kospi200": True, "kosdaq150": False})
    BARS_DB[cd] = make_bars(last_open=11_000, last_high=11_120, last_low=10_980, last_close=11_055, prev_close=11_000, last_vol=105_000)
    TREND_DB[cd] = good_trend
    FIN_DB[cd] = dict(good_fin, opYoyPct=28.0 + idx)
    DISC_DB[cd] = clean_disc

krx_master.load = lambda: UNIVERSE_CALM
eng_calm = P.PeadEngine(namuh_account=None)
st_calm = eng_calm.run_scan(datetime(2026, 11, 5, 15, 11, 0, tzinfo=P.KST))  # 11/05 는 가짜 일봉 기준 평일 휴장일
check("거래량 폭발주가 없는 조용한 장세에도 200일선 정배열 백업 풀로 10개(5칸 × 2배수) 추천을 채운다",
      len(st_calm["recommendations"]) == 10,
      f"1차 통과 {st_calm['scanMeta']['stage1Passed']}건 · 최종 추천 {len(st_calm['recommendations'])}개")
check("평일 휴장일에 수동 스캔을 돌려도 KOFR 파킹 이자를 중복 적립하지 않는다",
      eng_calm.parking["accruedKrw"] == 0.0,
      f"휴장일 파킹 이자 {eng_calm.parking['accruedKrw']}원")

print("── 5단계: FastAPI 엔드포인트 · 인증 · CSRF 검증 ──")
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import requests  # noqa: E402
import uvicorn  # noqa: E402
import server  # noqa: E402

P._engine = eng
with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    _port = _s.getsockname()[1]

_cfg = uvicorn.Config(server.app, host="127.0.0.1", port=_port, log_level="error")
_srv = uvicorn.Server(_cfg)
_th = threading.Thread(target=_srv.run, daemon=True)
_th.start()
for _ in range(50):
    try:
        if requests.get(f"http://127.0.0.1:{_port}/api/health", timeout=0.5).ok:
            break
    except Exception:
        time.sleep(0.1)

try:
    base = f"http://127.0.0.1:{_port}"
    r_no_auth = requests.get(f"{base}/api/namuh/pead", timeout=5)
    check("로그인 전 /api/namuh/pead 조회는 401 로 차단한다", r_no_auth.status_code == 401, f"HTTP {r_no_auth.status_code}")

    r_no_csrf = requests.post(f"{base}/api/auth/login", json={"password": os.environ["APP_ACCESS_PASSWORD"]}, timeout=5)
    check("CSRF 헤더 없는 POST 는 403 으로 거절한다", r_no_csrf.status_code == 403, f"HTTP {r_no_csrf.status_code}")

    sess = requests.Session()
    sess.headers.update({"X-Requested-With": "alpha-console"})
    r_login = sess.post(f"{base}/api/auth/login", json={"password": os.environ["APP_ACCESS_PASSWORD"]}, timeout=5)
    check("정상 로그인 통과", r_login.status_code == 200, "")

    r_st = sess.get(f"{base}/api/namuh/pead", timeout=5)
    st_json = r_st.json()
    check("GET /api/namuh/pead 상태 · 파킹 · 수익 요약 정상 반환",
          r_st.status_code == 200 and st_json["paper"] is True and st_json["summary"]["closedTrades"] == 1,
          f"자산 {st_json['summary']['totalEquityKrw']:,}원")

    r_pm = sess.post(f"{base}/api/namuh/pead/params", json={"maxSlots": 4, "candidateMultiplier": 3, "minVolRatio": 2.5}, timeout=5)
    check("POST /api/namuh/pead/params 슬롯·추천배수 파라미터 변경 반영",
          r_pm.status_code == 200
          and r_pm.json()["params"]["maxSlots"] == 4
          and r_pm.json()["params"]["candidateMultiplier"] == 3
          and r_pm.json()["summary"]["recommendationLimit"] == 12, "")
finally:
    _srv.should_exit = True
    _th.join(timeout=3)

print(f"\n{'=' * 58}")
if FAIL:
    print(f"통과 {len(PASS)}개 · 실패 {len(FAIL)}개")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print(f"전체 {len(PASS)}개 항목 통과")
