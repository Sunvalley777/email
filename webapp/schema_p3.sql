-- =====================================================================
-- 지텍 AI 메일 비서 P3 - 재분류 · 분류 이름 편집 · 개인 규칙 (여러 번 실행해도 안전)
--   웹 앱 시작 시 자동 적용 (classify.py 도 시작 시 적용)
-- =====================================================================

-- 1단계: 수동 확정 · 규칙 버전 · 일괄 재분류 작업
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS manual       BOOLEAN NOT NULL DEFAULT FALSE; -- 사람이 고친 분류 (일괄 재분류에서 보호)
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS manual_at    TIMESTAMPTZ;
ALTER TABLE mail.ai_result ADD COLUMN IF NOT EXISTS rule_version TEXT;                           -- 분류에 쓴 규칙·지시문 버전

CREATE TABLE IF NOT EXISTS mail.reclass_job (
    id          BIGSERIAL PRIMARY KEY,
    requested_by TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'admin',      -- admin / user_rule
    filter      JSONB NOT NULL,                      -- {account, category, since, until, stale, ids}
    total       INT NOT NULL DEFAULT 0,
    done        INT NOT NULL DEFAULT 0,
    changed     INT NOT NULL DEFAULT 0,              -- 분류가 바뀐 건수
    status      TEXT NOT NULL DEFAULT 'queued',      -- queued / running / done / stopped / error
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

-- 2단계: 분류 이름·색·순서 (내부 코드는 고정, 화면 이름만 변경)
CREATE TABLE IF NOT EXISTS mail.category (
    code   TEXT PRIMARY KEY,                         -- 내부 코드 (DB·규칙·알림에서 쓰는 값, 변경 불가)
    label  TEXT NOT NULL,                            -- 회사 기본 이름 (관리자 변경)
    color  TEXT NOT NULL,
    sort   INT  NOT NULL
);
INSERT INTO mail.category(code, label, color, sort) VALUES
    ('정부과제', '정부과제', '#2563eb', 1), ('사내업무', '사내업무', '#0f766e', 2),
    ('외부협력', '외부협력', '#9333ea', 3), ('뉴스레터', '뉴스레터', '#94a3b8', 4),
    ('시스템알림', '시스템알림', '#64748b', 5)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS mail.category_user (     -- 사용자별 이름·색·순서·숨김
    account TEXT NOT NULL,
    code    TEXT NOT NULL REFERENCES mail.category(code),
    label   TEXT,
    color   TEXT,
    sort    INT,
    hidden  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (account, code)
);

-- 3단계: 개인 규칙 · 개인 세부분류 · 수정 기록 · 공통 규칙 추가분
CREATE TABLE IF NOT EXISTS mail.user_rule (
    id         BIGSERIAL PRIMARY KEY,
    account    TEXT NOT NULL,
    match_type TEXT NOT NULL,                        -- from_addr / from_domain / subject
    pattern    TEXT NOT NULL,
    category   TEXT NOT NULL REFERENCES mail.category(code),
    subtype    TEXT,
    hits       INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account, match_type, pattern)
);

CREATE TABLE IF NOT EXISTS mail.user_subtype (       -- 개인 세부분류 (키워드가 제목·본문에 있으면 지정)
    id         BIGSERIAL PRIMARY KEY,
    account    TEXT NOT NULL,
    category   TEXT NOT NULL REFERENCES mail.category(code),
    name       TEXT NOT NULL,
    keywords   TEXT NOT NULL,                        -- 쉼표 = 또는
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account, category, name)
);

CREATE TABLE IF NOT EXISTS mail.correction (         -- 사용자가 분류를 고친 기록 → 공통 규칙 개선 제안
    id           BIGSERIAL PRIMARY KEY,
    account      TEXT NOT NULL,
    message_id   BIGINT REFERENCES mail.message(id) ON DELETE CASCADE,
    from_addr    TEXT,
    old_category TEXT,
    new_category TEXT,
    old_subtype  TEXT,
    new_subtype  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_corr_from ON mail.correction (from_addr);

CREATE TABLE IF NOT EXISTS mail.rule_extra (         -- 관리자가 웹에서 추가한 공통 규칙 (rules.json 에 더해서 적용)
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,                        -- domain
    pattern    TEXT NOT NULL,
    category   TEXT NOT NULL REFERENCES mail.category(code),
    added_by   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, pattern)
);

-- 지텍 브랜드 분류 색 (홈페이지 블루 계열) - 관리자가 바꾸지 않은 기본값만 갱신
UPDATE mail.category SET color='#1F4E9A' WHERE code='정부과제'   AND lower(color)='#2563eb';
UPDATE mail.category SET color='#0F8F84' WHERE code='사내업무'   AND lower(color)='#0f766e';
UPDATE mail.category SET color='#7B4BC4' WHERE code='외부협력'   AND lower(color)='#9333ea';
UPDATE mail.category SET color='#9AA1AB' WHERE code='뉴스레터'   AND lower(color)='#94a3b8';
UPDATE mail.category SET color='#6B7280' WHERE code='시스템알림' AND lower(color)='#64748b';

-- 이미지 메일 OCR 결과 (classify.py --init-db 를 실행하지 않은 서버도 자동 생성)
CREATE TABLE IF NOT EXISTS mail.ocr (
    message_id  BIGINT PRIMARY KEY REFERENCES mail.message(id) ON DELETE CASCADE,
    text        TEXT,
    n_images    INT,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
