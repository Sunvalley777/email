# 지텍 AI 메일 비서 (파일럿)

다우오피스(IMAP) 메일을 사내 서버에서 수집·분류·요약하고 웹·PC·텔레그램으로 알리는 시스템.

| 폴더 | 내용 | 서버 위치 |
|---|---|---|
| `collector/` | IMAP 수집기 (2분 주기, 읽기 전용) | `~/mail-collector/` (루트로 복사) |
| `classifier/` | 규칙 + 로컬 LLM(Ollama) 분류, OCR, 재분류 | `~/mail-collector/classifier/` |
| `webapp/` | FastAPI 웹 앱 (HTTPS 8443) | `~/mail-collector/webapp/` |
| `notifier/` | 알림기 (웹·PC·텔레그램, 1분 주기) | `~/mail-collector/notifier/` |
| `pc-alert/` | PC 바탕화면 알림 프로그램 · 사용자 안내서 | 각 PC |
| `design/` | 화면 시안 · 지텍 로고 | - |
| `deploy.ps1` | PC → 서버 배포 (SSH 키) | - |

- 비밀 정보(`.env`, 인증서)와 메일 데이터(`data/`)는 저장소에 넣지 않는다 → `.gitignore`
- 서버 설정 예시는 각 폴더의 `.env.example`
- 커밋 규칙: 연구소 git-convention (`type(scope): 한국어 요약`)
