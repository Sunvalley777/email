# 지텍 AI 메일 관리 시스템 P0: 메일 수집기

```
 다우오피스(gw.z-tec.co.kr:993) ──IMAP 읽기전용, 5분 주기──► collector.py (redmine-server)
                                                         ├─► labrag-pg  mail.* 테이블
                                                         └─► data/  원본 .eml + 첨부
```

## 파일 구성
| 파일 | 내용 |
|---|---|
| collector.py | 수집기 (증분 수집, 한글 헤더·본문 디코딩, 첨부 저장) |
| schema.sql | DB 스키마 `mail.sync_state / message / attachment / ai_result` |
| .env.example | 접속 정보 템플릿 (비밀번호는 서버의 .env에만 저장) |
| install.sh | 가상환경 생성, 접속 확인, 스키마 생성 |
| systemd/ | 5분 주기 자동 실행 타이머 |

## 배포 순서

### 1) PC → 서버 복사 (PowerShell)
```powershell
scp -r C:\work\email\collector redmine@172.1.2.42:~/mail-collector
```

### 2) 서버 설치 (ssh redmine@172.1.2.42)
```bash
cd ~/mail-collector
sed -i 's/\r$//' install.sh          # Windows 줄바꿈 제거
bash install.sh                       # 처음 실행 시 .env만 생성되고 멈춤
nano .env                             # IMAP_PASS, PG_DSN 입력
bash install.sh                       # 접속 확인 + 스키마 생성
```
PG_DSN의 dbname과 user는 labrag-pg 실제 값으로 바꿔야 합니다. 확인 명령:
`docker exec labrag-pg env | grep POSTGRES`

### 3) 시험 수집 → 전체 적재
```bash
./venv/bin/python collector.py --limit 20   # 폴더당 20건만
./venv/bin/python collector.py --stats      # 결과 확인
./venv/bin/python collector.py              # 나머지 전체 (Inbox 740건 + Sent)
```

### 4) 자동 실행 등록 (5분 주기)
```bash
sudo cp systemd/mail-collector.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-collector.timer
systemctl list-timers | grep mail          # 다음 실행 시각 확인
journalctl -u mail-collector -n 50         # 실행 로그
```

## 안전 장치
- 폴더를 읽기 전용(EXAMINE)으로 열고 BODY.PEEK로 가져오므로 **읽음 표시나 메일함이 변경되지 않습니다**.
- 재실행해도 중복 적재되지 않습니다 (UIDVALIDITY + UID 기준).
- `.env` 권한은 600 (redmine 계정만 읽을 수 있음).
- DRM 의심 첨부는 `is_drm=true`로 표시해 P1 단계에서 로컬 LLM으로만 처리합니다.
