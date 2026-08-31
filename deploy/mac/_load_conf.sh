#!/bin/bash
# server.conf 를 찾아 읽는다. 두 .command 스크립트가 공유한다.
#
# 데스크탑에 복사한 파일에서도 동작해야 하므로, 스크립트 옆과
# 저장소 경로 두 곳을 순서대로 찾는다.

_find_conf() {
  local here="$1"
  for c in "$here/server.conf" \
           "$HOME/gemini_alpha_lab/deploy/mac/server.conf"; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

load_conf() {
  local here conf
  here="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
  if ! conf="$(_find_conf "$here")"; then
    echo ""
    echo "❌ server.conf 를 찾지 못했습니다."
    echo ""
    echo "   아래처럼 만들어 주세요:"
    echo "     cd ~/gemini_alpha_lab/deploy/mac"
    echo "     cp server.conf.example server.conf"
    echo "     nano server.conf     # 서버 공인 IP 를 넣습니다"
    echo ""
    read -p "Enter 를 누르면 창이 닫힙니다. " _
    exit 1
  fi

  # 값만 읽는다. 파일을 source 하지 않는다 —
  # 설정파일이 명령을 실행하게 두면 안 된다.
  SERVER_HOST=$(grep -E '^SERVER_HOST=' "$conf" | tail -1 | cut -d= -f2- | tr -d ' "'"'"'\r')
  SERVER_USER=$(grep -E '^SERVER_USER=' "$conf" | tail -1 | cut -d= -f2- | tr -d ' "'"'"'\r')
  PORT=$(       grep -E '^PORT='        "$conf" | tail -1 | cut -d= -f2- | tr -d ' "'"'"'\r')
  SERVER_USER="${SERVER_USER:-root}"
  PORT="${PORT:-8888}"

  case "$SERVER_HOST" in
    ""|*여기에*)
      echo ""
      echo "❌ $conf 의 SERVER_HOST 가 채워지지 않았습니다."
      echo "   서버 공인 IP 를 넣어 주세요."
      echo ""
      read -p "Enter 를 누르면 창이 닫힙니다. " _
      exit 1 ;;
  esac
  SERVER="${SERVER_USER}@${SERVER_HOST}"
}
