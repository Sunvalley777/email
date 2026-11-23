#!/usr/bin/env bash
# 서버에서 실행: cd ~/mail-collector/webapp && bash install.sh
set -e
cd "$(dirname "$0")"
VENV=../venv/bin
echo "[1/5] 패키지 설치 (수집기 가상환경 공유)"
$VENV/pip install -q -r requirements.txt
$VENV/pip install -q "cryptography>=41"

echo "[2/5] 키 생성"
if ! grep -q '^FERNET_KEY=' ../.env; then
  echo "FERNET_KEY=$($VENV/python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" >> ../.env
  echo "  FERNET_KEY 생성 → ../.env (분실 시 저장된 비밀번호 복호화 불가, 백업 권장)"
fi
[ -f .env ] || cp .env.example .env
if grep -q 'install.sh가_자동생성' .env; then
  sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$($VENV/python -c 'import secrets;print(secrets.token_urlsafe(48))')|" .env
fi
chmod 600 .env ../.env

echo "[3/5] HTTPS 인증서 (사내용 자체 서명, 10년)"
mkdir -p certs
if [ ! -f certs/cert.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=172.1.2.42" -addext "subjectAltName=IP:172.1.2.42,DNS:redmine-server" 2>/dev/null
  chmod 600 certs/key.pem
fi

echo "[4/5] DB 스키마 (mail.account, mail.audit_log)"
$VENV/python - <<'P'
import os, psycopg
from pathlib import Path
for p in ("../.env", ".env"):
    for l in Path(p).read_text(encoding="utf-8").splitlines():
        if "=" in l and not l.strip().startswith("#"):
            k, v = l.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
with psycopg.connect(os.environ["PG_DSN"]) as c:
    c.execute(Path("schema_p2.sql").read_text(encoding="utf-8"))
print("  스키마 OK")
P

echo "[5/5] 완료. 시험 실행:"
echo "  $VENV/uvicorn app:app --host 0.0.0.0 --port 8443 --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem"
echo "  브라우저: https://172.1.2.42:8443  (자체 서명 인증서 경고 → '고급 → 계속')"
