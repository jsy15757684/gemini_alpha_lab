#!/bin/bash
set -e

echo "========================================================"
echo "🚀 Gemini Alpha Lab 24H 무중단 클라우드 자동 배포 스크립트"
echo "========================================================"

# Docker 설치 여부 확인
if ! command -v docker &> /dev/null; then
    echo "📦 Docker가 설치되어 있지 않습니다. 자동 설치를 진행합니다..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    echo "✅ Docker 설치 완료!"
fi

# Docker Compose 설치 여부 확인
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "📦 Docker Compose를 설정합니다..."
    sudo apt-get update && sudo apt-get install -y docker-compose-plugin
fi

echo "⚡ 24시간 무중단 자동매매 봇 컨테이너를 빌드 및 백그라운드 가동합니다..."
docker compose down || true
docker compose up --build -d

echo "========================================================"
echo "🎉 배포 성공! 이제 내 PC를 완전히 꺼도 봇이 24시간 돌아갑니다!"
echo "• 웹 대시보드 주소: http://$(curl -s ifconfig.me):8888"
echo "• 실시간 로그 확인: docker compose logs -f"
echo "========================================================"
