# 지텍 AI 메일 관리 시스템 P1: 분류·요약

```
 mail.message (수집기 P0)
      │
      ▼ ① 규칙 분류 (도메인·헤더)          rules.json
      ├─ 뉴스레터 ──────────────► 제목만 저장 (LLM 생략)
      ├─ 정부과제 ─┐
      ├─ 사내업무 ─┼─► ② Ollama (로컬) ─► 요약·세부분류·마감일·할 일 추출
      └─ 외부협력 ─┘                     bge-m3 임베딩
      ▼
 mail.ai_result  +  view: mail.v_deadlines / mail.v_actions
```

## 배포

### 1) PC → 서버 복사 (PowerShell)
```powershell
scp -r C:\work\email\classifier redmine@172.1.2.42:~/mail-collector/
```

### 2) Ollama와 모델 확인 (서버)
```bash
cd ~/mail-collector/classifier
cp .env.example .env
../venv/bin/python classify.py --check
```
- Ollama가 없으면: `curl -fsSL https://ollama.com/install.sh | sh`
- 모델이 없으면: `ollama pull qwen2.5:7b` / `ollama pull bge-m3`
- Ollama가 Docker 등 다른 주소에 있으면 `.env`의 `OLLAMA_URL` 수정

### 3) 스키마 확장 → 시험 → 전체
```bash
../venv/bin/python classify.py --init-db
../venv/bin/python classify.py --limit 20
../venv/bin/python classify.py --report
nohup ../venv/bin/python classify.py --limit 2000 > classify_full.log 2>&1 &   # 전체 (백그라운드)
tail -f classify_full.log
```

### 4) 10분 주기 자동 실행
```bash
sudo cp systemd/mail-classifier.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-classifier.timer
```

## 운영
| 작업 | 명령 |
|---|---|
| 현황·마감·할 일 보기 | `classify.py --report` |
| 규칙 수정 후 재분류 | `rules.json` 수정 → `classify.py --reset` → `classify.py --limit 2000` |
| LLM 실패 건 재처리 | `docker exec labrag-pg psql -U rag -d labrag -c "DELETE FROM mail.ai_result WHERE error IS NOT NULL"` 후 재실행 |
| 실행 로그 | `journalctl -u mail-classifier -n 50` |

모든 처리는 서버 내부의 Ollama에서 이루어지며, 메일 내용이 외부 클라우드로 전송되지 않습니다.
