import urllib.request
import urllib.error
import http.cookiejar
import json
import os
import time

FAILURES = []

# 인증이 붙었으므로 세션 쿠키를 유지하는 opener 를 쓴다.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def login():
    """APP_ACCESS_PASSWORD 로 로그인해 세션 쿠키를 확보한다."""
    pw = (os.getenv("APP_ACCESS_PASSWORD") or "").strip()
    if not pw:
        print("❌ APP_ACCESS_PASSWORD 환경변수가 없어 점검을 진행할 수 없습니다.")
        print("   실행 예: APP_ACCESS_PASSWORD='...' python3 test_system_health.py")
        return False
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/auth/login",
            data=json.dumps({"password": pw}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        _OPENER.open(req, timeout=10)
        print("🔐 인증 성공 — 세션으로 점검을 진행합니다.\n")
        return True
    except urllib.error.HTTPError as e:
        print(f"❌ 로그인 실패 (HTTP {e.code}): 비밀번호가 서버 설정과 일치하는지 확인하세요.\n")
        return False
    except Exception as e:
        print(f"❌ 서버에 연결할 수 없습니다: {e}\n")
        return False

BASE_URL = 'http://localhost:8888'

def test_api(name, url, method='GET', data=None):
    try:
        req = urllib.request.Request(
            f'{BASE_URL}{url}',
            data=json.dumps(data).encode('utf-8') if data else None,
            headers={'Content-Type': 'application/json'} if data else {}
        )
        req.get_method = lambda: method
        res = _OPENER.open(req, timeout=10)
        body = json.loads(res.read())
        print(f'✅ [{name}] 정상 응답 (HTTP {res.getcode()})')
        return body
    except Exception as e:
        print(f'❌ [{name}] 오류: {e}')
        FAILURES.append(f'{name}: {e}')
        return None

def run_diagnostics():
    print('===========================================================')
    print('  🔍 GEMINI ALPHA LAB 전체 시스템 무결성 & 연동 종합 점검')
    print('===========================================================\n')

    if not login():
        return 2

    # 1. 인기 자산 목록
    pop = test_api('1. 인기 자산 목록 API', '/api/popular')

    # 2. 통합 번들 (BTC)
    bundle = test_api('2. 종목 데이터 & AI 감성 번들 API (BTC)', '/api/symbol/bundle?symbol=BTC-USD')
    if bundle:
        q = bundle.get('quote', {})
        s = bundle.get('sentiment', {})
        print(f'   ↳ BTC 실시간 시세: ${q.get("currentPrice", 0):,.2f} | Gemini 감성 점수: {s.get("sentimentScore")}점 ({s.get("sentiment")})')

    # 3. 6대 월가 구루
    gurus = test_api('3. 월가 전설의 6대 구루 인텔리전스 API', '/api/gurus')
    if gurus:
        print(f'   ↳ 등록된 월가 구루: {len(gurus.get("gurus", []))}명 (버핏, 린치, 시몬스, 달리오 등)')

    # 4. 상용 봇 마켓플레이스
    market = test_api('4. 봇 마켓플레이스 & 랭킹 리더보드 API', '/api/marketplace/bots')
    if market:
        print(f'   ↳ 등록된 상용 봇: {len(market.get("bots", []))}개 (인피니티 그리드, DCA 마틴게일 등)')

    # 5. 브로커 센터 (정예화된 3대 브로커)
    brokers = test_api('5. 3대 핵심 브로커 연동 센터 API', '/api/broker/list')
    if brokers:
        names = [b['name'] for b in brokers.get('brokers', [])]
        print(f'   ↳ 현재 브로커: {" | ".join(names)}')

    # 6. 봇 배포 테스트 (Bithumb 연동)
    bot_payload = {
        'symbol': 'BTC',
        'mode': 'PAPER',
        'broker': 'BITHUMB',
        'capital': 1000000.0,
        'strategyParams': {
            'enableVolumeSurge': True,
            'enableAiSentimentGate': True,
            'minSentimentScore': 60,
            'enableTrailingStop': True,
            'enableScaleInOut': True,
            'rsiBuy': 35.0,
            'rsiSell': 70.0,
            'fastMa': 5,
            'slowMa': 20,
            'takeProfitPct': 10.0,
            'stopLossPct': 5.0
        }
    }
    new_bot = test_api('6. 봇 신규 배포 & 24H 백그라운드 엔진 가동 API', '/api/bot/deploy', 'POST', bot_payload)
    bot_id = new_bot.get('botId') if new_bot else None

    # 7. 활성 봇 목록 및 실시간 상태
    bots_list = test_api('7. 활성 봇 실시간 관제 및 PnL 집계 API', '/api/bot/list')
    if bots_list:
        print(f'   ↳ 현재 실시간 가동 중인 봇: {len(bots_list.get("bots", []))}개')

    # 8. 봇 정지 테스트
    if bot_id:
        test_api('8. 봇 개별 즉시 청산 & 비상 정지 API', '/api/bot/stop', 'POST', {'botId': bot_id})
        test_api('9. 봇 아카이브 데이터 삭제 API', '/api/bot/delete', 'POST', {'botId': bot_id})

    # 10. 빗썸 공식 실시간 Public Ticker 직접 점검
    from services.bithumb_client import BithumbClient
    bc = BithumbClient()
    t_res = bc.get_ticker('BTC', 'KRW')
    if t_res.get('status') == '0000':
        closing_p = int(float(t_res['data']['closing_price']))
        print(f'✅ [10. 빗썸 공식 REST API 실시간 호가 통신] 정상 (BTC 실시간 시세: {closing_p:,}원)')
    else:
        print(f'❌ [10. 빗썸 공식 REST API] 오류')
        FAILURES.append('10. 빗썸 공식 REST API')

    print('\n===========================================================')
    if FAILURES:
        # 예전엔 실패해도 무조건 '100% 무결' 을 출력해서 장애가 묻혔다.
        print(f'  ❌ {len(FAILURES)}개 모듈 실패 — 아래 항목을 확인하세요')
        for f in FAILURES:
            print(f'     · {f}')
    else:
        print('  🎉 10대 핵심 모듈 전부 정상 응답')
    print('===========================================================')
    return 1 if FAILURES else 0

if __name__ == '__main__':
    import sys
    sys.exit(run_diagnostics())
