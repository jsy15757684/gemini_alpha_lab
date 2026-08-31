#!/bin/bash
# 빗썸 자동매매 콘솔 — 화면 열기 (더블클릭 실행)
#
# 하는 일
#   1. Vultr 서버로 SSH 터널을 연다
#   2. 앱이 응답하는지 확인한다
#   3. 브라우저를 연다
#   4. 이 창이 닫히면 터널만 끊긴다 (서버의 봇은 계속 돈다)
#
# 설계 메모: 터널을 127.0.0.1 에만 바인딩하고 ExitOnForwardFailure=yes 를 준다.
# 그래야 8888 이 이미 쓰이고 있을 때 조용히 다른 서버로 넘어가지 않고
# 그 자리에서 실패한다. 같은 주소가 다른 서버를 가리키는 사고를 막는 장치다.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_load_conf.sh" 2>/dev/null \
  || source "$HOME/gemini_alpha_lab/deploy/mac/_load_conf.sh"
load_conf     # SERVER · SERVER_HOST · PORT 를 채운다

printf '\033[1;36m'
cat <<'BANNER'
────────────────────────────────────────────
  빗썸 원화 자동매매 콘솔 — 접속
────────────────────────────────────────────
BANNER
printf '\033[0m\n'

# ── 포트 선점 확인 ────────────────────────────────────────────────
holder=$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null | tail -n +2)
if [ -n "$holder" ]; then
  echo "⚠️  ${PORT} 번 포트가 이미 사용 중입니다:"
  echo "$holder" | awk '{print "     ",$1,"(PID",$2")",$9}'
  echo ""
  if echo "$holder" | grep -q Python; then
    echo "   맥에서 서버가 돌고 있는 것 같습니다. 이걸 끄지 않으면"
    echo "   화면이 Vultr 가 아니라 맥을 보여줄 수 있습니다."
    echo ""
    read -p "   맥 서버를 끄고 계속할까요? [y/N] " ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
      pkill -f 'uvicorn' 2>/dev/null
      sleep 2
      echo "   → 종료했습니다."
    else
      echo "   중단합니다."
      read -p "   Enter 를 누르면 창이 닫힙니다. " _
      exit 1
    fi
  else
    # 이미 ssh 가 붙어 있다. 다만 어느 주소에 바인딩됐는지는 알 수 없다.
    # (예전 방식으로 연 터널은 IPv6 [::1] 에만 붙는다.)
    # 실제로 응답하는 주소를 찾아서 그걸 연다. 추측해서 열면 빈 화면이 뜬다.
    echo "   이미 터널이 열려 있는 것 같습니다. 응답하는 주소를 찾습니다."
    url=""
    for cand in "http://127.0.0.1:${PORT}" "http://[::1]:${PORT}"; do
      case "$(curl -s -m 4 "${cand}/api/health" 2>/dev/null)" in
        *'"status":"ok"'*) url="$cand"; break ;;
      esac
    done
    if [ -n "$url" ]; then
      echo "   ✅ $url 이 응답합니다 — 브라우저를 엽니다."
      open "$url"
      read -p "   Enter 를 누르면 창이 닫힙니다. " _
      exit 0
    fi
    echo ""
    echo "   ⚠️  ssh 가 ${PORT} 를 점유했는데 앱이 응답하지 않습니다."
    echo "      끊긴 터널이 포트만 붙잡고 있는 상태일 수 있습니다."
    echo ""
    read -p "   그 ssh 를 끊고 새로 열까요? [y/N] " ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
      echo "$holder" | awk '{print $2}' | while read -r p; do kill "$p" 2>/dev/null; done
      sleep 2
      echo "   → 끊었습니다. 새 터널을 엽니다."
    else
      echo "   중단합니다."
      read -p "   Enter 를 누르면 창이 닫힙니다. " _
      exit 1
    fi
  fi
  echo ""
fi

# ── 서버 생존 확인 ────────────────────────────────────────────────
echo "▶ 서버 확인 중 (${SERVER_HOST}) ..."
if ! nc -z -w 6 "$SERVER_HOST" 22 2>/dev/null; then
  echo ""
  echo "❌ 서버에 접속할 수 없습니다 (SSH 22번 무응답)."
  echo "   Vultr 콘솔에서 인스턴스가 Running 인지 확인하세요."
  echo ""
  read -p "Enter 를 누르면 창이 닫힙니다. " _
  exit 1
fi
echo "  ✅ 서버 응답 있음"
echo ""

# ── 터널이 열리면 브라우저를 연다 (백그라운드 감시) ──────────────
(
  for i in $(seq 1 40); do
    sleep 1
    body=$(curl -s -m 3 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)
    case "$body" in
      *'"status":"ok"'*)
        echo ""
        echo "  ✅ 앱 응답 확인 — 브라우저를 엽니다"
        echo "     $body"
        open "http://127.0.0.1:${PORT}"
        exit 0
        ;;
    esac
  done
  echo ""
  echo "  ⚠️  40초 안에 앱이 응답하지 않았습니다."
  echo "     서버에서 서비스 상태를 확인하세요:"
  echo "       systemctl status bithumb-bot --no-pager"
) &

# ── 터널 (전경 실행: 이 창이 곧 터널이다) ────────────────────────
echo "▶ 터널을 엽니다. 비밀번호를 입력하세요 (화면에 안 보이는 게 정상)"
echo ""

ssh -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "127.0.0.1:${PORT}:127.0.0.1:${PORT}" \
    "$SERVER"

code=$?
echo ""
printf '\033[1;33m'
cat <<'OUTRO'
────────────────────────────────────────────
  터널이 닫혔습니다.

  화면 접속만 끊겼을 뿐, 서버의 봇은 계속 돕니다.
  다시 보시려면 이 파일을 또 더블클릭하세요.
────────────────────────────────────────────
OUTRO
printf '\033[0m'
[ $code -ne 0 ] && echo "(ssh 종료 코드: $code)"
echo ""
read -p "Enter 를 누르면 창이 닫힙니다. " _
