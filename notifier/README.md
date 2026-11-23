# 텔레그램 알림 (notifier)

```
 수집기(2분) → 분류(수집 직후) → notifier(1분) → 텔레그램 봇 → 직원 휴대폰
                                    ↑ 사용자별 규칙 · 조용한 시간 · 같은 공고 1회 · 마감 D-3/D-1
```
메일 도착 → 알림까지 약 2~4분.

## 1) 봇 만들기 (관리자 1회, 휴대폰 텔레그램)
1. `@BotFather` 검색 → 대화 시작 → `/newbot`
2. 봇 이름: `지텍 메일비서` / 사용자명: `ztec_mail_bot` 처럼 `_bot`으로 끝나는 고유 이름
3. 받은 토큰(`1234567890:AA...`)을 서버 `~/mail-collector/.env`에 추가:
   ```
   TELEGRAM_BOT_TOKEN=1234567890:AA...
   ```
   토큰은 채팅·메일로 공유하지 말 것 (유출 시 BotFather `/revoke`)

## 2) 서버 설치
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.telegram.org    # 302 또는 200이면 외부 접속 가능
cd ~/mail-collector/notifier
../venv/bin/python notifier.py --check
../venv/bin/python notifier.py --init-db
sudo cp systemd/mail-notifier.* /etc/systemd/system/
sudo cp ../systemd/mail-collector.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-notifier.timer
sudo systemctl restart mail-collector.timer mail-web
```

## 3) 직원 사용
웹 앱 상단 [⚙️ 알림 규칙·연결] → [텔레그램 연결 시작] → 링크 열기 → 봇에서 [시작] → 연결 완료 메시지 수신

## 보안
- 기본 전송 내용: 분류 · 보낸 곳 · AI 할 일 · 마감 (제목은 '상세' 선택 시에만)
- 보안등급 L1(DRM 첨부) 메일은 항상 최소 정보만
- 메일 본문·첨부는 전송하지 않음. 링크는 사내망에서만 열림
- 수집 중지·데이터 삭제 시 텔레그램 연결도 함께 해제
