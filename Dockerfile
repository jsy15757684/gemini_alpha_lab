FROM python:3.9-slim

# 작업 디렉토리 설정
WORKDIR /app

# 시스템 필수 패키지 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 파이썬 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스코드 전체 복사
COPY . .

# 포트는 환경변수 $PORT 로 받는다. 없으면 8888.
EXPOSE 8888
ENV PORT=8888

# 24시간 무중단 uvicorn 서버 실행 ($PORT 를 반영하려면 shell form 이 필요하다)
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8888}
