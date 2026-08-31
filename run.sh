#!/bin/bash
# Gemini Alpha Lab 로컬 실행 스크립트
# 어느 PC 에서든 돌아가도록: venv 자동 생성 -> 의존성 설치 -> .env 로드 -> 기동
set -u

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "=========================================================="
echo " 🪙 빗썸 원화 자동매매 콘솔"
echo "=========================================================="

# ── 1. .env 로드 ────────────────────────────────────────────
# 키를 쉘 히스토리에 남기지 않기 위해 파일에서 읽는다.
#
# 주의: `. ./.env` 로 source 하면 안 된다.
#   KEY= value   ← '=' 뒤 공백이 있으면 쉘은 값을 '명령'으로 실행해버리고
#                   변수는 빈 값이 된다. 붙여넣기할 때 흔히 생기는 형태다.
#   KEY=a b c    ← 따옴표 없는 공백도 같은 문제
# 그래서 직접 파싱한다. 값을 실행하지 않으므로 안전하기도 하다.
# 파서는 scripts/load_env.sh 에 있다. systemd 서비스도 같은 파일을 쓴다.
. "$DIR/scripts/load_env.sh"


if load_env_file ".env"; then
    echo "✅ .env 로드 (설정 ${ENV_LOADED}개, 빈 값 ${ENV_SKIPPED}개 건너뜀)"
else
    echo "ℹ️  .env 가 없습니다.  cp .env.example .env  후 값을 채우세요."
fi

# ── 2. 파이썬 환경 준비 ──────────────────────────────────────
if [ -x "venv/bin/python3" ]; then
    PYTHON="venv/bin/python3"
elif [ -x ".venv/bin/python3" ]; then
    PYTHON=".venv/bin/python3"
elif [ -x "/Users/jay_mac/my_ai_system/ai_env/bin/python3" ]; then
    PYTHON="/Users/jay_mac/my_ai_system/ai_env/bin/python3"
elif [ -x "../ai_env/bin/python3" ]; then
    PYTHON="../ai_env/bin/python3"
else
    echo "🔧 가상환경이 없어 venv 를 새로 만듭니다 (처음 1회만, 1~2분 소요)"
    BASE_PY="$(command -v python3 || true)"
    if [ -z "$BASE_PY" ]; then
        echo "❌ python3 를 찾을 수 없습니다. Python 3.9 이상을 먼저 설치하세요."
        exit 1
    fi
    "$BASE_PY" -m venv venv || { echo "❌ venv 생성 실패"; exit 1; }
    PYTHON="venv/bin/python3"
    "$PYTHON" -m pip install --quiet --upgrade pip
    echo "🔧 의존성 설치 중..."
    "$PYTHON" -m pip install --quiet -r requirements.txt || { echo "❌ 의존성 설치 실패"; exit 1; }
    echo "✅ 설치 완료"
fi

# 의존성 누락 확인 (PyJWT 를 빠뜨려 배포가 죽은 전례가 있다)
if ! "$PYTHON" -c "import fastapi, uvicorn, requests, jwt" 2>/dev/null; then
    echo "🔧 누락된 의존성을 설치합니다..."
    "$PYTHON" -m pip install --quiet -r requirements.txt || { echo "❌ 의존성 설치 실패"; exit 1; }
fi

echo "Using Python: $PYTHON"

# ── 3. 필수 설정 점검 ───────────────────────────────────────
if [ -z "${APP_ACCESS_PASSWORD:-}" ]; then
    echo ""
    echo "⚠️  APP_ACCESS_PASSWORD 가 없습니다."
    echo "   서버는 뜨지만 모든 데이터 API 가 잠긴 상태(503)로 동작합니다."
    echo "   .env 에 APP_ACCESS_PASSWORD 를 넣고 다시 실행하세요."
    echo ""
fi

if [ -n "${BITHUMB_API_KEY:-}" ]; then
    echo "🪙 빗썸 키가 환경변수로 주입되었습니다."
    echo "   빗썸 [API 관리 > IP 주소 등록] 에 이 PC 의 공인 IP 가 등록되어 있어야 합니다."
    echo "   IP 확인: 로그인 후 [빗썸 계정] 탭"
fi

PORT="${PORT:-8888}"

# ── 포트 점유 확인 ──────────────────────────────────────────
# --reload 개발 서버는 종료가 깔끔하지 않으면 프로세스가 남아 포트를 계속 잡는다.
# 그대로 두면 "[Errno 48] Address already in use" 로 기동이 실패한다.
if command -v lsof >/dev/null 2>&1; then
    HOLDER_PIDS="$(lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$HOLDER_PIDS" ]; then
        echo ""
        echo "⚠️  포트 ${PORT} 을(를) 이미 다른 프로세스가 쓰고 있습니다:"
        lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | tail -n +2 | sed 's/^/     /'
        # 이 프로젝트의 서버가 남아 있는 경우에만 자동 정리한다.
        OURS=""
        for pid in $HOLDER_PIDS; do
            if ps -o command= -p "$pid" 2>/dev/null | grep -q "uvicorn server:app"; then
                OURS="$OURS $pid"
            fi
        done
        if [ -n "$OURS" ]; then
            echo "   → 이전에 실행된 이 프로젝트의 서버입니다. 정리하고 계속합니다."
            # shellcheck disable=SC2086
            kill $OURS 2>/dev/null || true
            sleep 2
            for pid in $OURS; do kill -9 "$pid" 2>/dev/null || true; done
            sleep 1
        else
            echo "   → 이 프로젝트의 서버가 아닙니다. 임의로 종료하지 않습니다."
            echo "   다른 포트로 실행하려면:  PORT=8899 ./run.sh"
            exit 1
        fi
    fi
fi

echo "Starting server at http://localhost:${PORT} ..."

# 로컬 실행이므로 외부에 열지 않는다 (0.0.0.0 대신 127.0.0.1)
if command -v open >/dev/null 2>&1; then
    (sleep 2 && open "http://localhost:${PORT}") &
fi

exec "$PYTHON" -m uvicorn server:app --host 127.0.0.1 --port "${PORT}" --reload
