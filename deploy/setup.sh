#!/bin/bash
# 빗썸 원화 자동매매 콘솔 — VPS 최초 설치 스크립트 (Ubuntu 20.04 / 22.04 / 24.04)
#
# 사용법 (VPS 에 접속한 뒤):
#   sudo bash deploy/setup.sh
#
# 하는 일
#   1. 파이썬·가상환경 준비
#   2. 의존성 설치
#   3. .env 생성 (없으면)
#   4. systemd 서비스 등록 — 죽으면 자동 재시작, 부팅 시 자동 시작
#   5. 방화벽에서 앱 포트를 열지 않는다 (기본은 로컬 바인딩 + SSH 터널 권장)
#
# 이 스크립트는 여러 번 실행해도 안전하다.

set -euo pipefail

APP_USER="${APP_USER:-$(logname 2>/dev/null || echo ubuntu)}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="bithumb-bot"
PORT="${PORT:-8888}"

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$1"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "sudo 로 실행하세요:  sudo bash deploy/setup.sh"
  exit 1
fi

log "1/5 시스템 패키지"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null
python3 --version

log "2/5 가상환경과 의존성"
cd "$APP_DIR"
if [ ! -x "venv/bin/python3" ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
./venv/bin/python3 -c "import fastapi, uvicorn, jwt, requests; print('  의존성 확인 완료')"

log "3/5 설정 파일"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  warn ".env 를 만들었습니다. 아래 값을 채운 뒤 서비스를 재시작하세요:"
  echo "     APP_ACCESS_PASSWORD   (필수, 20자 이상)"
  echo "     BITHUMB_API_KEY / BITHUMB_SECRET_KEY"
  echo "     편집:  nano $APP_DIR/.env"
else
  chmod 600 .env
  echo "  기존 .env 유지 (권한 600 으로 조정)"
fi
mkdir -p data
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

log "4/5 systemd 서비스 등록"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=빗썸 원화 자동매매 콘솔
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PORT=${PORT}
# .env 는 systemd 의 EnvironmentFile 대신 앱과 같은 파서로 읽는다.
# systemd 파서는 따옴표·이스케이프 규칙이 달라 로컬과 서버가 .env 를 다르게
# 읽을 수 있다. 그 차이로 원인 모를 인증 실패가 나는 것을 막는다.
# 127.0.0.1 에만 바인딩한다. 외부 노출은 SSH 터널이나 리버스 프록시로 한다.
ExecStart=${APP_DIR}/deploy/service-start.sh

# 죽으면 항상 다시 띄운다. 자동매매 봇에는 이게 핵심이다.
Restart=always
RestartSec=5
# 짧은 시간에 반복 실패해도 포기하지 않는다 (기본은 5회 후 포기)
StartLimitIntervalSec=0

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# 최소 권한
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${APP_DIR}/data

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1
echo "  ${SERVICE_NAME}.service 등록 완료 (부팅 시 자동 시작, 죽으면 자동 재시작)"

log "5/5 확인"
if grep -qE '^APP_ACCESS_PASSWORD=.+' .env; then
  systemctl restart "${SERVICE_NAME}"
  sleep 3
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    echo "  서비스 가동 중"
    curl -fsS "http://127.0.0.1:${PORT}/api/health" && echo
  else
    warn "서비스가 뜨지 않았습니다. 로그를 확인하세요:"
    echo "     journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
  fi
else
  warn "APP_ACCESS_PASSWORD 가 비어 있어 서비스를 시작하지 않았습니다."
  echo "     nano $APP_DIR/.env   후"
  echo "     sudo systemctl start ${SERVICE_NAME}"
fi

cat <<INFO

────────────────────────────────────────────────────────
설치 완료

이 서버의 공인 IP (빗썸에 등록할 값):
  $(curl -fsS https://api.ipify.org 2>/dev/null || echo '조회 실패 — 직접 확인하세요')

자주 쓰는 명령
  상태     sudo systemctl status ${SERVICE_NAME}
  재시작   sudo systemctl restart ${SERVICE_NAME}
  정지     sudo systemctl stop ${SERVICE_NAME}
  로그     sudo journalctl -u ${SERVICE_NAME} -f

화면 접속 (내 PC 에서 SSH 터널)
  ssh -L ${PORT}:127.0.0.1:${PORT} ${APP_USER}@<서버IP>
  그 뒤 브라우저에서  http://localhost:${PORT}

업데이트
  cd ${APP_DIR} && git pull && sudo bash deploy/setup.sh
────────────────────────────────────────────────────────
INFO
