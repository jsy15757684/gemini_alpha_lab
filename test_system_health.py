"""빗썸 원화 자동매매 콘솔 — 전체 점검.

실행: APP_ACCESS_PASSWORD='...' python3 test_system_health.py
실패한 항목이 있으면 목록과 함께 0 이 아닌 종료 코드를 반환한다.
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8888")
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))
FAILURES = []


def call(name, path, method="GET", payload=None, expect=200):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"} if payload is not None else {})
    req.get_method = lambda: method
    try:
        res = OPENER.open(req, timeout=30)
        code, body = res.getcode(), json.loads(res.read() or b"null")
    except urllib.error.HTTPError as e:
        code, body = e.code, None
        try:
            body = json.loads(e.read())
        except Exception:
            pass
    except Exception as e:
        print(f"❌ {name}: {e}")
        FAILURES.append(name)
        return None
    if code != expect:
        detail = (body or {}).get("detail") if isinstance(body, dict) else body
        print(f"❌ {name}: HTTP {code} (기대 {expect}) {str(detail)[:90]}")
        FAILURES.append(name)
        return None
    print(f"✅ {name}")
    return body


def main():
    print("=" * 58)
    print("  빗썸 원화 자동매매 콘솔 — 전체 점검")
    print("=" * 58 + "\n")

    pw = (os.getenv("APP_ACCESS_PASSWORD") or "").strip()
    if not pw:
        print("❌ APP_ACCESS_PASSWORD 환경변수가 없어 진행할 수 없습니다.")
        return 2

    call("00. 헬스체크", "/api/health")
    call("01. 로그인 전에는 데이터 API 차단", "/api/prices", expect=401)

    if call("02. 로그인", "/api/auth/login", "POST", {"password": pw}) is None:
        print("\n로그인 실패로 이후 점검을 중단합니다.")
        return 2

    meta = call("03. 코인·간격 목록", "/api/coins")
    if meta:
        print(f"    코인 {len(meta['coins'])}종 · 간격 {len(meta['intervals'])}종")

    prices = call("04. 5종 실시간 시세", "/api/prices")
    if prices:
        ok = [p for p in prices["prices"] if "error" not in p]
        print(f"    수신 {len(ok)}/{len(prices['prices'])}종")
        for p in ok[:2]:
            print(f"      {p['coin']} {p['price']:,.0f}원 ({p['changePercent']:+.2f}%)")
        if len(ok) < len(prices["prices"]):
            FAILURES.append("04. 일부 종목 시세 실패")

    c = call("05. 캔들+지표", "/api/candles?coin=BTC&interval=24h")
    if c:
        ready = [b for b in c["candles"] if b["ready"]]
        print(f"    캔들 {len(c['candles'])}개 · 지표 유효 {len(ready)}개")
        if not ready:
            FAILURES.append("05. 지표가 하나도 계산되지 않음")

    bt = call("06. 백테스트", "/api/backtest", "POST",
              {"coin": "BTC", "interval": "24h", "initialKrw": 1000000})
    if bt:
        print(f"    수익률 {bt['totalReturnPct']:+.2f}% · 벤치마크 {bt['benchmarkReturnPct']:+.2f}%"
              f" · 거래 {bt['totalTrades']}회")

    bot = call("07. 모의투자 봇 가동", "/api/bot/deploy", "POST",
               {"coin": "BTC", "interval": "24h", "mode": "PAPER", "capitalKrw": 1000000})
    bot_id = bot.get("botId") if bot else None
    if bot:
        print(f"    {bot['botId']} · 현재가 {bot['currentPrice']:,.0f}원")

    call("08. 실시세 없는 코인은 거부", "/api/bot/deploy", "POST",
         {"coin": "NOTACOIN", "mode": "PAPER", "capitalKrw": 1000000}, expect=400)

    lst = call("09. 봇 목록", "/api/bot/list")
    if lst:
        print(f"    가동 {lst['activeCount']}/{lst['maxActive']}")

    if bot_id:
        call("10. 봇 정지", "/api/bot/stop", "POST", {"botId": bot_id})
        call("11. 봇 삭제", "/api/bot/delete", "POST", {"botId": bot_id})

    acc = call("12. 빗썸 계정 상태", "/api/account")
    if acc:
        print(f"    연동 {acc['connected']} · 보관 {acc['source']}"
              + (f" · 인증 {'성공' if acc.get('balanceOk') else '실패'}" if acc["connected"] else ""))

    ip = call("13. 등록용 공인 IP", "/api/system/egress_ip")
    if ip:
        print(f"    {ip.get('registerThisIp') or '확인 불가'}")

    gem = call("14. Gemini 설정 상태", "/api/gemini/status")
    if gem:
        print(f"    연동 {gem['configured']} · 모델 {gem['model']}")

    call("15. 로그아웃", "/api/auth/logout", "POST")
    call("16. 로그아웃 후 차단 확인", "/api/prices", expect=401)

    print("\n" + "=" * 58)
    if FAILURES:
        print(f"  ❌ {len(FAILURES)}개 실패")
        for f in FAILURES:
            print(f"     · {f}")
    else:
        print("  ✅ 전체 정상")
    print("=" * 58)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
