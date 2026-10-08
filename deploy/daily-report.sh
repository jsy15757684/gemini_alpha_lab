#!/bin/bash
# 매일 아침 점검 보고(텔레그램)를 systemd 타이머로 건다 (되돌리기: --undo).
#
#   gal-daily-report.timer    월~토 09:35 (한국시간) — 토요일은 금요일 밤 미국장 몫
#   gal-daily-report.service  scripts/daily_report.py 를 서비스 계정으로 한 번 돌린다
#
# 읽기만 하는 보고다 — 주문 · 봇 변경을 하지 않는다. 서비스 계정은 원래 시스템
# 로그를 못 읽는다. 이 서비스에만 systemd-journal 그룹을 붙여 ERROR 줄을 센다
# (계정 자체의 그룹은 바꾸지 않는다).
#
# 먼저 .env 에 두 줄을 넣는다 (deploy/README.md '텔레그램 알림'):
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-bithumb}"
NAME="gal-daily-report"
UNIT_DIR="/etc/systemd/system"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 로 실행하세요: sudo bash deploy/daily-report.sh"; exit 1
fi

if [ "${1:-}" = "--undo" ]; then
  systemctl disable --now "${NAME}.timer" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${NAME}.timer" "${UNIT_DIR}/${NAME}.service"
  systemctl daemon-reload
  echo "점검 보고 타이머를 껐습니다."
  exit 0
fi

cat > "${UNIT_DIR}/${NAME}.service" <<EOF
[Unit]
Description=gemini_alpha_lab 일일 점검 보고 (텔레그램)
After=network-online.target bithumb-namuh.service
Wants=network-online.target

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_USER}
SupplementaryGroups=systemd-journal
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/scripts/daily_report.py
TimeoutStartSec=180
NoNewPrivileges=true
PrivateTmp=true
EOF

cat > "${UNIT_DIR}/${NAME}.timer" <<EOF
[Unit]
Description=gemini_alpha_lab 일일 점검 보고 — 월~토 09:35 한국시간

[Timer]
OnCalendar=Mon..Sat *-*-* 09:35:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${NAME}.timer"
echo "✅ 점검 보고 타이머를 걸었습니다."
systemctl list-timers "${NAME}.timer" --no-pager | head -3
echo ""
echo "지금 한 번 보내 보기:   sudo systemctl start ${NAME}.service && journalctl -u ${NAME} -n 5 --no-pager"
echo "보내지 않고 보기:       sudo -u ${APP_USER} ${APP_DIR}/venv/bin/python ${APP_DIR}/scripts/daily_report.py --dry-run"
