#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "=========================================================="
echo " 🚀 Gemini Alpha Lab - AI 퀀트 & 스마트 인베스트먼트 OS"
echo "=========================================================="

# 가상환경 감지
if [ -d "/Users/jay_mac/my_ai_system/ai_env" ]; then
    PYTHON="/Users/jay_mac/my_ai_system/ai_env/bin/python3"
elif [ -d "../ai_env" ]; then
    PYTHON="../ai_env/bin/python3"
elif [ -d "venv" ]; then
    PYTHON="venv/bin/python3"
else
    PYTHON="python3"
fi

echo "Using Python: $PYTHON"
echo "Starting Gemini Alpha Lab Server at http://localhost:8888 ..."

# 백그라운드로 1초 뒤 브라우저 열기
(sleep 1.5 && open "http://localhost:8888") &

$PYTHON -m uvicorn server:app --host 0.0.0.0 --port 8888 --reload
