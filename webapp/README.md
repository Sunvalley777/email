# 지텍 AI 메일 비서: 파일럿 웹 앱 (A방식)

```
 직원 브라우저 ──HTTPS──► webapp (uvicorn :8443)
   ① 로그인: 그룹웨어 ID/PW → IMAP 접속으로 본인 확인
   ② 동의: 최초 1회 → 비밀번호 Fernet 암호화 → mail.account
   ③ 대시보드: 본인 메일만 (마감 · 조치 필요 · 행사 · 분류별 목록)
                    │
 mail.account ──► collector.py (5분, 등록 계정 순회) ──► classify.py (10분)
```

## 서버 폴더 구성
```
~/mail-collector/
 ├─ collector.py, .env (PG_DSN, FERNET_KEY)   ← 수집기 (다중 계정 버전으로 교체)
 ├─ venv/                                     ← 공용 가상환경
 ├─ classifier/                               ← 분류기 (전 계정 처리 버전으로 교체)
 └─ webapp/                                   ← 웹 앱 (신규)
```

## 배포

### 1) PC → 서버 복사 (PowerShell)
```powershell
scp C:\work\email\collector\collector.py C:\work\email\collector\requirements.txt redmine@172.1.2.42:~/mail-collector/
scp C:\work\email\classifier\classify.py redmine@172.1.2.42:~/mail-collector/classifier/
scp -r C:\work\email\webapp redmine@172.1.2.42:~/mail-collector/
```

### 2) 설치 (서버)
```bash
cd ~/mail-collector/webapp
sed -i 's/\r$//' install.sh
bash install.sh
nano .env            # ADMIN_ACCOUNTS 확인
```

### 3) 시험 실행 → 브라우저 접속
```bash
../venv/bin/uvicorn app:app --host 0.0.0.0 --port 8443 --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
```
`https://172.1.2.42:8443` 접속 → 인증서 경고에서 "고급 → 계속". 확인 후 Ctrl+C.

### 4) 상시 실행 등록
```bash
sudo cp systemd/mail-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-web
sudo systemctl restart mail-collector.timer mail-classifier.timer
```
방화벽을 사용 중이면 `sudo ufw allow 8443/tcp`.

## 보안 설계
| 항목 | 처리 |
|---|---|
| 비밀번호 | Fernet(AES) 암호화 저장, 키는 서버 `.env`(권한 600). 동의 전에는 서버 메모리에만 10분 보관 |
| 비밀번호 오류 | 수집기가 즉시 해당 계정 수집 중단 (그룹웨어 계정 잠김 방지) → 앱 재로그인 시 자동 재개 |
| 열람 범위 | 모든 조회가 로그인 계정 기준. 다른 사람 메일·첨부 접근 시 404 |
| 관리자 | 계정별 건수·오류만 표시, 메일 내용 열람 불가 |
| 세션 | HTTPS 전용 쿠키, 8시간 만료, CSRF 토큰, 로그인 5회 실패 시 10분 차단 |
| 감사 로그 | 로그인·동의·철회·삭제를 `mail.audit_log`에 기록 |
| 철회 | 수집 중지(비밀번호 삭제) / 데이터 전체 삭제(DB+파일) |

## 주의
- `FERNET_KEY`를 잃어버리면 저장된 비밀번호를 복호화할 수 없습니다. 이 경우 각자 다시 로그인하면 복구됩니다. 별도 백업을 권장합니다.
- 파일럿 대상자에게 동의 절차를 사전 안내하고, 사내 정보보안 규정·법무 검토를 거쳐 확대하세요.
