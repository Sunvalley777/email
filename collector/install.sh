#!/usr/bin/env bash
# 서버(redmine-server)에서 실행:  bash install.sh
set -e
cd "$(dirname "$0")"
echo "[1/4] Python 가상환경 생성"
python3 -m venv venv || { echo "python3-venv 필요: sudo apt install -y python3-venv"; exit 1; }
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
echo "[2/4] .env 확인"
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; echo "  .env 생성됨 → nano .env 로 비밀번호 입력 후 다시 실행"; exit 0; fi
chmod 600 .env
mkdir -p data
echo "[3/4] 접속 확인"
./venv/bin/python collector.py --check
echo "[4/4] DB 스키마 생성"
./venv/bin/python collector.py --init-db
echo "완료. 다음: 시험 수집 → ./venv/bin/python collector.py --limit 20"
