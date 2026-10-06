# VPS 설치 안내

빗썸은 API 키에 **IP 등록을 요구**합니다. 가정·사무실 회선은 IP 가 수시로 바뀌고
(실측: 하루 사이 `x.x.x.x` → `x.x.x.x` → `x.x.x.x`),
PaaS(Render·Heroku 류)는 아웃바운드가 공용 대역이라 등록할 수 없습니다.
**24시간 실전 매매에는 고정 IP 를 가진 VPS 가 필요합니다.**

---

## 1. VPS 만들기

국내 업체 기준 권장 사양입니다. 이 프로그램은 가볍습니다.

| 항목 | 권장 | 비고 |
| :--- | :--- | :--- |
| OS | **Ubuntu 22.04 LTS** | 설치 스크립트가 이 기준 |
| CPU / RAM | 1 vCPU / 1GB | 봇 10개까지 여유 |
| 디스크 | 20GB | 봇 상태 저장에 필요 |
| 리전 | **한국** | 빗썸까지 지연이 짧습니다 |
| 공인 IP | **고정 IP 필수** | 유동 IP 면 의미가 없습니다 |

> 신청 화면에서 **"고정 IP"** 또는 **"공인 IP 할당"** 옵션을 반드시 확인하세요.
> 업체에 따라 별도 신청·과금입니다. 이게 없으면 VPS 를 쓰는 이유가 사라집니다.

방화벽(보안그룹)은 **SSH(22번)만** 열어두면 됩니다. 앱은 외부에 노출하지
않고 SSH 터널로 접속합니다.

---

## 2. 설치

VPS 에 SSH 로 접속한 뒤:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/jsy15757684/gemini_alpha_lab.git
cd gemini_alpha_lab
sudo bash deploy/setup.sh
```

스크립트가 파이썬 환경, 의존성, `.env`, systemd 서비스까지 한 번에 처리합니다.
서비스는 **전용 시스템 계정(`bithumb`)** 으로 돌아가며, 코드는 읽기 전용이고
쓰기는 `data/` 에만 허용됩니다. 앱이 뚫려도 root 가 되지 않고 자기 코드도
고칠 수 없습니다. 이미 root 로 설치해 두었다면 `/opt` 로 옮기고 계정을
바꾸는 작업이 필요합니다.
끝나면 **이 서버의 공인 IP** 를 출력합니다. 그 값을 적어두세요.

---

## 3. 설정값 채우기

```bash
nano ~/gemini_alpha_lab/.env
```

최소 세 줄만 채우면 됩니다. `=` 뒤에 공백을 두지 마세요.

```
APP_ACCESS_PASSWORD=20자이상의값
BITHUMB_API_KEY=빗썸에서발급한값
BITHUMB_SECRET_KEY=빗썸에서발급한값
```

저장 후:

```bash
sudo systemctl restart bithumb-bot
sudo systemctl status bithumb-bot
```

---

## 4. 빗썸에 IP 등록

빗썸 **[API 관리 > IP 주소 등록]** 에 2번에서 출력된 **VPS 의 공인 IP** 를 넣습니다.
기존에 등록해 둔 집·사무실 IP 는 지워도 됩니다.

확인:

```bash
curl -s https://api.ipify.org && echo
```

이 값과 빗썸에 등록한 값이 같아야 합니다.

---

## 5. 화면 접속

앱은 `x.x.x.x` 에만 바인딩되어 있어 외부에서 직접 열 수 없습니다.
**의도된 설계입니다** — 실계좌 주문 권한을 가진 콘솔을 인터넷에 그대로
노출하지 않기 위해서입니다.

내 PC 에서 SSH 터널을 엽니다:

```bash
ssh -L 8888:127.0.0.1:8888 ubuntu@서버IP
```

터널이 열린 상태로 브라우저에서 **http://localhost:8888** 접속.

> 외부에서 바로 접속하고 싶다면 Nginx + Let's Encrypt 로 HTTPS 리버스 프록시를
> 두세요. 그 경우 반드시 HTTPS 여야 합니다 — 세션 쿠키의 `Secure` 속성이
> HTTPS 에서만 붙습니다.

---

## 6. 운영

```bash
sudo systemctl status bithumb-bot     # 상태
sudo systemctl restart bithumb-bot    # 재시작
sudo journalctl -u bithumb-bot -f     # 실시간 로그
```

### 크립토 / 나무증권 두 서비스로 나눴을 때 (`deploy/split-services.sh`)

| 서비스 | 역할 | 주소 |
|---|---|---|
| `bithumb-bot` | 화면 · 로그인 · 빗썸 봇 (`APP_ROLE=crypto`) | 127.0.0.1:8888 |
| `bithumb-namuh` | 나무증권 봇 — 해외 무한매수 (`APP_ROLE=namuh`) | 127.0.0.1:8889 (내부 전용) |

```bash
sudo systemctl restart bithumb-bot bithumb-namuh      # 둘 다 재시작
sudo journalctl -u bithumb-namuh -f                    # 나무증권 쪽 로그
sudo bash deploy/split-services.sh --undo             # 한 서비스로 되돌리기
```

한쪽만 재시작하면 그쪽 코드만 바뀐다. 화면만 고친 업데이트면 `bithumb-bot` 만
재시작해도 되고, 그때 나무증권 봇(미국장 LOC 창 포함)은 끊기지 않는다.

프로세스가 죽으면 **5초 뒤 자동 재시작**되고, 서버가 재부팅돼도 자동으로 뜹니다.
봇 상태는 `data/bots.json` 에 저장되어 재시작 후 복원되며, 실전 봇이 포지션을
들고 있었다면 **빗썸 실제 보유량과 대조한 뒤에만** 재가동합니다.

### 업데이트

```bash
cd /opt/gemini_alpha_lab && git pull && bash deploy/fix-perms.sh && systemctl restart bithumb-bot
# 두 서비스로 나눴다면
cd /opt/gemini_alpha_lab && git pull && bash deploy/fix-perms.sh && systemctl restart bithumb-bot bithumb-namuh
```

`git pull` 은 root 로 실행되므로 새 파일이 root 소유로 생깁니다. `fix-perms.sh`
가 서비스 계정이 읽을 수 있도록 소유권을 되돌립니다. 빠뜨리면 업데이트 후
서비스가 뜨지 않을 수 있습니다.

### 텔레그램 알림 — 매일 아침 점검 보고 (`deploy/daily-report.sh`)

월~토 09:35(한국시간)에 서버가 혼자 점검해 텔레그램으로 보냅니다. 맥 · Claude 앱이
꺼져 있어도 됩니다. 읽기만 합니다(주문 · 봇 변경 없음).

- 해외 봇: **봇 장부 수량 = 계좌 수량** 인지 · 지난 24시간 체결
- 서버: 두 서비스 상태 · 지난 24시간 ERROR · 나무증권 토큰 남은 시간
- 이상이 있으면 첫 줄이 `🚨 확인 필요: …` 로 시작합니다

1. 텔레그램에서 **@BotFather** → `/newbot` → 이름을 정하면 봇 토큰을 줍니다.
2. 서버 `.env` 에 한 줄 넣습니다 (토큰은 채팅 · 코드에 붙이지 마세요):
   ```
   TELEGRAM_BOT_TOKEN=<BotFather 가 준 토큰>
   ```
3. 텔레그램에서 방금 만든 봇에게 아무 말(`/start`)이나 보낸 뒤, 받을 대화의 번호를 찾습니다:
   ```bash
   sudo -u bithumb /opt/gemini_alpha_lab/venv/bin/python /opt/gemini_alpha_lab/scripts/daily_report.py --find-chat
   ```
   나온 `TELEGRAM_CHAT_ID=...` 줄을 `.env` 에 넣습니다.
4. 연결 확인 → 타이머 걸기:
   ```bash
   sudo -u bithumb /opt/gemini_alpha_lab/venv/bin/python /opt/gemini_alpha_lab/scripts/daily_report.py --ping
   sudo bash deploy/daily-report.sh            # 되돌리기: --undo
   sudo systemctl start gal-daily-report.service   # 지금 한 번 보내 보기
   ```

보내지 않고 내용만 보려면 `--dry-run`. 봇 토큰이 새어 나갔다고 생각되면
@BotFather 에서 `/revoke` 로 바꾸고 `.env` 를 고칩니다.

---

## 나무증권 실계좌 소액 시험 (`scripts/namuh_live_smoke.py`)

모의계좌로는 확인할 수 없는 경로(실계좌 주문 도메인 · LOC 접수와 정산 · 원화 증거금)를
UPRO 1주로 태워 봅니다. **실제 돈이 나갑니다** (1주 왕복마다 호가 차이 + 수수료).
봇을 실계좌로 만들기 **전에**, `.env` 를 `NAMUH_MOCK=0` 으로 바꾸고 서비스를 재시작한 뒤에 합니다.
모의 봇은 그 전에 지워 둡니다(모의 장부를 실계좌로 들고 가지 않게).

단계마다 따로, 터미널을 붙여(`ssh -t`) 실행합니다. 주문 직전에 `UPRO` 를 직접 쳐야 진행됩니다.

```bash
ssh -t <서버>
cd /opt/gemini_alpha_lab
sudo -u bithumb env $(grep -E '^NAMUH_' .env | xargs) venv/bin/python scripts/namuh_live_smoke.py check
```

| 언제 (한국시간 · 서머타임 중) | 단계 | 확인하는 것 |
|---|---|---|
| 아무 때 | `check` | 실전 계좌로 확인되는지 · 잔고 · UPRO 0주 (주문 없음) |
| 1일차 22:30 정규장 시작 뒤 | `buy-sell` | 실계좌 지정가 매수 → 체결 → 매도 |
| 1일차 같은 시간 | `cancel` | 체결 안 될 지정가 → 코드가 스스로 취소 |
| 1일차 (원화로 살 계획이면) | `krw` | 원화 증거금(통합증거금) 주문이 받아지는지 |
| 1일차 04:45 전 (15:45 ET) | `loc` | LOC 1주 접수 (현재가 +3% 한도 → 마감가에 체결) |
| 2일차 05:00 마감 뒤 | `loc-check` | 마감에 1주 체결됐는지 잔고로 확인 |
| 2일차 22:30 정규장 시작 뒤 | `sell` | LOC 로 산 1주 정리 |

멈추는 경우: 모의계좌 모드 · 계좌가 실전으로 확인 안 됨 · UPRO 를 쓰는 봇이 있음 ·
UPRO 보유가 예상과 다름 · 1주 $400 초과 · 장이 닫힘 · 터미널이 아님 · 승인 입력이 다름 ·
주문 결과를 모름(그때는 나무증권 앱에서 체결 · 미체결을 확인). 결과는 `data/live_smoke_log.jsonl`.

---

## 실전 전 최종 점검

- [ ] `curl -s https://api.ipify.org` 값이 빗썸에 등록한 IP 와 일치
- [ ] 화면 `빗썸 계정` 탭에서 **인증 확인: 성공** 과 잔고가 보임
- [ ] `sudo systemctl status bithumb-bot` 이 `active (running)`
- [ ] 재부팅 후에도 자동으로 뜨는지 확인 (`sudo reboot` 후 재접속)
- [ ] **모의투자로 며칠 돌려** 전략이 실제로 어떻게 행동하는지 확인
- [ ] 실전은 **소액부터**. 실주문 경로는 실계좌로 체결까지 검증된 적이 없습니다
