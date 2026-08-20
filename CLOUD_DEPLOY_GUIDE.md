# ☁️ 내 PC를 끄고 24시간 365일 무중단 자동매매 돌리는 2가지 방법

이 가이드를 따라 하시면 **내 컴퓨터를 완전히 끄거나 노트북을 덮어도**, 봇이 24시간 내내 거래소/증권사 시세를 감시하며 자동으로 매매를 수행합니다.

---

## 🌟 방법 1. [가장 추천 / 100% 무료] Render.com에 5분 만에 올리기 (가장 쉬움)

별도의 리눅스 서버 설정 없이, 웹사이트 클릭 몇 번으로 **무료 웹 주소(`https://내봇.onrender.com`)**를 받아 스마트폰으로도 24시간 접속할 수 있는 가장 쉬운 방법입니다.

### 📌 3단계 배포 순서:
1. **GitHub에 소스코드 올리기**:
   - GitHub(https://github.com)에 로그인 후 `gemini_alpha_lab` 폴더를 새 레포지토리로 푸시합니다.
2. **Render.com 가입 & 연결**:
   - [Render.com](https://render.com)에 접속하여 GitHub 계정으로 무료 회원가입합니다.
   - **`[New +]`** $\rightarrow$ **`[Web Service]`** 클릭 $\rightarrow$ 방금 올린 GitHub 레포지토리를 선택합니다.
3. **설정값 입력 후 `Deploy` 클릭**:
   - **Runtime**: `Python 3` 또는 `Docker`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: `Free (무료)`
   - 하단의 **`[Create Web Service]`**를 누르면 끝!

> ✅ **결과**: `https://gemini-alpha-lab.onrender.com` 같은 고유 주소가 생성되어, **내 PC를 꺼도 24시간 돌아가며 스마트폰으로도 접속**할 수 있습니다.

---

## 🏛️ 방법 2. [전문가형 / 월 0원 평생 무료] 오라클 클라우드 (Oracle Cloud VPS)

오라클 클라우드는 **평생 무료(Always Free)** 가상 리눅스 서버(VPS)를 제공합니다.

### 📌 설치 순서 (명령어 1줄로 끝):
1. **오라클 클라우드** 가입 후 무료 인스턴스(Ubuntu)를 1개 생성합니다.
2. 터미널(SSH)로 접속한 후, 아래 명령어를 복사해 붙여넣기만 하면 **Docker 컨테이너가 24시간 자동 가동**됩니다:

```bash
git clone https://github.com/내계정/gemini_alpha_lab.git
cd gemini_alpha_lab
bash deploy_cloud.sh
```

> ✅ `restart: always` 옵션이 적용되어 있어, 서버가 재부팅되어도 자동으로 봇이 살아납니다!

---

## 💡 방법 3. [초간단 월 5,000원] AWS 라이트세일 (Amazon Lightsail)

1. [AWS Lightsail](https://lightsail.aws.amazon.com) 접속 $\rightarrow$ **[인스턴스 생성]**
2. OS: `Linux (Ubuntu)` $\rightarrow$ 플랜: `월 $3.5 (첫 달 무료)` 선택
3. 브라우저에서 SSH 터미널 열기 $\rightarrow$ `bash deploy_cloud.sh` 실행!

---

## 📱 스마트폰 홈 화면에 앱처럼 추가하는 꿀팁

클라우드 배포가 완료되면 스마트폰 사파리/크롬으로 해당 주소에 접속한 후:
- 아이폰: **[공유] $\rightarrow$ [홈 화면에 추가]**
- 안드로이드: **[더보기(⋮)] $\rightarrow$ [홈 화면에 앱 추가]**
- 이렇게 하시면 **진짜 모바일 핀테크 앱처럼 언제 어디서나 1초 만에 열어서 실시간 봇 수익률을 확인**하실 수 있습니다!
