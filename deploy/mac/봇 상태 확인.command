#!/bin/bash
# 빗썸 자동매매 콘솔 — 서버 상태 점검 (더블클릭 실행)
#
# 브라우저를 열지 않고, 서버가 24시간 돌 상태인지만 확인한다.
# 터널이 필요 없다 — SSH 로 붙어서 명령만 돌리고 끊는다.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_load_conf.sh" 2>/dev/null \
  || source "$HOME/gemini_alpha_lab/deploy/mac/_load_conf.sh"
load_conf     # SERVER · SERVER_HOST · PORT 를 채운다

printf '\033[1;36m'
cat <<'BANNER'
────────────────────────────────────────────
  서버 상태 점검
────────────────────────────────────────────
BANNER
printf '\033[0m\n'

echo "▶ 네트워크"
if nc -z -w 6 "$SERVER_HOST" 22 2>/dev/null; then
  echo "  ✅ SSH(22) 열림"
else
  echo "  ❌ 무응답 — Vultr 콘솔에서 인스턴스가 Running 인지 확인하세요"
  read -p "Enter 를 누르면 창이 닫힙니다. " _
  exit 1
fi
ping -c 3 -t 5 "$SERVER_HOST" 2>&1 | tail -1 | sed 's/^/  /'
echo ""

echo "▶ 서버에 접속합니다. 비밀번호를 입력하세요"
echo ""

ssh -o ConnectTimeout=10 "$SERVER" 'bash -s' -- "$SERVER_HOST" <<'REMOTE'
echo "── 서비스 ─────────────────────────────"
en=$(systemctl is-enabled bithumb-bot 2>/dev/null)
ac=$(systemctl is-active  bithumb-bot 2>/dev/null)
[ "$en" = "enabled" ] && echo "  ✅ 부팅 시 자동 시작: $en" || echo "  ❌ 부팅 시 자동 시작: ${en:-확인불가}"
[ "$ac" = "active"  ] && echo "  ✅ 현재 가동 중: $ac"      || echo "  ❌ 현재 가동 중: ${ac:-확인불가}"
echo "  가동 시간: $(systemctl show bithumb-bot -p ActiveEnterTimestamp --value 2>/dev/null)"
echo "  재시작 횟수: $(systemctl show bithumb-bot -p NRestarts --value 2>/dev/null)"

echo ""
echo "── 앱 ─────────────────────────────────"
h=$(curl -s -m 5 http://127.0.0.1:8888/api/health 2>/dev/null)
if [ -n "$h" ]; then echo "  ✅ $h"; else echo "  ❌ 앱이 응답하지 않습니다"; fi

echo ""
echo "── 공인 IP (빗썸 등록값과 같아야 함) ───"
expected="$1"
ip=$(curl -s -m 8 https://api.ipify.org 2>/dev/null)
if [ -z "$ip" ]; then
  echo "  ⚠️  공인 IP 조회 실패 (외부 네트워크 문제일 수 있습니다)"
elif [ "$ip" = "$expected" ]; then
  echo "  ✅ $ip"
else
  echo "  ⚠️  $ip  ← 접속에 쓴 $expected 와 다릅니다."
  echo "      빗썸에는 위의 $ip 를 등록해야 합니다."
fi

echo ""
echo "── 저장된 봇 ──────────────────────────"
f="$HOME/gemini_alpha_lab/data/bots.json"
if [ -f "$f" ]; then
  echo "  파일: $f ($(stat -c %s "$f" 2>/dev/null) bytes, 수정 $(stat -c %y "$f" 2>/dev/null | cut -d. -f1))"
  python3 - "$f" <<'PY' 2>/dev/null || echo "  (내용 파싱 실패)"
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
bots=d.get("bots") or []
if not bots:
    print("  저장된 봇 없음")
for b in bots:
    pos=b.get("pos") or {}
    held="포지션 보유" if pos.get("open") else "대기"
    print(f"  · {b.get('coin')} / {b.get('mode')} / {b.get('status')} — {held}")
PY
else
  echo "  아직 없음 (봇을 한 번도 가동하지 않았습니다)"
fi

echo ""
echo "── 최근 로그 10줄 ─────────────────────"
journalctl -u bithumb-bot -n 10 --no-pager 2>/dev/null | sed 's/^/  /'

echo ""
echo "── 디스크 / 메모리 ────────────────────"
df -h / | tail -1 | awk '{print "  디스크: "$3" / "$2" 사용 ("$5")"}'
free -m | awk '/Mem:/{print "  메모리: "$3"MB / "$2"MB 사용"}'
REMOTE

echo ""
printf '\033[1;33m────────────────────────────────────────────\033[0m\n'
read -p "Enter 를 누르면 창이 닫힙니다. " _
