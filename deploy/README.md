# VPS 설치 안내

빗썸은 API 키에 **IP 등록을 요구**합니다. 가정·사무실 회선은 IP 가 수시로 바뀌고
(실측: 하루 사이 `1.232.202.142` → `49.167.237.209` → `210.178.114.122`),
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

앱은 `127.0.0.1` 에만 바인딩되어 있어 외부에서 직접 열 수 없습니다.
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

프로세스가 죽으면 **5초 뒤 자동 재시작**되고, 서버가 재부팅돼도 자동으로 뜹니다.
봇 상태는 `data/bots.json` 에 저장되어 재시작 후 복원되며, 실전 봇이 포지션을
들고 있었다면 **빗썸 실제 보유량과 대조한 뒤에만** 재가동합니다.

### 업데이트

```bash
cd ~/gemini_alpha_lab && git pull && sudo bash deploy/setup.sh
```

---

## 실전 전 최종 점검

- [ ] `curl -s https://api.ipify.org` 값이 빗썸에 등록한 IP 와 일치
- [ ] 화면 `빗썸 계정` 탭에서 **인증 확인: 성공** 과 잔고가 보임
- [ ] `sudo systemctl status bithumb-bot` 이 `active (running)`
- [ ] 재부팅 후에도 자동으로 뜨는지 확인 (`sudo reboot` 후 재접속)
- [ ] **모의투자로 며칠 돌려** 전략이 실제로 어떻게 행동하는지 확인
- [ ] 실전은 **소액부터**. 실주문 경로는 실계좌로 체결까지 검증된 적이 없습니다
