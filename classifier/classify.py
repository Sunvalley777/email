# -*- coding: utf-8 -*-
"""
지텍 AI 메일 관리 시스템 P1 - 규칙 분류 + Ollama 요약·추출

  python3 classify.py --check        # Ollama·DB 확인, 설치된 모델 목록
  python3 classify.py --init-db      # P1 스키마 확장 (최초 1회)
  python3 classify.py --limit 20     # 미처리 메일 20건 처리 (시험)
  python3 classify.py                # 미처리 메일 전체 처리 (기본 최대 BATCH_LIMIT건)
  python3 classify.py --report       # 분류 현황 · 다가오는 마감 · 조치 필요
  python3 classify.py --reset        # AI 결과 전체 삭제 (규칙 변경 후 재분류용)

처리 흐름
  ① 도메인·헤더 규칙 → 사내업무 / 정부과제 / 뉴스레터 / 외부협력
  ② 뉴스레터는 LLM 생략 (제목만 저장, 다이제스트용)
  ③ 나머지는 Ollama(로컬)로 요약·세부분류·마감일·조치사항 추출 → 외부로 나가는 데이터 없음
"""
import argparse
import hashlib
import email
import json
import logging
import os
import re
import sys
import time
import urllib.request
from datetime import date
from email.parser import BytesHeaderParser
from pathlib import Path

import ocr as OCR

BASE = Path(__file__).resolve().parent


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BASE / ".env")             # classifier 전용 설정 (Ollama)
load_env(BASE.parent / ".env")      # 수집기 설정 (PG_DSN, IMAP_USER)

PG_DSN = os.getenv("PG_DSN", "")
ACCOUNT = os.getenv("IMAP_USER", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")          # 비우면 임베딩 생략
NUM_CTX = int(os.getenv("NUM_CTX", "8192"))
MAX_BODY = int(os.getenv("MAX_BODY_CHARS", "3500"))
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "50"))
RULES = json.loads((BASE / "rules.json").read_text(encoding="utf-8"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("classify")


# ---------------------------------------------------------------- DB
def db():
    import psycopg
    if not PG_DSN:
        sys.exit("[설정 오류] PG_DSN이 없습니다 (~/mail-collector/.env 확인)")
    return psycopg.connect(PG_DSN)


# ---------------------------------------------------------------- Ollama
def ollama(path, payload=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def llm_json(system, user):
    payload = {
        "model": LLM_MODEL, "stream": False, "format": "json",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    last = None
    for _ in range(2):
        out = ollama("/api/chat", payload)["message"]["content"]
        out = re.sub(r"(?s)<think>.*?</think>", "", out).strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            m = re.search(r"\{.*\}", out, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
            last = e
    raise ValueError(f"LLM JSON 파싱 실패: {last}")


def embed(text):
    if not EMBED_MODEL:
        return None
    r = ollama("/api/embed", {"model": EMBED_MODEL, "input": text[:2000]})
    v = (r.get("embeddings") or [None])[0]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]" if v and len(v) == 1024 else None


# ---------------------------------------------------------------- 규칙 분류
def domain_of(addr):
    return (addr or "").split("@")[-1].lower()


def is_bulk(raw_path):
    """원본 헤더로 대량 발송 메일 판별 (List-Unsubscribe, Precedence: bulk)"""
    try:
        with open(raw_path, "rb") as f:
            h = BytesHeaderParser().parse(f)
        if h.get("List-Unsubscribe") or h.get("List-Id"):
            return "header:list-unsubscribe"
        if (h.get("Precedence") or "").lower() in ("bulk", "list", "junk"):
            return "header:precedence-bulk"
    except Exception:
        pass
    return None


def rule_classify(from_addr, raw_path, subject=""):
    d = domain_of(from_addr)
    fa = (from_addr or "").lower()
    subj = subject or ""
    sl = subj.lower()
    if any(p.lower() in sl for p in RULES.get("system_subject_patterns", [])):
        return "시스템알림", "subject:system"
    for pat, cat in CTX["extra"].items():            # 관리자가 웹에서 추가한 공통 규칙
        if d == pat or d.endswith("." + pat):
            return cat, f"extra:{pat}"
    if d in RULES["internal_domains"]:
        return "사내업무", f"domain:{d}"
    if any(p.lower() in sl for p in RULES.get("promo_subject_patterns", [])):
        return "뉴스레터", "subject:promo"
    if d in RULES["gov_domains"] or any(d.endswith(s) for s in RULES["gov_suffixes"]):
        return "정부과제", f"domain:{d}"
    if d in RULES["newsletter_domains"]:
        return "뉴스레터", f"domain:{d}"
    bulk = is_bulk(raw_path) if raw_path else None
    if bulk:
        return "뉴스레터", bulk
    if any(k in fa for k in RULES["newsletter_from_keywords"]):
        return "뉴스레터", "from-keyword"
    if any(d.endswith(s) for s in RULES["university_suffixes"]):
        return "외부협력", f"univ:{d}"
    return "외부협력", "default"


SEC_LEVEL = {"사내업무": "L2", "정부과제": "L3", "뉴스레터": "L3", "외부협력": "L2", "시스템알림": "L2"}

# ---------------------------------------------------------------- P3: 공통 규칙 추가분 · 개인 규칙 · 개인 세부분류
CTX = {"extra": {}, "user_rules": {}, "user_subtypes": {}, "version": None}


def ensure_p3(conn):
    """P3 스키마(웹 앱과 공용) 적용 - 여러 번 실행해도 안전"""
    f = BASE.parent / "webapp" / "schema_p3.sql"
    if f.exists():
        conn.execute(f.read_text(encoding="utf-8"))
        conn.commit()


def load_ctx(conn):
    """DB 규칙을 읽고 규칙 버전(rules.json + 지시문 + 웹 추가 규칙)을 계산"""
    extra = {r[0]: r[1] for r in conn.execute(
        "SELECT lower(pattern), category FROM mail.rule_extra WHERE kind='domain' ORDER BY id").fetchall()}
    ur, us = {}, {}
    for acct, rid, mt, pat, cat, sub in conn.execute(
            "SELECT account, id, match_type, lower(pattern), category, subtype FROM mail.user_rule ORDER BY id"):
        ur.setdefault(acct, []).append((rid, mt, pat, cat, sub))
    for acct, cat, name, kw in conn.execute(
            "SELECT account, category, name, keywords FROM mail.user_subtype ORDER BY id"):
        us.setdefault(acct, []).append((cat, name, [k.strip().lower() for k in kw.split(",") if k.strip()]))
    src = (BASE / "rules.json").read_text(encoding="utf-8") + SYSTEM + json.dumps(PROMPTS, ensure_ascii=False) \
        + json.dumps(sorted(extra.items()), ensure_ascii=False)
    CTX.update(extra=extra, user_rules=ur, user_subtypes=us, version=hashlib.sha1(src.encode()).hexdigest()[:10])
    return CTX


def user_rule_hit(account, from_addr, subject):
    fa, d, sl = (from_addr or "").lower(), domain_of(from_addr), (subject or "").lower()
    for rid, mt, pat, cat, sub in CTX["user_rules"].get(account, []):
        if (mt == "from_addr" and fa == pat) or (mt == "from_domain" and (d == pat or d.endswith("." + pat))) \
                or (mt == "subject" and pat and pat in sl):
            return rid, cat, sub
    return None


def user_subtype_hit(account, category, subject, body):
    text = f"{subject or ''}\n{body or ''}".lower()
    for cat, name, kws in CTX["user_subtypes"].get(account, []):
        if cat == category and any(k in text for k in kws):
            return name
    return None
NO_ACTION_SUBTYPES = {"결재·시스템알림", "광고·홍보", "홍보·소식"}


# ---------------------------------------------------------------- 프롬프트
SYSTEM = ("너는 검사장비 제조사 지텍(주) 직원(메일 수신자)의 메일 비서다. 메일을 읽고 요청된 JSON만 출력한다. "
          "모르는 값은 null. 날짜는 반드시 YYYY-MM-DD (연도가 없으면 보낸날짜 기준 연도). 요약은 한국어 2문장 이내.\n"
          "[action_needed 판정 기준 - 엄격히 적용]\n"
          "true: 수신자에게 회신·제출·신청·참석 회신·결재·검토를 명시적으로 요청하거나, 수신자가 직접 처리해야 하는 기한이 있는 경우\n"
          "false: 단순 안내·홍보·소식·행사 소개·자료 공유·자동 알림·구독 안내. 애매하면 false.\n"
          "action에는 '확인하고 필요한 조치' 같은 일반 문구 금지. 구체적인 행동(무엇을, 언제까지)만 쓰고 없으면 null.\n"
          "[날짜] '마감', '까지', '접수기간', '신청기간', '제출기한'의 끝 날짜는 deadline. 행사가 열리는 날짜는 event_date.")

PROMPTS = {
    "정부과제": """정부·공공기관 메일이다. 아래 JSON으로 답하라.
{"subtype":"공고|수요조사|사업관리|평가·심사|행사·교육|홍보·소식|기타", "agency":"기관명", "title":"공고·안내 제목",
 "deadline":"접수·제출 마감일 또는 null", "event_date":"행사일 또는 null", "budget":"지원규모 또는 null",
 "summary":"요약", "action_needed":true/false, "action":"구체적 할 일 또는 null"}""",
    "사내업무": """사내 메일이다. 아래 JSON으로 답하라.
{"subtype":"보고|요청|공지|회의·일정|결재·시스템알림|기타", "dept":"발신 부서 또는 null",
 "deadline":"요청 기한 또는 null", "event_date":"회의·행사일 또는 null", "summary":"요약",
 "action_needed":true/false, "action":"구체적 할 일 또는 null"}""",
    "외부협력": """외부 기관·업체 메일이다. 아래 JSON으로 답하라.
{"subtype":"영업·견적|기술문의|협력제안|회의·일정|행사·세미나|광고·홍보|기타", "org":"상대 기관·회사명",
 "deadline":"회신·참가 기한 또는 null", "event_date":"회의·행사일 또는 null", "summary":"요약",
 "action_needed":true/false, "action":"구체적 할 일 또는 null"}""",
}


QUOTE_MARKERS = re.compile(r"(?m)^(-{2,}\s*(Original Message|원본 메시지|Forwarded message|전달된 메시지).*|"
                           r"On .+ wrote:|.+님이 작성:)\s*$")


def clean_body(t):
    t = t or ""
    m = QUOTE_MARKERS.search(t)
    head = t[:m.start()] if m and m.start() > 200 else t       # 회신 인용부 제거 (본문이 짧으면 유지)
    head = "\n".join(l for l in head.splitlines() if not l.lstrip().startswith(">"))
    head = re.sub(r"\n\s*\n+", "\n\n", head)
    return head.strip()[:MAX_BODY]


def norm_date(s, ref_year):
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})[-./월]\s*(\d{1,2})", s)
        if m:
            return date(ref_year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------- 처리
def pending(conn, limit):
    return conn.execute("""
        SELECT COLS FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id = m.id
        WHERE r.message_id IS NULL AND m.direction = 'in'
        ORDER BY m.sent_at DESC NULLS LAST LIMIT %s""".replace("COLS", ROW_COLS), (limit,)).fetchall()


ROW_COLS = """m.id, m.from_addr, m.from_name, m.subject, m.body_text, m.sent_at, m.raw_path,
               coalesce((SELECT array_agg(a.filename) FROM mail.attachment a WHERE a.message_id=m.id), '{}'),
               coalesce((SELECT bool_or(a.is_drm) FROM mail.attachment a WHERE a.message_id=m.id), false),
               coalesce(m.has_html, false),
               (SELECT count(*) FROM mail.attachment a WHERE a.message_id=m.id
                  AND (a.content_type LIKE 'image/%%' OR a.filename ~* '\\.(jpe?g|png|gif|bmp|tiff?|webp)$')),
               m.account"""


def mail_ocr(conn, mid, raw_path, force=False):
    """이미지 속 글자 (저장된 결과가 있으면 재사용)"""
    if conn.execute("SELECT to_regclass('mail.ocr')").fetchone()[0] is None:
        ensure_p3(conn)                            # OCR 표가 없으면 자동 생성
    if not force:
        r = conn.execute("SELECT text FROM mail.ocr WHERE message_id=%s", (mid,)).fetchone()
        if r:
            return r[0] or ""

    def blob(sha):
        r = conn.execute("SELECT stored_path FROM mail.attachment WHERE message_id=%s AND sha256=%s LIMIT 1",
                         (mid, sha)).fetchone()
        return r[0] if r else None

    t0 = time.time()
    try:
        text, n, err = OCR.ocr_mail(raw_path, blob)
    except Exception as e:
        text, n, err = "", 0, str(e)[:300]
    conn.execute("""INSERT INTO mail.ocr(message_id, text, n_images, error) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (message_id) DO UPDATE SET text=EXCLUDED.text, n_images=EXCLUDED.n_images,
                    error=EXCLUDED.error, created_at=now()""", (mid, text, n, err))
    log.info("  OCR id=%s 이미지 %d장 · 글자 %d자 · %.1fs%s", mid, n, len(text), time.time() - t0,
             f" · 오류: {err}" if err else "")
    return text


def upsert(conn, mid, **f):
    conn.execute("""
        INSERT INTO mail.ai_result(message_id, sec_level, category, subtype, customer, summary, extracted,
            action_needed, action, deadline, rule_hit, model, embedding, error, processed_at, rule_version, manual)
        VALUES (%(mid)s,%(sec)s,%(cat)s,%(sub)s,%(cust)s,%(summ)s,%(ext)s,%(act)s,%(action)s,%(dl)s,
                %(rule)s,%(model)s,%(emb)s::vector,%(err)s,now(),%(ver)s,false)
        ON CONFLICT (message_id) DO UPDATE SET
            sec_level=EXCLUDED.sec_level, category=EXCLUDED.category, subtype=EXCLUDED.subtype,
            customer=EXCLUDED.customer, summary=EXCLUDED.summary, extracted=EXCLUDED.extracted,
            action_needed=EXCLUDED.action_needed, action=EXCLUDED.action, deadline=EXCLUDED.deadline,
            rule_hit=EXCLUDED.rule_hit, model=EXCLUDED.model, embedding=EXCLUDED.embedding,
            error=EXCLUDED.error, processed_at=now(), rule_version=EXCLUDED.rule_version,
            manual=false, manual_at=NULL""", dict(mid=mid, ver=CTX["version"], **f))


class KeepOld(Exception):
    """재분류 중 AI 실패 → 기존 결과를 그대로 둠"""


def process(conn, row, keep_on_error=False):
    mid, from_addr, from_name, subject, body, sent_at, raw_path, atts, has_drm, has_html, n_img, account = row
    forced_sub = None
    hit = user_rule_hit(account, from_addr, subject)
    if hit:                                            # 개인 규칙이 가장 우선
        rid, cat, forced_sub = hit
        rule = f"user:{rid}"
        conn.execute("UPDATE mail.user_rule SET hits=hits+1 WHERE id=%s", (rid,))
    else:
        cat, rule = rule_classify(from_addr, raw_path, subject)
    ocr_text = ""
    if cat not in ("뉴스레터", "시스템알림") and OCR.need_ocr(body, has_html, n_img):
        ocr_text = mail_ocr(conn, mid, raw_path)
    sec = "L1" if has_drm else SEC_LEVEL[cat]
    base = dict(sec=sec, cat=cat, rule=rule, sub=None, cust=None, summ=subject, ext=None,
                act=False, action=None, dl=None, model=None, emb=None, err=None)

    if cat in ("뉴스레터", "시스템알림"):        # LLM 생략
        base.update(cust=from_name or domain_of(from_addr),
                    sub=forced_sub or user_subtype_hit(account, cat, subject, body) or cat)
        upsert(conn, mid, **base)
        return cat, 0.0

    sent_s = sent_at.strftime("%Y-%m-%d") if sent_at else "미상"
    user = (f"보낸사람: {from_name} <{from_addr}>\n보낸날짜: {sent_s}\n제목: {subject}\n"
            f"첨부: {', '.join(atts) if atts else '없음'}\n\n본문:\n{clean_body(body)}")
    if ocr_text:
        user += "\n\n[본문 이미지 속 글자 (OCR, 오탈자 있을 수 있음)]\n" + ocr_text[:MAX_BODY]
    t0 = time.time()
    try:
        j = llm_json(SYSTEM, PROMPTS[cat] + "\n\n---\n" + user)
        ref_year = sent_at.year if sent_at else date.today().year
        base.update(
            sub=j.get("subtype"),
            cust=j.get("agency") or j.get("org") or j.get("dept") or from_name,
            summ=j.get("summary") or subject,
            ext=json.dumps(j, ensure_ascii=False),
            act=bool(j.get("action_needed")),
            action=j.get("action"),
            dl=norm_date(j.get("deadline"), ref_year),
            model=LLM_MODEL,
        )
        mine = forced_sub or user_subtype_hit(account, cat, subject, (body or "") + "\n" + ocr_text)
        if mine:                                   # 개인 세부분류 (AI 세부분류는 extracted 에 남음)
            base["sub"] = mine
        if base["sub"] in NO_ACTION_SUBTYPES:      # 안내·홍보·알림은 조치 불필요로 보정
            base.update(act=False, action=None)
        if base["act"] and not base["action"]:
            base["act"] = False
        try:
            base["emb"] = embed(f"{subject}\n{base['summ']}")
        except Exception as e:
            log.warning("임베딩 실패 id=%s: %s", mid, e)
    except Exception as e:
        if keep_on_error:
            raise KeepOld(str(e)[:300])
        base["err"] = str(e)[:500]
        log.warning("LLM 실패 id=%s: %s", mid, e)
    upsert(conn, mid, **base)
    return cat, time.time() - t0


def run(limit):
    with db() as conn:
        ensure_p3(conn)
        load_ctx(conn)
        rows = pending(conn, limit)
        log.info("미처리 메일 %d건 처리 시작 (모델 %s)", len(rows), LLM_MODEL)
        stat = {}
        for i, row in enumerate(rows, 1):
            cat, sec = process(conn, row)
            conn.commit()
            stat[cat] = stat.get(cat, 0) + 1
            log.info("[%d/%d] %-4s %4.1fs  %s", i, len(rows), cat, sec, (row[3] or "")[:50])
        log.info("완료: %s", stat)


def check():
    tags = ollama("/api/tags", timeout=10)
    names = [m["name"] for m in tags.get("models", [])]
    log.info("Ollama OK  %s  모델: %s", OLLAMA_URL, ", ".join(names) or "(없음)")
    for need in filter(None, [LLM_MODEL, EMBED_MODEL]):
        ok = need in names or f"{need}:latest" in names
        log.info("  %-14s %s", need, "설치됨" if ok else f"없음 → ollama pull {need}")
    with db() as conn:
        n = conn.execute("SELECT count(*) FROM mail.message WHERE direction='in'").fetchone()[0]
        d = conn.execute("SELECT count(*) FROM mail.ai_result").fetchone()[0]
        log.info("DB OK  받은 메일 %d건 / AI 처리 %d건", n, d)


def init_db():
    with db() as conn:
        conn.execute((BASE / "schema_p1.sql").read_text(encoding="utf-8"))
    log.info("P1 스키마 확장 완료")


def report():
    with db() as conn:
        print("\n■ 분류 현황")
        for r in conn.execute("""SELECT category, coalesce(subtype,'-'), count(*),
                                 count(*) FILTER (WHERE action_needed), count(*) FILTER (WHERE error IS NOT NULL)
                                 FROM mail.ai_result GROUP BY 1,2 ORDER BY 1, 3 DESC"""):
            print(f"  {r[0]:6s} {r[1]:12s} {r[2]:4d}건  조치필요 {r[3]:3d}  오류 {r[4]}")
        print("\n■ 다가오는 마감")
        for r in conn.execute("""SELECT deadline, category, customer, left(subject,45), n FROM (
                 SELECT DISTINCT ON (k) *, count(*) OVER (PARTITION BY k) n FROM (
                   SELECT *, regexp_replace(subject, '^((re|fw|fwd)\\s*:\\s*)+', '', 'i') k FROM mail.v_deadlines) a
                 ORDER BY k, sent_at DESC) b ORDER BY deadline LIMIT 15"""):
            dup = f" (동일 {r[4]}건)" if r[4] > 1 else ""
            print(f"  {r[0]}  {r[1]:6s} {str(r[2] or '')[:14]:14s} {r[3]}{dup}")
        print("\n■ 다가오는 행사·회의")
        for r in conn.execute("""SELECT (r.extracted->>'event_date')::date d, r.category, left(m.subject,50)
                                 FROM mail.ai_result r JOIN mail.message m ON m.id=r.message_id
                                 WHERE r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                                   AND (r.extracted->>'event_date')::date >= CURRENT_DATE
                                 ORDER BY 1 LIMIT 15"""):
            print(f"  {r[0]}  {r[1]:6s} {r[2]}")
        print("\n■ 최근 조치 필요 (10건)")
        for r in conn.execute("""SELECT sent_at::date, category, left(subject,35), left(action,40), n FROM (
                 SELECT DISTINCT ON (k) *, count(*) OVER (PARTITION BY k) n FROM (
                   SELECT *, regexp_replace(subject, '^((re|fw|fwd)\\s*:\\s*)+', '', 'i') k FROM mail.v_actions) a
                 ORDER BY k, sent_at DESC) b ORDER BY sent_at DESC LIMIT 10"""):
            dup = f" (동일 {r[4]}건)" if r[4] > 1 else ""
            print(f"  {r[0]}  {r[1]:6s} {r[2]}{dup}  → {r[3]}")


def ocr_backfill(limit):
    """이미 분류된 이미지 메일(본문 글자 적음)을 OCR 후 다시 분류"""
    if not OCR.available():
        sys.exit("tesseract 미설치 → sudo apt install -y tesseract-ocr tesseract-ocr-kor")
    with db() as conn:
        ensure_p3(conn)
        load_ctx(conn)
        rows = conn.execute(("SELECT " + ROW_COLS + """ FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                 WHERE m.direction='in' AND r.category NOT IN ('뉴스레터','시스템알림')
                   AND NOT EXISTS (SELECT 1 FROM mail.ocr o WHERE o.message_id=m.id)
                 ORDER BY m.sent_at DESC NULLS LAST""")).fetchall()
        rows = [r for r in rows if OCR.need_ocr(r[4], r[9], r[10])][:limit]
        log.info("OCR 대상(이미지 메일) %d건 → 다시 분류", len(rows))
        for i, row in enumerate(rows, 1):
            cat, sec = process(conn, row)
            conn.commit()
            log.info("[%d/%d] %-4s %4.1fs  %s", i, len(rows), cat, sec, (row[3] or "")[:50])


# ---------------------------------------------------------------- 재분류 (기존 결과를 지우지 않고 덮어쓰기)
def job_where(f):
    """작업 조건 → SQL. 사람이 고친 분류(manual)는 ids 지정(본인 요청) 때만 다시 분석"""
    w, a = ["m.direction='in'"], []
    if f.get("ids"):
        w.append("m.id = ANY(%s)")
        a.append([int(i) for i in f["ids"]])
    else:
        w.append("coalesce(r.manual, false) = false")
    if f.get("account"):
        w.append("m.account = %s")
        a.append(f["account"])
    if f.get("category"):
        w.append("r.category = %s")
        a.append(f["category"])
    if f.get("since"):
        w.append("m.sent_at >= %s::date")
        a.append(f["since"])
    if f.get("until"):
        w.append("m.sent_at < %s::date + 1")
        a.append(f["until"])
    if f.get("stale"):
        w.append("r.rule_version IS DISTINCT FROM %s")
        a.append(CTX["version"])
    if f.get("user_rule"):                           # 개인 규칙에 맞는 메일
        mt, pat = f["user_rule"]["match_type"], f["user_rule"]["pattern"].lower()
        if mt == "from_addr":
            w.append("lower(m.from_addr) = %s")
            a.append(pat)
        elif mt == "from_domain":
            w.append("(split_part(lower(m.from_addr),'@',2) = %s OR split_part(lower(m.from_addr),'@',2) LIKE %s)")
            a += [pat, "%." + pat]
        else:
            w.append("lower(m.subject) LIKE %s")
            a.append("%" + pat + "%")
    if f.get("rule_like"):
        w.append("r.rule_hit LIKE %s")
        a.append(f["rule_like"])
    return " AND ".join(w), a


def job_rows(conn, f):
    where, args = job_where(f)
    return conn.execute(("SELECT " + ROW_COLS + ", r.category FROM mail.message m "
                         "JOIN mail.ai_result r ON r.message_id = m.id WHERE " + where +
                         " ORDER BY m.sent_at DESC NULLS LAST"), args).fetchall()


def run_job(job_id):
    with db() as conn:
        ensure_p3(conn)
        load_ctx(conn)
        j = conn.execute("SELECT filter, status FROM mail.reclass_job WHERE id=%s", (job_id,)).fetchone()
        if not j or j[1] not in ("queued", "running"):
            log.info("작업 %s 없음 또는 이미 끝남", job_id)
            return
        f = j[0] if isinstance(j[0], dict) else json.loads(j[0])
        rows = job_rows(conn, f)
        conn.execute("""UPDATE mail.reclass_job SET status='running', total=%s, done=0, changed=0, started_at=now()
                        WHERE id=%s""", (len(rows), job_id))
        conn.commit()
        log.info("재분류 작업 #%s 시작: %d건 (규칙 버전 %s)", job_id, len(rows), CTX["version"])
        changed, failed, streak, last_err = 0, 0, 0, None
        try:
            for i, row in enumerate(rows, 1):
                if i % 5 == 1 and conn.execute("SELECT status FROM mail.reclass_job WHERE id=%s",
                                               (job_id,)).fetchone()[0] == "stop":
                    conn.execute("UPDATE mail.reclass_job SET status='stopped', finished_at=now() WHERE id=%s",
                                 (job_id,))
                    conn.commit()
                    log.info("작업 #%s 중지됨 (%d/%d)", job_id, i - 1, len(rows))
                    return
                old = row[-1]
                try:
                    cat, sec = process(conn, row[:-1], keep_on_error=True)
                    streak = 0
                except KeepOld as e:                   # AI 실패 → 기존 결과 유지
                    conn.rollback()
                    failed, streak, last_err = failed + 1, streak + 1, str(e)
                    cat, sec = old, 0.0
                    log.warning("AI 실패 → 기존 결과 유지 id=%s: %s", row[0], e)
                    if streak >= 3:
                        raise RuntimeError(f"AI 서버 응답 없음 (연속 3건 실패): {e}")
                changed += cat != old
                conn.execute("UPDATE mail.reclass_job SET done=%s, changed=%s WHERE id=%s", (i, changed, job_id))
                conn.commit()
                log.info("[#%s %d/%d] %s→%s %4.1fs  %s", job_id, i, len(rows), old, cat, sec, (row[3] or "")[:40])
            conn.execute("UPDATE mail.reclass_job SET status='done', finished_at=now(), error=%s WHERE id=%s",
                         (f"AI 실패 {failed}건 (기존 결과 유지): {last_err}"[:500] if failed else None, job_id))
        except Exception as e:
            conn.rollback()
            conn.execute("UPDATE mail.reclass_job SET status='error', error=%s, finished_at=now() WHERE id=%s",
                         (str(e)[:500], job_id))
            log.exception("작업 #%s 오류", job_id)
        conn.commit()
        log.info("작업 #%s 완료: 바뀐 분류 %d건", job_id, changed)


def create_job(by, f, scope="admin"):
    with db() as conn:
        ensure_p3(conn)
        r = conn.execute("INSERT INTO mail.reclass_job(requested_by, scope, filter) VALUES (%s,%s,%s) RETURNING id",
                         (by, scope, json.dumps(f, ensure_ascii=False))).fetchone()
    return r[0]


def reset():
    with db() as conn:
        n = conn.execute("DELETE FROM mail.ai_result").rowcount
    log.info("AI 결과 %d건 삭제 (원본 메일은 그대로)", n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="지텍 메일 분류기 P1")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ocr-backfill", action="store_true", help="기존 이미지 메일 OCR 후 다시 분류")
    ap.add_argument("--ids", help="이 메일들만 다시 분석 (쉼표 구분, 수동 확정도 다시 분석)")
    ap.add_argument("--job", type=int, help="웹에서 만든 재분류 작업 실행")
    ap.add_argument("--reclassify", action="store_true", help="조건에 맞는 메일 덮어쓰기 재분류 (수동 확정 제외)")
    ap.add_argument("--category")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--account")
    ap.add_argument("--stale", action="store_true", help="규칙이 바뀐 뒤 아직 반영 안 된 메일만")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.init_db:
        init_db()
    elif a.report:
        report()
    elif a.reset:
        reset()
    elif a.ocr_backfill:
        ocr_backfill(a.limit or 200)
    elif a.job:
        run_job(a.job)
    elif a.ids:
        run_job(create_job("cli", {"ids": [int(x) for x in a.ids.split(",") if x.strip()]}, "ids"))
    elif a.reclassify:
        f = {k: v for k, v in dict(category=a.category, since=a.since, until=a.until, account=a.account,
                                   stale=a.stale).items() if v}
        run_job(create_job("cli", f))
    else:
        run(a.limit or BATCH_LIMIT)
