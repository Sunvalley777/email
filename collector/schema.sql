-- =====================================================================
-- 지텍 AI 메일 관리 시스템 P0 - 수집 스키마
-- 대상 DB : labrag-pg (172.1.2.42:5435)
-- 기존 RAG 테이블과 섞이지 않도록 별도 스키마 'mail' 사용
-- 여러 번 실행해도 안전 (IF NOT EXISTS)
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS mail;
CREATE EXTENSION IF NOT EXISTS vector;   -- labrag-pg에 이미 설치되어 있음

-- 폴더별 동기화 상태 (마지막 수집 UID)
CREATE TABLE IF NOT EXISTS mail.sync_state (
    account        TEXT        NOT NULL,
    folder         TEXT        NOT NULL,
    uidvalidity    BIGINT      NOT NULL,
    last_uid       BIGINT      NOT NULL DEFAULT 0,
    last_sync_at   TIMESTAMPTZ,
    last_count     INT         DEFAULT 0,
    last_error     TEXT,
    PRIMARY KEY (account, folder)
);

-- 메일 본문·헤더
CREATE TABLE IF NOT EXISTS mail.message (
    id             BIGSERIAL   PRIMARY KEY,
    account        TEXT        NOT NULL,
    folder         TEXT        NOT NULL,          -- Inbox / Sent
    uidvalidity    BIGINT      NOT NULL,
    uid            BIGINT      NOT NULL,
    message_id     TEXT,                           -- RFC Message-ID
    in_reply_to    TEXT,
    references_ids TEXT[],
    thread_key     TEXT,                           -- 스레드 묶음 키 (최초 Message-ID)
    subject        TEXT,
    from_name      TEXT,
    from_addr      TEXT,
    to_addrs       TEXT[],
    cc_addrs       TEXT[],
    sent_at        TIMESTAMPTZ,
    body_text      TEXT,                           -- text/plain 우선, 없으면 HTML→텍스트
    has_html       BOOLEAN     DEFAULT FALSE,
    size_bytes     INT,
    raw_path       TEXT,                           -- 서버에 저장한 원본 .eml 경로
    direction      TEXT,                           -- in / out
    collected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account, folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS ix_mail_msgid   ON mail.message (message_id);
CREATE INDEX IF NOT EXISTS ix_mail_thread  ON mail.message (thread_key);
CREATE INDEX IF NOT EXISTS ix_mail_sent    ON mail.message (sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_mail_from    ON mail.message (from_addr);

-- 첨부파일 (파일은 디스크, 메타데이터만 DB)
CREATE TABLE IF NOT EXISTS mail.attachment (
    id             BIGSERIAL   PRIMARY KEY,
    message_id     BIGINT      NOT NULL REFERENCES mail.message(id) ON DELETE CASCADE,
    filename       TEXT,
    content_type   TEXT,
    size_bytes     INT,
    sha256         TEXT,
    stored_path    TEXT,
    is_drm         BOOLEAN     DEFAULT FALSE,      -- DRM 의심 파일 (로컬 처리 전용)
    extracted_text TEXT                            -- P1에서 채움
);
CREATE INDEX IF NOT EXISTS ix_att_msg ON mail.attachment (message_id);

-- AI 처리 결과 (P0에서는 비어 있음, P1 분류·요약 단계에서 채움)
CREATE TABLE IF NOT EXISTS mail.ai_result (
    message_id     BIGINT      PRIMARY KEY REFERENCES mail.message(id) ON DELETE CASCADE,
    sec_level      TEXT,                           -- L1 기밀 / L2 내부 / L3 일반
    category       TEXT,                           -- 영업요청 / 기술문의 / AS / 협력사 / 정부과제 / 기타
    customer       TEXT,
    summary        TEXT,
    extracted      JSONB,                          -- 장비·사양·납기·금액·요청액션
    action_needed  BOOLEAN,
    model          TEXT,
    embedding      vector(1024),                   -- bge-m3
    processed_at   TIMESTAMPTZ DEFAULT now(),
    reviewed_by    TEXT,                           -- 사람 검수 (human-in-the-loop)
    reviewed_ok    BOOLEAN
);
CREATE INDEX IF NOT EXISTS ix_ai_category ON mail.ai_result (category);
