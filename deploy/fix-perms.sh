#!/bin/bash
# 업데이트(git pull) 후 소유권·권한을 제자리로 돌린다.
#
# 서비스는 전용 계정으로 돌고 코드는 읽기 전용이다. git pull 은 root 로
# 실행되므로 새 파일이 root:root 로 생기고, 그러면 서비스 계정이 그룹으로
# 읽을 수 없게 된다. 지금은 umask 022 덕에 'others 읽기' 로 우연히 동작하지만
# umask 가 다르면 그 순간 서비스가 못 뜬다. 그 우연에 기대지 않는다.
#
# 디렉터리에 setgid 를 걸어 앞으로 생기는 파일이 그룹을 물려받게 하고,
# 기존 파일의 소유권도 다시 맞춘다. 여러 번 실행해도 안전하다.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-bithumb}"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 로 실행하세요: sudo bash deploy/fix-perms.sh"; exit 1
fi
id "$APP_USER" >/dev/null 2>&1 || { echo "계정이 없습니다: $APP_USER"; exit 1; }

# 코드 — root 소유, 서비스 계정은 읽기만
chown -R root:"$APP_USER" "$APP_DIR"
chmod -R u=rwX,g=rX,o= "$APP_DIR"
# 앞으로 만들어지는 파일이 그룹을 물려받도록 (핵심)
find "$APP_DIR" -type d -exec chmod g+s {} +

# data — 서비스 계정이 써야 한다
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

# .env — 읽기만
[ -f "$APP_DIR/.env" ] && { chown root:"$APP_USER" "$APP_DIR/.env"; chmod 640 "$APP_DIR/.env"; }

chmod 750 "$APP_DIR/deploy/"*.sh "$APP_DIR/scripts/"*.sh 2>/dev/null || true

echo "권한 정리 완료"
echo "  코드   root:$APP_USER (읽기 전용, setgid)"
echo "  data   $APP_USER (쓰기 가능)"
echo "  .env   root:$APP_USER 640"
