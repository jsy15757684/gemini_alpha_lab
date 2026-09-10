#!/bin/bash
# 비밀번호 지문 계산 — 서버의 비밀번호와 같은 값인지 대조한다.
#
# 비밀번호 자체는 아무 데도 보내지 않는다. 화면에도 안 찍고, 파일로도 안 남긴다.
# 서버가 기동 로그에 남기는 지문과 같은 방식(services/auth.py 와 동일한 솔트)으로
# 계산해서, '내가 치는 값' 과 '서버가 가진 값' 이 같은지만 판정한다.

printf '\033[1;36m'
cat <<'BANNER'
────────────────────────────────────────────
  비밀번호 지문 확인
────────────────────────────────────────────
BANNER
printf '\033[0m'
cat <<'INTRO'

서버 쪽 지문을 먼저 확인하세요. 서버에 접속해서:

  journalctl -u bithumb-bot | grep 지문 | tail -1

이런 줄이 나옵니다:
  APP_ACCESS_PASSWORD 로드됨 · 길이 20자 · 지문 abcd1234

아래에 비밀번호를 입력하면 같은 방식으로 계산해 보여줍니다.
길이와 지문이 둘 다 같으면 같은 비밀번호입니다.

INTRO

printf '비밀번호 (화면에 안 보입니다): '
IFS= read -rs pw
echo ""
echo ""

if [ -z "$pw" ]; then
  echo "  입력이 없습니다."
  echo ""
  read -p "Enter 를 누르면 창이 닫힙니다. " _
  exit 1
fi

PW="$pw" python3 - <<'PY'
import hashlib, os, sys

SALT = b"gemini-alpha-lab/password-fingerprint/v1"   # services/auth.py 와 동일
pw = os.environ["PW"]

def fp(s):
    return hashlib.sha256(SALT + s.encode("utf-8")).hexdigest()[:8]

print(f"  입력한 값        길이 {len(pw)}자 · 지문 {fp(pw)}")

# 흔한 실수를 같이 보여준다. 앞뒤 공백 하나로 인증이 막힌 적이 있다.
st = pw.strip()
if st != pw:
    print(f"  앞뒤 공백 제거   길이 {len(st)}자 · 지문 {fp(st)}")
    print("")
    print("  ⚠️  입력값에 앞뒤 공백이 있습니다. 서버 .env 의 '=' 뒤 공백이")
    print("      원인이었던 적이 있습니다. 서버 지문이 아래쪽과 같다면")
    print("      .env 에서 공백을 지우세요.")
PY

cat <<'OUTRO'

  판정 방법
    길이·지문 둘 다 일치  → 비밀번호는 맞습니다. 다른 원인입니다.
    길이가 다름            → 서버 .env 값이 내가 아는 값과 다릅니다.
    길이 같고 지문 다름    → 보이지 않는 문자(공백·특수문자) 차이입니다.

OUTRO
read -p "Enter 를 누르면 창이 닫힙니다. " _
