#!/usr/bin/env python3
"""화면 확인용 로컬 인스턴스. 실계좌·실키·운영 데이터에 닿지 않는다.

이 스크립트가 있는 이유:

UI 를 눈으로 확인하려고 개발 기계에서 서버를 띄운 적이 있다. 그때
`os.environ.pop()` 으로 키를 지우고 시작했는데도 실계좌 인증 호출이 나갔다.
server.py 가 import 시점에 `.env` 를 읽어 키를 다시 주입하기 때문이다.
빗썸은 IP 화이트리스트가 막아줬지만, Gemini 는 IP 제한이 없어 호출이
그대로 나가고 할당량을 썼다.

그래서 '지우는' 방식을 버리고 **네 겹으로 막는다.** 어느 하나가 뚫려도
나머지가 남는다.

  1) APP_SKIP_DOTENV=1  — server.py 가 .env 를 아예 읽지 않는다
  2) 환경변수 제거      — 셸에 남아 있던 키도 지운다
  3) 사전 점검(fail closed) — 그래도 키가 남아 있으면 **기동하지 않는다**
  4) 호출 경로 차단     — 빗썸 인증 메서드와 Gemini 호출을 예외로 바꾼다.
                          키가 어떻게든 들어와도 요청 자체가 나갈 수 없다

데이터도 임시 디렉터리로 돌려, 로컬 실행이 운영·개발 장부를 건드리지 않는다.

    python3 scripts/ui_preview.py          # 기동 (포트 8890)
    python3 scripts/ui_preview.py --check  # 차단만 검증하고 종료
"""

import os
import sys
import tempfile

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

PORT = int(os.getenv("UI_PREVIEW_PORT", "8890"))
PASSWORD = "ui-preview-local-only"

# 실키가 들어올 수 있는 통로를 모두 적어둔다. 새 연동을 붙이면 여기에 추가한다.
SECRET_ENV = (
    "BITHUMB_API_KEY", "BITHUMB_SECRET_KEY",
    "BINANCE_API_KEY", "BINANCE_SECRET_KEY",
    "GEMINI_API_KEY", "BITHUMB_PROXY_URL",
)


class Blocked(RuntimeError):
    """격리 실행에서 막은 호출."""


def _fail(msg: str) -> None:
    print(f"\n❌ 기동을 중단했습니다 — {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def isolate() -> str:
    """① .env 차단 ② 환경변수 제거 ③ 데이터 격리. 샌드박스 경로를 돌려준다."""
    os.environ["APP_SKIP_DOTENV"] = "1"
    for k in SECRET_ENV:
        os.environ.pop(k, None)
    os.environ["APP_ACCESS_PASSWORD"] = PASSWORD
    os.environ["PORT"] = str(PORT)

    sandbox = tempfile.mkdtemp(prefix="ui-preview-")
    from services import botstore, tradelog
    botstore._DATA_DIR = sandbox
    botstore.STORE_FILE = os.path.join(sandbox, "bots.json")
    botstore.arb_store.path = os.path.join(sandbox, "arb_bots.json")
    tradelog.LOG_FILE = os.path.join(sandbox, "trades.json")
    return sandbox


def block_live_calls() -> None:
    """④ 실계좌·유료 API 로 나가는 경로를 예외로 바꾼다.

    키를 지우는 것만으로는 부족하다. 지우는 데 실패한 적이 있다.
    여기서 막으면 키가 들어와도 요청이 나갈 수 없다.
    """
    from services import bithumb, gemini_service, keystore

    def refuse(what):
        def _inner(*_a, **_kw):
            raise Blocked(f"격리 실행에서는 {what} 를 호출하지 않습니다.")
        return _inner

    # 빗썸: 키가 필요한 메서드 전부 (잔고·주문·연결시험)
    for name in ("_v1_post", "_v2_headers", "get_balance",
                 "market_buy", "market_sell", "test_connection"):
        setattr(bithumb.BithumbAccount, name, refuse(f"빗썸 인증 API({name})"))

    # 키스토어가 키를 들고 있지 않게 한다 (화면은 '미연동' 으로 보인다).
    # 디스크에 저장된 키도 읽지 않도록 경로를 빈 임시 폴더로 돌린다.
    keys_dir = tempfile.mkdtemp(prefix="ui-preview-keys-")
    keystore.KEYS_FILE = os.path.join(keys_dir, "keys.json")
    gemini_service.GEMINI_KEY_FILE = os.path.join(keys_dir, "gemini_key.json")

    # Gemini: 할당량을 쓰는 호출 전부
    for name in ("analyze_coin", "scan_all_coins", "analyze_raoer_context"):
        if hasattr(gemini_service, name):
            setattr(gemini_service, name, refuse(f"Gemini API({name})"))


def verify() -> None:
    """차단이 실제로 걸렸는지 확인한다. 하나라도 뚫리면 기동하지 않는다.

    **server.py 를 import 한 뒤에 부른다.** import 시점에 .env 를 읽어 키를
    다시 주입하는 것이 바로 지난번에 뚫린 지점이라, 그 뒤에 봐야 의미가 있다.
    """
    from services import bithumb, gemini_service, keystore

    leaked = [k for k in SECRET_ENV if os.environ.get(k)]
    if leaked:
        _fail(f"환경변수에 키가 남아 있습니다: {', '.join(leaked)} "
              "— .env 가 다시 주입됐을 수 있습니다 (APP_SKIP_DOTENV 확인)")

    if keystore.keystore.account.configured:
        _fail(f"빗썸 키스토어가 실계좌 키를 들고 있습니다 "
              f"({keystore.keystore.account.masked_key()}, 출처 {keystore.keystore.source})")

    # gemini_service 는 server.py 와 별개로 .env 를 직접 읽는 두 번째 경로가
    # 있었다. 환경변수만 보면 놓친다 — 키스토어 자체를 확인한다.
    if gemini_service.gemini_keystore.configured:
        _fail(f"Gemini 키스토어가 키를 들고 있습니다 "
              f"(출처 {gemini_service.gemini_keystore.source})")

    acct = bithumb.BithumbAccount("dummy-key", "dummy-secret")
    for call, label in ((lambda: acct.get_balance(), "빗썸 잔고"),
                        (lambda: acct.market_buy("BTC", 10000), "빗썸 매수"),
                        (lambda: acct.market_sell("BTC", 1.0), "빗썸 매도"),
                        (lambda: acct.test_connection(), "빗썸 연결시험")):
        try:
            call()
        except Blocked:
            continue
        except Exception as e:
            _fail(f"{label} 가 Blocked 가 아닌 예외로 실패했습니다 ({e!r}) — 차단이 아닙니다")
        _fail(f"{label} 호출이 막히지 않았습니다")

    for name in ("analyze_coin", "scan_all_coins", "analyze_raoer_context"):
        fn = getattr(gemini_service, name, None)
        if fn is None:
            continue
        try:
            fn("BTC")
        except Blocked:
            continue
        except Exception as e:
            _fail(f"Gemini {name} 가 Blocked 가 아닌 예외로 실패했습니다 ({e!r})")
        _fail(f"Gemini {name} 호출이 막히지 않았습니다")

    print("  ✅ 빗썸 인증 API 4종 차단")
    print("  ✅ Gemini 호출 3종 차단")
    print("  ✅ 환경변수에 실키 없음")
    print("  ✅ .env 미로드 (APP_SKIP_DOTENV=1)")


def main() -> None:
    check_only = "--check" in sys.argv
    print("── 격리 점검 ──")
    sandbox = isolate()
    block_live_calls()

    # server.py 를 여기서 직접 import 한다. uvicorn 에 "server:app" 문자열을
    # 넘기면 import 가 기동 안에서 일어나 검증을 앞세울 수 없다.
    import server as server_module

    verify()
    print(f"  ✅ 데이터 경로 격리: {sandbox}")
    print(f"  ✅ server.py import 후에도 키 없음")

    if check_only:
        print("\n차단 확인만 하고 종료합니다 (--check).")
        return

    print(f"\n화면: http://127.0.0.1:{PORT}/   비밀번호: {PASSWORD}")
    print("실계좌 연동은 '미연동' 으로, AI 분석은 오류로 보입니다 — 의도된 것입니다.\n")
    import uvicorn
    uvicorn.run(server_module.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
