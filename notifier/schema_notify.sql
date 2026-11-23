-- =====================================================================
-- 지텍 AI 메일 비서 - 텔레그램 알림 (여러 번 실행해도 안전)
-- =====================================================================
CREATE TABLE IF NOT EXISTS mail.notify_setting (
    account        TEXT PRIMARY KEY,
    tg_chat_id     BIGINT,                         -- 텔레그램 대화 ID (연결 시 자동 저장)
    tg_name        TEXT,
    tg_linked_at   TIMESTAMPTZ,
    link_code      TEXT,                           -- 연결용 1회 코드 (15분 유효)
    link_code_at   TIMESTAMPTZ,
    enabled        BOOLEAN  NOT NULL DEFAULT TRUE,
    content_level  TEXT     NOT NULL DEFAULT 'summary',  -- minimal / summary / full
    quiet_start    SMALLINT NOT NULL DEFAULT 22,   -- 조용한 시간 시작 (시)
    quiet_end      SMALLINT NOT NULL DEFAULT 7,    -- 조용한 시간 종료 (시)
    weekend_quiet  BOOLEAN  NOT NULL DEFAULT TRUE,
    remind_days    TEXT     NOT NULL DEFAULT '3,1' -- 마감 D-n 리마인드
);

CREATE TABLE IF NOT EXISTS mail.notify_rule (
    id               BIGSERIAL PRIMARY KEY,
    account          TEXT NOT NULL,
    name             TEXT NOT NULL,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    from_contains    TEXT,      -- 보낸사람 주소·이름 (쉼표 = 또는)
    subject_contains TEXT,      -- 제목 키워드 (쉼표 = 또는)
    category         TEXT,      -- 정부과제 / 사내업무 / 외부협력 ...
    subtype_contains TEXT,      -- 세부분류 (쉼표 = 또는)
    action_only      BOOLEAN NOT NULL DEFAULT FALSE,  -- AI '조치 필요'만
    deadline_days    INT,       -- 마감 N일 이내만
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rule_acct ON mail.notify_rule (account);

CREATE TABLE IF NOT EXISTS mail.notify_log (
    id          BIGSERIAL PRIMARY KEY,
    account     TEXT NOT NULL,
    message_id  BIGINT,
    kind        TEXT NOT NULL,          -- new / remind-3 / remind-1 / test / dup
    rule_name   TEXT,
    subject_key TEXT,                   -- 중복 공고 판정용 (Re/Fwd 제거 제목)
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN,
    error       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_notify_once ON mail.notify_log (account, message_id, kind) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_notify_subj ON mail.notify_log (account, subject_key, sent_at DESC);

CREATE TABLE IF NOT EXISTS mail.kv (key TEXT PRIMARY KEY, value TEXT);   -- 텔레그램 getUpdates offset 등

-- ---------------------------------------------------------------------
-- v2: 사내 알림 (웹 앱 알림센터 + PC 바탕화면 알림) - 기존 데이터는 '읽음/표시됨' 처리
-- ---------------------------------------------------------------------
ALTER TABLE mail.notify_setting ADD COLUMN IF NOT EXISTS created_at   TIMESTAMPTZ DEFAULT now();
ALTER TABLE mail.notify_setting ADD COLUMN IF NOT EXISTS pc_token_hash TEXT;      -- PC 알림 프로그램 인증 (SHA-256)
ALTER TABLE mail.notify_setting ADD COLUMN IF NOT EXISTS pc_seen_at   TIMESTAMPTZ; -- PC 프로그램 마지막 접속
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS title   TEXT;
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS body    TEXT;
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS tg      TEXT        DEFAULT 'sent';  -- pending/sent/none/skip/expired
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ DEFAULT now();   -- 웹 알림센터 읽음
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS pc_at   TIMESTAMPTZ DEFAULT now();   -- PC 바탕화면 알림 표시
ALTER TABLE mail.notify_log ALTER COLUMN tg DROP DEFAULT;
ALTER TABLE mail.notify_log ALTER COLUMN read_at DROP DEFAULT;
ALTER TABLE mail.notify_log ALTER COLUMN pc_at DROP DEFAULT;
CREATE INDEX IF NOT EXISTS ix_notify_unread ON mail.notify_log (account) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_notify_tg ON mail.notify_log (account) WHERE tg = 'pending';

-- ---------------------------------------------------------------------
-- v3: 알림센터에 텔레그램 전송 결과 표시 (전송 시각 · 실패 사유 · 재시도 횟수)
-- ---------------------------------------------------------------------
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS tg_at    TIMESTAMPTZ;        -- 텔레그램 전송 성공 시각
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS tg_error TEXT;               -- 마지막 실패 사유
ALTER TABLE mail.notify_log ADD COLUMN IF NOT EXISTS tg_try   INT NOT NULL DEFAULT 0;  -- 실패 횟수 (5회 → fail)
