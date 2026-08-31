#!/bin/bash
# systemd 가 실행하는 진입점.
# .env 를 run.sh 와 동일한 파서로 읽어 로컬과 서버의 동작이 갈리지 않게 한다.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# shellcheck source=../scripts/load_env.sh
. "$DIR/scripts/load_env.sh"

if load_env_file "$DIR/.env"; then
    echo "[service] .env 로드 (설정 ${ENV_LOADED}개, 빈 값 ${ENV_SKIPPED}개 건너뜀)"
else
    echo "[service] .env 가 없습니다. 기동은 하지만 모든 데이터 API 가 잠깁니다." >&2
fi

if [ -z "${APP_ACCESS_PASSWORD:-}" ]; then
    echo "[service] APP_ACCESS_PASSWORD 가 없습니다. .env 를 채우고 재시작하세요." >&2
fi

PORT="${PORT:-8888}"
echo "[service] 127.0.0.1:${PORT} 에서 기동합니다"
exec "$DIR/venv/bin/python3" -m uvicorn server:app --host 127.0.0.1 --port "${PORT}"
