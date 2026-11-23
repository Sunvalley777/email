-- =====================================================================
-- 지텍 AI 메일 관리 시스템 P2 - 다중 사용자 (여러 번 실행해도 안전)
-- =====================================================================
CREATE TABLE IF NOT EXISTS mail.account (
    account          TEXT PRIMARY KEY,               -- 메일 주소 (그룹웨어 ID)
    display_name     TEXT,
    enc_pass         TEXT,                           -- Fernet 암호문 (평문 저장 안 함)
    enabled          BOOLEAN NOT NULL DEFAULT FALSE, -- 수집 대상 여부
    consent_at       TIMESTAMPTZ,                    -- 수집 동의 일시 (철회 시 NULL)
    consent_version  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at    TIMESTAMPTZ,
    last_sync_at     TIMESTAMPTZ,
    last_error       TEXT
);

-- 감사 로그 (로그인·동의·철회·삭제 이력)
CREATE TABLE IF NOT EXISTS mail.audit_log (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    account  TEXT,
    action   TEXT NOT NULL,
    detail   TEXT,
    ip       TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_acct ON mail.audit_log (account, at DESC);
