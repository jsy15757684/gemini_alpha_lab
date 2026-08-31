# 맥에서 더블클릭으로 쓰는 스크립트

VPS 에 올린 콘솔을 맥에서 열고 점검하는 파일입니다. Finder 에서 더블클릭하면
터미널이 열리면서 실행됩니다.

## 준비 (한 번만)

```bash
cd ~/gemini_alpha_lab/deploy/mac
cp server.conf.example server.conf
nano server.conf          # SERVER_HOST 에 VPS 공인 IP 를 넣습니다
```

`server.conf` 는 `.gitignore` 대상입니다. **서버 주소를 공개 저장소에 올리지
않으려는 의도**이므로 이 파일을 커밋하지 마세요.

데스크탑 등 편한 곳에 `.command` 파일을 복사해 두어도 됩니다. 스크립트는
자기 옆과 `~/gemini_alpha_lab/deploy/mac/` 두 곳에서 `server.conf` 를 찾습니다.

## 봇 콘솔 열기.command

SSH 터널을 열고 브라우저까지 띄웁니다.

1. 포트가 이미 쓰이는지 확인 — 맥 서버가 붙어 있으면 끌지 물어봅니다
2. VPS 가 살아 있는지 확인
3. 터널 연결 (비밀번호 입력)
4. 앱이 `{"status":"ok"}` 로 응답한 뒤에 브라우저를 엽니다
5. 창을 닫으면 터널만 끊깁니다 — **서버의 봇은 계속 돕니다**

터널은 `127.0.0.1` 에만 바인딩하고 `ExitOnForwardFailure=yes` 를 줍니다.
포트가 겹칠 때 조용히 다른 서버로 넘어가는 대신 그 자리에서 실패하게 하려는
장치입니다. `localhost` 는 IPv6(`::1`)를 먼저 쓰기 때문에, 맥 서버와 터널이
같은 포트에 붙으면 **같은 주소가 다른 서버를 가리키는** 상황이 실제로 생깁니다.

## 봇 상태 확인.command

브라우저를 열지 않고 "24시간 돌 상태인가" 만 점검합니다.

- systemd 자동시작(`enabled`) / 가동중(`active`) / 재시작 횟수
- 앱 응답
- 서버 공인 IP 가 접속에 쓴 IP 와 같은지 (빗썸 등록값 확인)
- 저장된 봇 목록과 포지션 보유 여부
- 최근 로그 10줄, 디스크·메모리

## 참고

매번 비밀번호를 넣기 번거로우면 SSH 키를 등록하세요.

```bash
ssh-keygen -t ed25519            # 키가 없다면
ssh-copy-id root@<서버IP>
```
