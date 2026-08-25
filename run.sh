#!/bin/bash
# Gemini Alpha Lab 로컬 실행 스크립트
# 어느 PC 에서든 돌아가도록: venv 자동 생성 -> 의존성 설치 -> .env 로드 -> 기동
set -u

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "=========================================================="
echo " 🚀 Gemini Alpha Lab - AI 퀀트 & 자동매매 OS"
echo "=========================================================="

# ── 1. .env 로드 ────────────────────────────────────────────
# 키를 쉘 히스토리에 남기지 않기 위해 파일에서 읽는다.
#
# 주의: `. ./.env` 로 source 하면 안 된다.
#   KEY= value   ← '=' 뒤 공백이 있으면 쉘은 값을 '명령'으로 실행해버리고
#                   변수는 빈 값이 된다. 붙여넣기할 때 흔히 생기는 형태다.
#   KEY=a b c    ← 따옴표 없는 공백도 같은 문제
# 그래서 직접 파싱한다. 값을 실행하지 않으므로 안전하기도 하다.
load_env_file() {
    local file="$1" line key val loaded=0 skipped=0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"                      # Windows 줄바꿈 제거
        case "$line" in ''|'#'*) continue;; esac
        case "$line" in *=*) ;; *) continue;; esac

        key="${line%%=*}"
        val="${line#*=}"

        key="$(printf '%s' "$key" | tr -d '[:space:]')"
        val="${val#"${val%%[![:space:]]*}"}"       # 앞 공백
        val="${val%"${val##*[![:space:]]}"}"       # 뒤 공백

        case "$val" in                              # 감싼 따옴표 제거
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac

        [ -z "$key" ] && continue
        if [ -z "$val" ]; then skipped=$((skipped+1)); continue; fi
        export "$key=$val"
        loaded=$((loaded+1))
    done < "$file"
    echo "✅ .env 로드 (설정 ${loaded}개, 빈 값 ${skipped}개 건너뜀)"
}

if [ -f ".env" ]; then
    load_env_file ".env"
else
    echo "ℹ️  .env 가 없습니다.  cp .env.example .env  후 값을 채우세요."
fi

# ── 2. 파이썬 환경 준비 ──────────────────────────────────────
if [ -x "/Users/jay_mac/my_ai_system/ai_env/bin/python3" ]; then
    PYTHON="/Users/jay_mac/my_ai_system/ai_env/bin/python3"   # 기존 개발 환경
elif [ -x "../ai_env/bin/python3" ]; then
    PYTHON="../ai_env/bin/python3"
elif [ -x "venv/bin/python3" ]; then
    PYTHON="venv/bin/python3"
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
if ! "$PYTHON" -c "import fastapi, uvicorn, yfinance, jwt" 2>/dev/null; then
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
    echo "   IP 확인: 로그인 후 [브로커/거래소 API 연동 센터] > 빗썸 [API 키 등록]"
fi

PORT="${PORT:-8888}"
echo "Starting server at http://localhost:${PORT} ..."

# 로컬 실행이므로 외부에 열지 않는다 (0.0.0.0 대신 127.0.0.1)
if command -v open >/dev/null 2>&1; then
    (sleep 2 && open "http://localhost:${PORT}") &
fi

exec "$PYTHON" -m uvicorn server:app --host 127.0.0.1 --port "${PORT}" --reload
