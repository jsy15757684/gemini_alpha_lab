#!/bin/bash
# 크립토와 나무증권을 두 서비스로 나눈다 (되돌리기: --undo).
#
#   bithumb-bot    APP_ROLE=crypto · 127.0.0.1:8888 · 화면 + 로그인 + 빗썸 봇
#   bithumb-namuh  APP_ROLE=namuh  · 127.0.0.1:8889 · 나무증권 봇 (내부 전용)
#
# 화면 주소는 그대로다. 나무증권 요청은 bithumb-bot 이 내부 토큰을 붙여
# bithumb-namuh 로 넘긴다. 토큰은 data/.internal_token 에 저절로 생긴다.
#
# 처음 뜰 때 data/bots.json 의 나무증권 봇이 data/bots_namuh.json 으로
# 옮겨진다. 체결 일지는 복사만 하고 원본을 지우지 않는다.
#
# --undo 는 한 서비스(APP_ROLE=all)로 돌린다. 그때는 두 파일을 모두 읽으므로
# 나눠 돌던 동안의 봇과 체결이 빠지지 않는다.
#
# 봇은 재시작해도 청산되지 않는다(상태는 디스크에 있다). 그래도 미국장
# 주문 시각(한국시간 22:30 무렵 · LOC 는 마감 전)은 피해서 돌린다.
set -euo pipefail

MAIN="bithumb-bot"
WORKER="bithumb-namuh"
WORKER_PORT="${WORKER_PORT:-8889}"
UNIT_DIR="/etc/systemd/system"
DROPIN_DIR="${UNIT_DIR}/${MAIN}.service.d"
DROPIN="${DROPIN_DIR}/10-role.conf"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 로 실행하세요: sudo bash deploy/split-services.sh"; exit 1
fi
[ -f "${UNIT_DIR}/${MAIN}.service" ] || { echo "${MAIN}.service 가 없습니다. deploy/setup.sh 를 먼저 돌리세요."; exit 1; }

health() {  # 포트, 이름
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$1/api/health" >/dev/null 2>&1; then
      echo "  ✅ $2 응답 (127.0.0.1:$1)"; return 0
    fi
    sleep 1
  done
  echo "  ❌ $2 가 30초 안에 응답하지 않았습니다 — journalctl -u $2 -n 50"; return 1
}

if [ "${1:-}" = "--undo" ]; then
  echo "── 한 서비스로 되돌립니다 ──"
  systemctl stop "$MAIN" "$WORKER" 2>/dev/null || true
  systemctl disable "$WORKER" 2>/dev/null || true
  rm -f "$DROPIN"
  systemctl daemon-reload
  systemctl start "$MAIN"
  health 8888 "$MAIN"
  echo "되돌렸습니다. ${MAIN} 이 크립토·나무증권 봇을 모두 돌립니다."
  exit 0
fi

if ss -ltn "sport = :${WORKER_PORT}" | grep -q LISTEN && ! systemctl is-active -q "$WORKER"; then
  echo "127.0.0.1:${WORKER_PORT} 을 다른 프로그램이 쓰고 있습니다. WORKER_PORT=다른번호 로 다시 돌리세요."; exit 1
fi

echo "── 1/4 나무증권 워커 서비스 만들기 (${WORKER}) ──"
# 기존 유닛을 그대로 본뜬다. 권한 제한(ProtectSystem 등)과 재시작 정책을 같게 둔다.
python3 - "${UNIT_DIR}/${MAIN}.service" "${UNIT_DIR}/${WORKER}.service" "$WORKER_PORT" <<'PY'
import re, sys
src, dst, port = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(src, encoding="utf-8").read()
t = re.sub(r"(?m)^Description=.*$", "Description=나무증권 자동매매 워커 (APP_ROLE=namuh)", t)
t = re.sub(r"(?m)^Environment=PORT=.*$", f"Environment=PORT={port}\nEnvironment=APP_ROLE=namuh", t)
t = re.sub(r"(?m)^SyslogIdentifier=.*$", "SyslogIdentifier=bithumb-namuh", t)
if "APP_ROLE=namuh" not in t:
    t = t.replace("[Service]\n", f"[Service]\nEnvironment=PORT={port}\nEnvironment=APP_ROLE=namuh\n", 1)
open(dst, "w", encoding="utf-8").write(t)
PY
echo "  ${UNIT_DIR}/${WORKER}.service"

echo "── 2/4 화면 서비스를 crypto 역할로 (${MAIN}) ──"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN" <<CONF
[Service]
Environment=APP_ROLE=crypto
Environment=APP_NAMUH_WORKER_URL=http://127.0.0.1:${WORKER_PORT}
CONF
echo "  ${DROPIN}"

echo "── 3/4 재시작 ──"
systemctl daemon-reload
systemctl stop "$MAIN"                  # 한 프로세스가 두 쪽 봇을 들고 있는 상태를 끝낸다
systemctl enable "$WORKER" >/dev/null
systemctl start "$WORKER"
systemctl start "$MAIN"

echo "── 4/4 확인 ──"
ok=0
health "$WORKER_PORT" "$WORKER" || ok=1
health 8888 "$MAIN" || ok=1
journalctl -u "$MAIN" -u "$WORKER" --since "-1 min" --no-pager | grep -E "프로세스 역할|봇 상태를 나눴|봇 복원|나무증권 체결" | sed 's/^/  /' || true
if [ "$ok" -ne 0 ]; then
  echo "문제가 있습니다. 되돌리려면: sudo bash deploy/split-services.sh --undo"; exit 1
fi
echo "나눴습니다. 화면 주소는 그대로입니다."
