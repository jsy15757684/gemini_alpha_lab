#!/bin/bash
# 이미 설치된 서비스의 systemd 설정을 제자리로 돌린다.
#
# 두 가지를 고친다. 둘 다 앱 재시작 없이 끝난다 — 봇은 계속 돈다.
#
# 1) StartLimitIntervalSec 이 [Service] 에 들어가 있어 systemd 가 무시한다.
#    ('Unknown key name ... in section Service, ignoring' 이 저널에 남는다)
#    이 키는 [Unit] 소속이다. 지금은 기본값(10초에 5회 실패하면 포기)이
#    걸려 있어서, 부팅 크래시가 나면 서비스가 죽은 채 방치된다. 그 상태는
#    빗썸에 포지션이 남았는데 손절·익절을 아무도 안 보는 상태와 같다.
#
# 2) journald 에 상한이 없다. 매 회차 매수 사유(AI 판단 포함)가 쌓여
#    꾸준히 커진다. 전용 서버라 200M 이면 넉넉하다.
#
# 여러 번 실행해도 안전하다.
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-bithumb-bot}"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 로 실행하세요: sudo bash deploy/fix-service.sh"; exit 1
fi
[ -f "$UNIT" ] || { echo "유닛 파일이 없습니다: $UNIT"; exit 1; }

cp -a "$UNIT" "${UNIT}.bak.$(date +%Y%m%d%H%M%S)"

python3 - "$UNIT" <<'PY'
import re, sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()

# 기존 키와 그에 딸린 주석을 절 구분 없이 걷어낸다.
# 이 스크립트가 직접 넣은 주석도 지워야 두 번 돌렸을 때 겹치지 않는다.
had = re.search(r"(?m)^StartLimitIntervalSec=", text) is not None
text = re.sub(r"(?m)^#[^\n]*(반복 실패|\[Unit\] 소속)[^\n]*\n", "", text)
text = re.sub(r"(?m)^StartLimitIntervalSec=.*\n", "", text)

# [Unit] 안에 넣는다 (이미 있으면 위에서 지워졌으니 한 번만 들어간다)
block = ("# 짧은 시간에 반복 실패해도 재시작을 포기하지 않는다 (기본은 10초에 5회 후 포기).\n"
         "# 이 키는 [Unit] 소속이다 — [Service] 에 두면 systemd 가 조용히 무시한다.\n"
         "StartLimitIntervalSec=0\n")
m = re.search(r"(?ms)^\[Unit\]\n(.*?)(?=^\[)", text)
if not m:
    sys.exit("[Unit] 절을 찾지 못했습니다")
text = text[:m.end(1)].rstrip("\n") + "\n" + block + "\n" + text[m.end(1):].lstrip("\n")

open(path, "w", encoding="utf-8").write(text)
print(f"  StartLimitIntervalSec → [Unit] 로 이동 (이전 위치에 {'있었음' if had else '없었음'})")
PY

mkdir -p /etc/systemd/journald.conf.d
cat > "/etc/systemd/journald.conf.d/99-${SERVICE_NAME}.conf" <<'JOURNALD'
[Journal]
SystemMaxUse=200M
SystemKeepFree=1G
JOURNALD
echo "  journald 상한 200M 설정"

systemctl daemon-reload
systemctl restart systemd-journald
journalctl --vacuum-size=200M >/dev/null 2>&1 || true

echo
echo "── 확인 ──"
echo -n "  유닛 문법      : "
if systemd-analyze verify "$UNIT" 2>&1 | grep -q .; then
  systemd-analyze verify "$UNIT" 2>&1 | sed 's/^/                   /'
else
  echo "이상 없음"
fi
echo "  재시작 제한    : StartLimitIntervalSec=$(systemctl show "$SERVICE_NAME" -p StartLimitIntervalUSec --value) (0 이면 무제한)"
echo "  서비스 상태    : $(systemctl is-active "$SERVICE_NAME")"
echo "  저널 사용량    : $(journalctl --disk-usage | grep -oE '[0-9.]+[KMG]' | tail -1)"
