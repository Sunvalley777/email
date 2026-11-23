-- =====================================================================
-- 지텍 AI 메일 관리 시스템 P1 - 분류·요약 확장 (여러 번 실행해도 안전)
-- =====================================================================
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS subtype   TEXT;   -- 세부 분류
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS rule_hit  TEXT;   -- 적용된 규칙 (domain:keit.re.kr 등)
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS deadline  DATE;   -- 마감일 (공고·요청)
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS action    TEXT;   -- 해야 할 일
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS error     TEXT;   -- LLM 처리 오류
CREATE INDEX IF NOT EXISTS ix_ai_deadline ON mail.ai_result (deadline);

-- 다가오는 마감 (오늘 이후)
CREATE OR REPLACE VIEW mail.v_deadlines AS
SELECT r.deadline, r.category, r.subtype, r.customer, m.subject, r.action, m.sent_at, m.id AS mail_id
FROM mail.ai_result r JOIN mail.message m ON m.id = r.message_id
WHERE r.deadline >= CURRENT_DATE
ORDER BY r.deadline;

-- 조치 필요 메일
CREATE OR REPLACE VIEW mail.v_actions AS
SELECT m.sent_at, r.category, r.customer, m.from_name, m.subject, r.action, r.deadline, m.id AS mail_id
FROM mail.ai_result r JOIN mail.message m ON m.id = r.message_id
WHERE r.action_needed
ORDER BY m.sent_at DESC;

-- 이미지 메일 OCR 결과 (이미지 속 글자 → AI 분류·요약에 함께 사용)
CREATE TABLE IF NOT EXISTS mail.ocr (
    message_id  BIGINT PRIMARY KEY REFERENCES mail.message(id) ON DELETE CASCADE,
    text        TEXT,
    n_images    INT,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
