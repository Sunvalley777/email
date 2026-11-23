# -*- coding: utf-8 -*-
"""
지텍 AI 메일 비서 - 파일럿 웹 앱 (A방식: 그룹웨어 계정 등록)

  로그인  : 그룹웨어(다우오피스) IMAP 접속으로 본인 확인 - 별도 회원가입 없음
  동의    : 최초 1회 수집 동의 → 비밀번호를 Fernet 암호화해 DB 저장
  대시보드: 본인 메일만 (마감·할 일·행사·분류별 목록·상세)
  설정    : 수집 중지(비밀번호 삭제) / 내 데이터 전체 삭제
  관리자  : 계정별 수집 현황 (메일 내용은 볼 수 없음)

실행: uvicorn app:app --host 0.0.0.0 --port 8443 --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
"""
import calendar
import imaplib
import os
import re
import secrets
import shutil
import ssl
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

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


load_env(BASE / ".env")
load_env(BASE.parent / ".env")
load_env(BASE.parent / "classifier" / ".env")   # LLM_MODEL · OLLAMA_URL (AI 요약 정리)

PG_DSN = os.environ["PG_DSN"]
FERNET = Fernet(os.environ["FERNET_KEY"].encode())
SESSION_SECRET = os.environ["SESSION_SECRET"]
IMAP_HOST = os.getenv("IMAP_HOST", "gw.z-tec.co.kr")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "z-tec.co.kr").lower()
ADMINS = {a.strip().lower() for a in os.getenv("ADMIN_ACCOUNTS", "").split(",") if a.strip()}
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE.parent / "data")))
CONSENT_VERSION = "2026-11-v1"
CATEGORIES = ["정부과제", "사내업무", "외부협력", "뉴스레터", "시스템알림"]

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="ztec_mail",
                   max_age=8 * 3600, same_site="lax", https_only=True)
tpl = Jinja2Templates(directory=str(BASE / "templates"))

# 동의 전 비밀번호 임시 보관 (서버 메모리, 10분) - 쿠키에는 넣지 않음
PENDING: dict[str, tuple[str, str, float]] = {}
FAILS: dict[str, list[float]] = {}


# ---------------------------------------------------------------- 공통
def db():
    conn = psycopg.connect(PG_DSN)
    conn.execute("SET TIME ZONE 'Asia/Seoul'")   # 화면 시각·오늘 날짜를 한국 시간 기준으로
    return conn


def audit(account, action, detail=None, ip=None):
    with db() as c:
        c.execute("INSERT INTO mail.audit_log(account, action, detail, ip) VALUES (%s,%s,%s,%s)",
                  (account, action, detail, ip))


def client_ip(req: Request):
    return req.headers.get("x-forwarded-for", req.client.host if req.client else "-")


def csrf_token(req: Request):
    if "csrf" not in req.session:
        req.session["csrf"] = secrets.token_urlsafe(24)
    return req.session["csrf"]


def check_csrf(req: Request, token: str):
    if not token or token != req.session.get("csrf"):
        raise HTTPException(400, "잘못된 요청입니다. 페이지를 새로고침한 뒤 다시 시도하세요.")


def current(req: Request):
    acct = req.session.get("acct")
    if not acct:
        return None
    with db() as c:
        r = c.execute("SELECT enabled, consent_at FROM mail.account WHERE account=%s", (acct,)).fetchone()
    if not r or not r[1]:
        return None
    return acct


ZTEC_LOGO = "data:image/png;base64,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"   # 지텍 컬러 로고 (홈페이지 logo2.png)


def sidebar_ctx(me):
    """공통 왼쪽 메뉴 건수 (기본 기간: 최근 1개월) — dashboard2 이외 화면용"""
    try:
        _, _, _, pw, pa = _period_where("", "", "")
        with db() as c:
            acct = c.execute("SELECT last_sync_at, last_error, display_name FROM mail.account WHERE account=%s",
                             (me,)).fetchone()
            stats = dict(c.execute(f"""SELECT coalesce(r.category,'미분류'), count(*) FROM mail.message m
                         LEFT JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.account=%s AND m.direction='in' {pw} GROUP BY 1""", [me] + pa).fetchall())
            n_action = c.execute(f"""SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.account=%s AND m.direction='in' AND r.action_needed {pw}""", [me] + pa).fetchone()[0]
            n_dl = c.execute("""SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.account=%s AND r.deadline >= CURRENT_DATE""", (me,)).fetchone()[0]
            n_ev = c.execute("""SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.account=%s AND r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                           AND (r.extracted->>'event_date')::date >= CURRENT_DATE""", (me,)).fetchone()[0]
            subs = c.execute(f"""SELECT u.name, u.category, count(r.message_id) FROM mail.user_subtype u
                         LEFT JOIN (mail.ai_result r JOIN mail.message m ON m.id=r.message_id AND m.account=%s {pw})
                           ON r.subtype=u.name AND r.category=u.category
                         WHERE u.account=%s GROUP BY 1, 2 ORDER BY 2, 1""", [me] + pa + [me]).fetchall()
            alerts = c.execute("SELECT count(*) FROM mail.notify_log WHERE account=%s AND read_at IS NULL "
                               "AND title IS NOT NULL", (me,)).fetchone()[0]
            ns = c.execute("""SELECT s.tg_chat_id IS NOT NULL, coalesce(s.enabled, true), s.pc_token_hash IS NOT NULL,
                              (SELECT count(*) FILTER (WHERE enabled) FROM mail.notify_rule WHERE account=%s),
                              (SELECT count(*) FROM mail.notify_rule WHERE account=%s)
                              FROM (SELECT 1) x LEFT JOIN mail.notify_setting s ON s.account=%s""", (me, me, me)).fetchone()
            col = c.execute("SELECT enabled, last_error FROM mail.account WHERE account=%s", (me,)).fetchone()
            n_urule = c.execute("SELECT count(*) FROM mail.user_rule WHERE account=%s", (me,)).fetchone()[0]
        return dict(view="", cat="", sub="", total=sum(stats.values()), n_action=n_action, n_deadline=n_dl,
                    n_event=n_ev, alerts=alerts, stats=stats, subs=subs, acct=acct,
                    tg_linked=ns[0], notify_on=ns[1], pc_on=ns[2], rules_on=ns[3], rules_all=ns[4],
                    collect_on=bool(col and col[0]), collect_err=bool(col and col[1]), n_urule=n_urule)
    except Exception as e:
        print("sidebar_ctx 실패:", e)
        return None


def render(req, name, **ctx):
    me = req.session.get("acct")
    if me and "sb" not in ctx and not name.startswith("_") and name not in ("login.html", "consent.html"):
        ctx["sb"] = sidebar_ctx(me)
    cats = cat_info(me)
    lab = {c["code"]: c["label"] for c in cats}
    ctx.setdefault("cats", cats)
    ctx.update(request=req, me=me, csrf=csrf_token(req), logo=ZTEC_LOGO,
               is_admin=me in ADMINS, today=date.today(),
               cl=lambda code: lab.get(code, code or "미분류"))
    return tpl.TemplateResponse(req, name, ctx)


def imap_verify(user, pw):
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=20)
    try:
        if pw.isascii():
            m.login(user, pw)
        else:
            m.authenticate("PLAIN", lambda _: f"\0{user}\0{pw}".encode("utf-8"))
        return True
    except imaplib.IMAP4.error:
        return False
    finally:
        try:
            m.logout()
        except Exception:
            pass


def too_many_fails(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    return len(FAILS[ip]) >= 5


# ---------------------------------------------------------------- 로그인·동의
@app.get("/")
def root(req: Request):
    return RedirectResponse("/dashboard2" if current(req) else "/login", 303)


@app.get("/login")
def login_page(req: Request, msg: str = ""):
    return render(req, "login.html", msg=msg)


@app.post("/login")
def login(req: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    check_csrf(req, csrf)
    ip = client_ip(req)
    email = email.strip().lower()
    if "@" not in email:
        email = f"{email}@{ALLOWED_DOMAIN}"
    if not email.endswith("@" + ALLOWED_DOMAIN):
        return render(req, "login.html", msg=f"@{ALLOWED_DOMAIN} 계정만 사용할 수 있습니다.")
    if too_many_fails(ip):
        return render(req, "login.html", msg="로그인 실패가 많습니다. 10분 뒤 다시 시도하세요.")
    try:
        ok = imap_verify(email, password)
    except Exception:
        return render(req, "login.html", msg="메일 서버에 연결할 수 없습니다. 잠시 뒤 다시 시도하세요.")
    if not ok:
        FAILS.setdefault(ip, []).append(time.time())
        audit(email, "login_fail", ip=ip)
        return render(req, "login.html", msg="계정 또는 비밀번호가 올바르지 않습니다. (그룹웨어 비밀번호)")

    req.session.clear()
    req.session["acct"] = email
    enc = FERNET.encrypt(password.encode("utf-8")).decode()
    with db() as c:
        r = c.execute("SELECT consent_at FROM mail.account WHERE account=%s", (email,)).fetchone()
        if r and r[0]:      # 이미 동의한 사용자 → 비밀번호 갱신 + 수집 재개
            c.execute("UPDATE mail.account SET enc_pass=%s, enabled=true, last_login_at=now(), last_error=NULL "
                      "WHERE account=%s", (enc, email))
            audit(email, "login", ip=ip)
            return RedirectResponse("/dashboard2", 303)
    token = secrets.token_urlsafe(16)
    PENDING[token] = (email, enc, time.time())
    req.session["pending"] = token
    audit(email, "login_need_consent", ip=ip)
    return RedirectResponse("/consent", 303)


@app.get("/consent")
def consent_page(req: Request):
    tok = req.session.get("pending")
    if not tok or tok not in PENDING:
        return RedirectResponse("/login?msg=다시 로그인해 주세요.", 303)
    return render(req, "consent.html", version=CONSENT_VERSION)


@app.post("/consent")
def consent(req: Request, agree: str = Form(...), name: str = Form(""), csrf: str = Form("")):
    check_csrf(req, csrf)
    tok = req.session.pop("pending", None)
    item = PENDING.pop(tok, None) if tok else None
    if not item or time.time() - item[2] > 600:
        return RedirectResponse("/login?msg=시간이 지났습니다. 다시 로그인해 주세요.", 303)
    email, enc, _ = item
    if agree != "yes":
        audit(email, "consent_decline", ip=client_ip(req))
        req.session.clear()
        return RedirectResponse("/login?msg=동의하지 않아 서비스를 시작하지 않았습니다.", 303)
    with db() as c:
        c.execute("""INSERT INTO mail.account(account, display_name, enc_pass, enabled, consent_at, consent_version,
                     last_login_at) VALUES (%s,%s,%s,true,now(),%s,now())
                     ON CONFLICT (account) DO UPDATE SET display_name=EXCLUDED.display_name,
                     enc_pass=EXCLUDED.enc_pass, enabled=true, consent_at=now(),
                     consent_version=EXCLUDED.consent_version, last_login_at=now(), last_error=NULL""",
                  (email, name.strip() or None, enc, CONSENT_VERSION))
    audit(email, "consent_agree", CONSENT_VERSION, client_ip(req))
    req.session["acct"] = email
    return RedirectResponse("/dashboard2?welcome=1", 303)


@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse("/login", 303)


# ---------------------------------------------------------------- 대시보드
DEFAULT_PERIOD = "1m"   # 첫 화면 기본 기간: 최근 1개월
PERIODS = [("today", "오늘"), ("week", "이번 주"), ("7d", "최근 7일"), ("1m", "최근 1개월"), ("month", "이번 달"),
           ("lastmonth", "지난 달"), ("3m", "최근 3개월"), ("all", "전체")]


def resolve_period(period, start, end):
    """기간 프리셋 또는 직접 입력 → (시작일, 종료일, 프리셋키). 종료일 포함."""
    today = date.today()
    s = e = None
    if not period and not start and not end:
        period = DEFAULT_PERIOD
    if period == "today":
        s = e = today
    elif period == "week":
        s, e = today - timedelta(days=today.weekday()), today
    elif period == "7d":
        s, e = today - timedelta(days=6), today
    elif period == "1m":
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        s, e = date(y, m, min(today.day, calendar.monthrange(y, m)[1])), today
    elif period == "month":
        s, e = today.replace(day=1), today
    elif period == "lastmonth":
        e = today.replace(day=1) - timedelta(days=1)
        s = e.replace(day=1)
    elif period == "3m":
        s, e = today - timedelta(days=90), today
    elif period in ("", "all"):
        try:
            s = date.fromisoformat(start) if start else None
            e = date.fromisoformat(end) if end else None
        except ValueError:
            s = e = None
        if s and e and s > e:
            s, e = e, s
        period = "custom" if (s or e) else "all"
    return s, e, period


@app.get("/dashboard")
def dashboard(req: Request, cat: str = "", q: str = "", welcome: int = 0,
              period: str = "", start: str = "", end: str = ""):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    ps, pe, period = resolve_period(period, start, end)
    pwhere, pargs = "", []
    if ps:
        pwhere += " AND m.sent_at >= %s::date"
        pargs.append(ps)
    if pe:
        pwhere += " AND m.sent_at < (%s::date + 1)"
        pargs.append(pe)

    with db() as c:
        acct = c.execute("SELECT enabled, last_sync_at, last_error, display_name FROM mail.account WHERE account=%s",
                         (me,)).fetchone()
        stats = dict(c.execute(f"""SELECT coalesce(r.category,'미분류'), count(*) FROM mail.message m
                                  LEFT JOIN mail.ai_result r ON r.message_id=m.id
                                  WHERE m.account=%s AND m.direction='in' {pwhere} GROUP BY 1""",
                               [me] + pargs).fetchall())
        deadlines = c.execute("""SELECT DISTINCT ON (k) r.deadline, r.category, r.customer, m.subject, r.action, m.id, k
                 FROM (SELECT *, regexp_replace(subject,'^((re|fw|fwd)\\s*:\\s*)+','','i') k FROM mail.message) m
                 JOIN mail.ai_result r ON r.message_id=m.id
                 WHERE m.account=%s AND r.deadline >= CURRENT_DATE
                 ORDER BY k, m.sent_at DESC""", (me,)).fetchall()
        deadlines = sorted(deadlines, key=lambda r: r[0])[:40]
        events = c.execute("""SELECT (r.extracted->>'event_date')::date d, r.category, m.subject, m.id
                 FROM mail.ai_result r JOIN mail.message m ON m.id=r.message_id
                 WHERE m.account=%s AND r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                   AND (r.extracted->>'event_date')::date >= CURRENT_DATE
                 ORDER BY 1 LIMIT 40""", (me,)).fetchall()
        act_where = pwhere if (ps or pe) else " AND m.sent_at > now() - interval '30 days'"
        actions = c.execute(f"""SELECT m.sent_at, r.category, m.subject, r.action, r.deadline, m.id
                 FROM mail.ai_result r JOIN mail.message m ON m.id=r.message_id
                 WHERE m.account=%s AND r.action_needed {act_where}
                 ORDER BY m.sent_at DESC LIMIT 60""", [me] + (pargs if (ps or pe) else [])).fetchall()
        sql, args = _mail_sql(me, pwhere, pargs, cat, q)
        cnt = c.execute(f"SELECT count(*) FROM ({sql}) x", args).fetchone()[0]
        mails = c.execute(sql + " ORDER BY m.sent_at DESC NULLS LAST, m.id DESC LIMIT %s",
                          args + [PAGE]).fetchall()
    total = sum(stats.values()) or 1

    base = {"period": period if period != "custom" else "",
            "start": ps.isoformat() if (period == "custom" and ps) else "",
            "end": pe.isoformat() if (period == "custom" and pe) else "", "cat": cat, "q": q}

    def qs(**kw):
        d = {**base, **kw}
        return "/dashboard?" + urlencode({k: v for k, v in d.items() if v})

    label = dict(PERIODS).get(period, "직접 지정" if period == "custom" else "전체")
    return render(req, "dashboard.html", acct=acct, stats=stats, total=total,
                  categories=[c["code"] for c in cat_info(me) if not c["hidden"]],
                  deadlines=deadlines, events=events, actions=actions, mails=mails, cat=cat, q=q,
                  welcome=welcome, periods=PERIODS, period=period, ps=ps, pe=pe, plabel=label,
                  qs=qs, cnt=cnt, page=PAGE, more_qs=urlencode({k: v for k, v in base.items() if v}))


PAGE = 20   # 받은 메일 목록: 처음 20건, 스크롤하면 20건씩 추가


def _mail_sql(me, pwhere, pargs, cat, q):
    sql = f"""SELECT m.id, m.sent_at, m.from_name, m.from_addr, m.subject, r.category, r.subtype, r.summary,
                    r.action_needed, (SELECT count(*) FROM mail.attachment a WHERE a.message_id=m.id)
             FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id=m.id
             WHERE m.account=%s AND m.direction='in' {pwhere}"""
    args = [me] + list(pargs)
    if cat == "미분류":
        sql += " AND r.category IS NULL"
    elif cat:
        sql += " AND r.category=%s"
        args.append(cat)
    if q:
        sql += " AND (m.subject ILIKE %s OR m.from_name ILIKE %s OR r.summary ILIKE %s)"
        args += [f"%{q}%"] * 3
    return sql, args


@app.get("/dashboard/more")
def dashboard_more(req: Request, offset: int = 0, cat: str = "", q: str = "",
                   period: str = "", start: str = "", end: str = ""):
    """무한 스크롤: 받은 메일 다음 20건 (표의 행 HTML)"""
    me = current(req)
    if not me:
        return Response(status_code=401)
    ps, pe, period = resolve_period(period, start, end)
    pwhere, pargs = "", []
    if ps:
        pwhere += " AND m.sent_at >= %s::date"
        pargs.append(ps)
    if pe:
        pwhere += " AND m.sent_at < (%s::date + 1)"
        pargs.append(pe)
    sql, args = _mail_sql(me, pwhere, pargs, cat, q)
    with db() as c:
        rows = c.execute(sql + " ORDER BY m.sent_at DESC NULLS LAST, m.id DESC LIMIT %s OFFSET %s",
                         args + [PAGE + 1, max(offset, 0)]).fetchall()
    more = len(rows) > PAGE
    resp = render(req, "_mail_rows.html", mails=rows[:PAGE])
    resp.headers["X-Has-More"] = "1" if more else "0"
    return resp


@app.get("/mail/{mid}")
def mail_detail(req: Request, mid: int, msg: str = ""):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        m = c.execute("""SELECT m.id, m.sent_at, m.from_name, m.from_addr, m.to_addrs, m.cc_addrs, m.subject,
                         m.body_text, r.category, r.subtype, r.customer, r.summary, r.action_needed, r.action,
                         r.deadline, r.extracted->>'event_date', r.sec_level, m.has_html,
                         coalesce(r.manual, false), r.rule_hit, r.extracted->>'subtype'
                         FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.id=%s AND m.account=%s""", (mid, me)).fetchone()
        if not m:
            raise HTTPException(404)
        atts = c.execute("SELECT id, filename, size_bytes, is_drm FROM mail.attachment WHERE message_id=%s "
                         "ORDER BY id", (mid,)).fetchall()
        _digest_table(c)
        dg = c.execute("SELECT content, model, created_at FROM mail.ai_digest WHERE message_id=%s", (mid,)).fetchone()
        ocr = c.execute("SELECT text, n_images, error FROM mail.ocr WHERE message_id=%s",
                        (mid,)).fetchone() if _has_ocr_table(c) else None
    digest = None
    if dg:
        try:
            digest = {**_json.loads(dg[0]), "_model": dg[1], "_at": dg[2]}
        except Exception:
            digest = None
    with db() as c:
        subs = [r[0] for r in c.execute("""SELECT DISTINCT subtype FROM mail.ai_result r JOIN mail.message m
                    ON m.id=r.message_id WHERE m.account=%s AND subtype IS NOT NULL
                    UNION SELECT name FROM mail.user_subtype WHERE account=%s ORDER BY 1""", (me, me))]
        urule = None
        if m[19] and m[19].startswith("user:"):
            urule = c.execute("SELECT match_type, pattern FROM mail.user_rule WHERE id=%s AND account=%s",
                              (int(m[19][5:]), me)).fetchone()
    return render(req, "mail.html", m=m, atts=atts, digest=digest, ocr=ocr, subs=subs, urule=urule, msg=msg,
                  dg_route=digest_route(m[8], m[16]),
                  dg_has_key=bool(ANTHROPIC_API_KEY))


@app.get("/att/{aid}")
def attachment(req: Request, aid: int):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        r = c.execute("""SELECT a.stored_path, a.filename FROM mail.attachment a JOIN mail.message m
                         ON m.id=a.message_id WHERE a.id=%s AND m.account=%s""", (aid, me)).fetchone()
    if not r:
        raise HTTPException(404)
    path = Path(r[0]).resolve()
    if DATA_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=r[1] or path.name)


# ---------------------------------------------------------------- 설정
@app.get("/settings")
def settings_page(req: Request, msg: str = ""):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        acct = c.execute("""SELECT enabled, consent_at, consent_version, last_sync_at, last_error,
                            (SELECT count(*) FROM mail.message WHERE account=%s)
                            FROM mail.account WHERE account=%s""", (me, me)).fetchone()
        logs = c.execute("SELECT at, action, ip FROM mail.audit_log WHERE account=%s ORDER BY at DESC LIMIT 10",
                         (me,)).fetchall()
    return render(req, "settings.html", acct=acct, logs=logs, msg=msg)


@app.post("/settings/stop")
def stop(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = req.session.get("acct")
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        c.execute("UPDATE mail.account SET enabled=false, enc_pass=NULL, consent_at=NULL WHERE account=%s", (me,))
        c.execute("UPDATE mail.notify_setting SET tg_chat_id=NULL, tg_linked_at=NULL WHERE account=%s", (me,))
    audit(me, "consent_withdraw", ip=client_ip(req))
    req.session.clear()
    return RedirectResponse("/login?msg=수집을 중지하고 저장된 비밀번호를 삭제했습니다. 기존 메일 데이터는 남아 있습니다.", 303)


@app.post("/settings/delete")
def delete_all(req: Request, confirm: str = Form(""), csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    if confirm.strip().lower() != me:
        return RedirectResponse("/settings?msg=확인용 메일 주소가 일치하지 않습니다.", 303)
    with db() as c:
        n = c.execute("DELETE FROM mail.message WHERE account=%s", (me,)).rowcount   # 첨부·AI결과는 CASCADE
        c.execute("DELETE FROM mail.sync_state WHERE account=%s", (me,))
        c.execute("DELETE FROM mail.account WHERE account=%s", (me,))
        for t in ("notify_rule", "notify_log", "notify_setting", "user_rule", "user_subtype", "category_user",
                  "correction"):
            c.execute(f"DELETE FROM mail.{t} WHERE account=%s", (me,))
    acct_dir = (DATA_DIR / me.replace("/", "_")).resolve()
    if DATA_DIR.resolve() in acct_dir.parents and acct_dir.exists():
        shutil.rmtree(acct_dir, ignore_errors=True)
    audit(me, "data_delete", f"{n} messages", client_ip(req))
    req.session.clear()
    return RedirectResponse(f"/login?msg=메일 {n}건과 첨부·AI 결과·비밀번호를 모두 삭제했습니다.", 303)


# ---------------------------------------------------------------- 텔레그램 알림
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_BOT = {}
LEVELS = [("minimal", "최소: 분류·보낸 곳·마감"), ("summary", "요약: + AI 할 일 (기본)"), ("full", "상세: + 메일 제목")]


def tg_api(method, **params):
    import json
    import urllib.parse
    import urllib.request
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                                 data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=15) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(out.get("description"))
    return out["result"]


def tg_bot_username():
    if "u" not in _TG_BOT and TG_TOKEN:
        try:
            _TG_BOT["u"] = tg_api("getMe")["username"]
        except Exception:
            return os.getenv("TELEGRAM_BOT_USERNAME", "")
    return _TG_BOT.get("u", os.getenv("TELEGRAM_BOT_USERNAME", ""))


def notify_row(c, me):
    c.execute("INSERT INTO mail.notify_setting(account) VALUES (%s) ON CONFLICT DO NOTHING", (me,))
    cols = ["tg_chat_id", "tg_name", "tg_linked_at", "link_code", "link_code_at", "enabled", "content_level",
            "quiet_start", "quiet_end", "weekend_quiet", "remind_days"]
    r = c.execute(f"SELECT {','.join(cols)} FROM mail.notify_setting WHERE account=%s", (me,)).fetchone()
    return dict(zip(cols, r))


@app.get("/notify")
def notify_page(req: Request, msg: str = ""):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        st = notify_row(c, me)
        rules = c.execute("""SELECT id, name, enabled, from_contains, subject_contains, category, subtype_contains,
                             action_only, deadline_days FROM mail.notify_rule WHERE account=%s ORDER BY id""",
                          (me,)).fetchall()
        logs = c.execute("""SELECT l.sent_at, l.kind, l.rule_name, l.tg, m.subject, l.message_id
                            FROM mail.notify_log l LEFT JOIN mail.message m ON m.id=l.message_id
                            WHERE l.account=%s AND l.kind <> 'dup' AND l.error IS DISTINCT FROM '중복 생략' ORDER BY l.sent_at DESC LIMIT 15""",
                         (me,)).fetchall()
    link, link_left, expired = None, 0, False
    if st["link_code"] and st["link_code_at"] and not st["tg_chat_id"]:
        age = (datetime.now(st["link_code_at"].tzinfo) - st["link_code_at"]).total_seconds()
        if age < 15 * 60 - 30:                    # 유효한 코드만 QR 표시 (만료 30초 전부터는 새로 발급 안내)
            link = f"https://t.me/{tg_bot_username()}?start={st['link_code']}"
            link_left = int(15 * 60 - age)
        else:                                     # 지난 코드 → 지우고 [연결 시작] 버튼을 다시 보여 줌
            expired = True
            with db() as c:
                c.execute("UPDATE mail.notify_setting SET link_code=NULL WHERE account=%s", (me,))
            st["link_code"] = None
    qr = None
    if link:
        try:   # 휴대폰 카메라로 찍으면 텔레그램이 바로 열리는 QR (서버에서 SVG 생성, 외부 서비스 사용 안 함)
            import segno
            from markupsafe import Markup
            qr = Markup(segno.make(link, error="m").svg_inline(scale=6, border=2, dark="#00467F"))
        except Exception:
            qr = None
    return render(req, "notify.html", link_left=link_left, link_expired=expired, st=st, rules=rules, logs=logs, msg=msg, link=link, qr=qr,
                  bot=tg_bot_username(), token_ok=bool(TG_TOKEN), levels=LEVELS, categories=[c["code"] for c in cat_info(me)])


@app.get("/api/notify/status")
def api_notify_status(req: Request):
    """알림 설정 화면이 텔레그램 연결 완료를 감지해 자동 새로고침하는 데 사용"""
    me = current(req)
    if not me:
        return _json_ascii({"linked": False}, 401)
    with db() as c:
        r = c.execute("SELECT tg_chat_id IS NOT NULL, link_code IS NOT NULL AND link_code_at > now() - interval '15 minutes' "
                      "FROM mail.notify_setting WHERE account=%s", (me,)).fetchone()
    return _json_ascii({"linked": bool(r and r[0]), "pending": bool(r and r[1])})


@app.post("/notify/link")
def notify_link(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    code = secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")
    with db() as c:
        notify_row(c, me)
        c.execute("UPDATE mail.notify_setting SET link_code=%s, link_code_at=now() WHERE account=%s", (code, me))
    audit(me, "telegram_link_start", ip=client_ip(req))
    return RedirectResponse("/notify", 303)


@app.post("/notify/unlink")
def notify_unlink(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        r = c.execute("SELECT tg_chat_id FROM mail.notify_setting WHERE account=%s", (me,)).fetchone()
        c.execute("UPDATE mail.notify_setting SET tg_chat_id=NULL, tg_linked_at=NULL, link_code=NULL WHERE account=%s",
                  (me,))
    if r and r[0] and TG_TOKEN:                     # 휴대폰에도 해제 사실을 알림
        try:
            tg_api("sendMessage", chat_id=r[0], text="🔕 웹 앱에서 지텍 AI 메일 비서 알림 연결을 해제했습니다.\n"
                                                     "다시 받으려면 웹 앱 [알림 규칙·연결]에서 연결하세요.")
        except Exception:
            pass
    audit(me, "telegram_unlink" if r and r[0] else "telegram_link_cancel", ip=client_ip(req))
    return RedirectResponse("/notify?msg=" + ("텔레그램 연결을 해제했습니다." if r and r[0] else "연결 요청을 취소했습니다."), 303)


@app.post("/notify/test")
def notify_test(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        st = notify_row(c, me)
    if not st["tg_chat_id"]:
        return RedirectResponse("/notify?msg=먼저 텔레그램을 연결하세요.", 303)
    try:
        tg_api("sendMessage", chat_id=st["tg_chat_id"], parse_mode="HTML",
               text="🔔 <b>테스트 알림</b>\n지텍 AI 메일 비서 알림이 정상 동작합니다.")
        m = "테스트 알림을 보냈습니다. 휴대폰을 확인하세요."
    except Exception as e:
        m = f"전송 실패: {e}"
    return RedirectResponse(f"/notify?msg={m}", 303)


@app.post("/notify/settings")
def notify_settings(req: Request, csrf: str = Form(""), enabled: str = Form(""), content_level: str = Form("summary"),
                    quiet_start: int = Form(22), quiet_end: int = Form(7), weekend_quiet: str = Form(""),
                    remind_days: str = Form("3,1")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    if content_level not in dict(LEVELS):
        content_level = "summary"
    rd = ",".join(sorted({x for x in re.findall(r"\d+", remind_days) if 0 < int(x) <= 30}, key=int, reverse=True))
    with db() as c:
        notify_row(c, me)
        c.execute("""UPDATE mail.notify_setting SET enabled=%s, content_level=%s, quiet_start=%s, quiet_end=%s,
                     weekend_quiet=%s, remind_days=%s WHERE account=%s""",
                  (enabled == "on", content_level, max(0, min(23, quiet_start)), max(0, min(23, quiet_end)),
                   weekend_quiet == "on", rd, me))
    return RedirectResponse("/notify?msg=설정을 저장했습니다.", 303)


@app.post("/notify/rule/add")
def rule_add(req: Request, csrf: str = Form(""), name: str = Form(""), from_contains: str = Form(""),
             subject_contains: str = Form(""), category: str = Form(""), subtype_contains: str = Form(""),
             action_only: str = Form(""), deadline_days: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    dd = int(deadline_days) if deadline_days.strip().isdigit() else None
    conds = [from_contains.strip(), subject_contains.strip(), category, subtype_contains.strip(), action_only, dd]
    if not any(x not in ("", None) for x in conds):
        return RedirectResponse("/notify?msg=조건을 하나 이상 입력하세요.", 303)
    if not name.strip():   # 이름을 비우면 조건으로 자동 생성
        parts = []
        if subject_contains.strip():
            parts.append(f"제목: {subject_contains.strip()}")
        if from_contains.strip():
            parts.append(f"보낸 사람: {from_contains.strip()}")
        if category:
            parts.append(category + (f"·{subtype_contains.strip()}" if subtype_contains.strip() else ""))
        elif subtype_contains.strip():
            parts.append(f"세부: {subtype_contains.strip()}")
        if action_only == "on":
            parts.append("조치 필요")
        if dd is not None:
            parts.append(f"마감 {dd}일 이내")
        name = " / ".join(parts)
    with db() as c:
        c.execute("""INSERT INTO mail.notify_rule(account,name,from_contains,subject_contains,category,subtype_contains,
                     action_only,deadline_days) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (me, name.strip()[:50], from_contains.strip() or None, subject_contains.strip() or None,
                   category or None, subtype_contains.strip() or None, action_only == "on", dd))
    return RedirectResponse("/notify?msg=규칙을 추가했습니다.", 303)


@app.post("/notify/rule/{rid}/{op}")
def rule_op(req: Request, rid: int, op: str, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        if op == "toggle":
            c.execute("UPDATE mail.notify_rule SET enabled = NOT enabled WHERE id=%s AND account=%s", (rid, me))
        elif op == "delete":
            c.execute("DELETE FROM mail.notify_rule WHERE id=%s AND account=%s", (rid, me))
    return RedirectResponse("/notify", 303)


# ---------------------------------------------------------------- 사내 알림 (알림센터 · PC 바탕화면 알림)
import hashlib
import json as _json
from fastapi.responses import Response


def _json_ascii(obj, status=200):
    # PowerShell 5.1 이 한글을 깨뜨리지 않도록 ASCII(\uXXXX)로 응답
    return Response(_json.dumps(obj, ensure_ascii=True, default=str), status_code=status,
                    media_type="application/json")


@app.get("/alerts")
def alerts_page(req: Request, show: str = "all"):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    where = " AND l.read_at IS NULL" if show == "unread" else ""
    with db() as c:
        rows = c.execute(f"""SELECT l.id, l.sent_at, l.kind, l.title, l.body, l.rule_name, l.read_at, l.message_id, l.tg,
                 l.tg_at, l.tg_error, l.tg_try, l.pc_at
                 FROM mail.notify_log l WHERE l.account=%s AND (l.kind='new' OR l.kind LIKE 'remind-%%')
                 AND l.error IS DISTINCT FROM '중복 생략' AND l.title IS NOT NULL {where}
                 ORDER BY l.sent_at DESC LIMIT 200""", (me,)).fetchall()
        unread = c.execute("SELECT count(*) FROM mail.notify_log WHERE account=%s AND read_at IS NULL "
                           "AND title IS NOT NULL", (me,)).fetchone()[0]
        pc = c.execute("SELECT pc_token_hash IS NOT NULL, pc_seen_at FROM mail.notify_setting WHERE account=%s",
                       (me,)).fetchone()
        st = c.execute("""SELECT tg_chat_id IS NOT NULL, enabled, quiet_start, quiet_end, weekend_quiet
                          FROM mail.notify_setting WHERE account=%s""", (me,)).fetchone()
    quiet = _quiet_until(st) if st and st[0] and st[1] else None
    items = [dict(r=r, tg=_tg_status(r, quiet), pc=_pc_status(r, pc)) for r in rows]
    stats = {"sent": sum(1 for i in items if i["tg"][0] == "ok"),
             "wait": sum(1 for i in items if i["tg"][0] == "wait"),
             "bad": sum(1 for i in items if i["tg"][0] == "bad")}
    return render(req, "alerts.html", rows=rows, items=items, unread=unread, show=show, pc=pc,
                  tg_linked=bool(st and st[0]), quiet=quiet, stats=stats)


def _quiet_until(st):
    """조용한 시간이면 끝나는 시각 문구, 아니면 None (notifier.is_quiet 과 같은 기준)"""
    _, _, qs, qe, wk = st
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if wk and now.weekday() >= 5:
        return "주말 (월요일 %02d시 이후 전송)" % qe
    h = now.hour
    if qs != qe and ((h >= qs or h < qe) if qs > qe else (qs <= h < qe)):
        return "%02d시~%02d시 (%02d시 이후 전송)" % (qs, qe, qe)
    return None


def _tg_status(r, quiet):
    """(종류, 아이콘·문구, 설명) - 종류: ok / wait / bad / off"""
    tg, tg_at, err, tries = r[8], r[9], r[10], r[11] or 0
    if tg == "sent":
        return ("ok", "✅ 전송 " + (tg_at.strftime("%H:%M") if tg_at else ""), "텔레그램 전송 완료")
    if tg == "pending":
        if err:
            return ("bad", f"⚠️ 재시도 {tries}/5", "전송 실패 → 1분 뒤 자동 재시도: " + err)
        if quiet:
            return ("wait", "🌙 보류", "조용한 시간 " + quiet)
        return ("wait", "⏳ 대기", "1분 안에 전송 예정")
    if tg == "fail":
        return ("bad", "❌ 실패", "5회 실패: " + (err or ""))
    if tg == "expired":
        return ("bad", "⌛ 만료", "24시간 동안 보내지 못해 생략 (조용한 시간·장애)")
    if tg == "none":
        return ("off", "— 미연결", err or "알림 당시 텔레그램 미연결")
    if tg == "skip":
        return ("off", "— 생략", "중복 알림")
    return ("off", "", "")


def _pc_status(r, pc):
    pc_at = r[12]
    if pc_at:
        return ("ok", "✅ 표시 " + pc_at.strftime("%H:%M"), "PC 바탕화면에 표시됨")
    if not (pc and pc[0]):
        return ("off", "— 미설치", "PC 알림 프로그램 미설치")
    if r[1] < datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(hours=24):
        return ("off", "— 생략", "24시간 지나 PC 표시 생략")
    return ("wait", "⏳ 대기", "PC 프로그램이 다음 확인(1분) 때 표시")


@app.get("/alerts/open/{aid}")
def alert_open(req: Request, aid: int):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        r = c.execute("UPDATE mail.notify_log SET read_at=coalesce(read_at, now()) WHERE id=%s AND account=%s "
                      "RETURNING message_id", (aid, me)).fetchone()
    return RedirectResponse(f"/dashboard2?m={r[0]}" if r and r[0] else "/alerts", 303)


@app.post("/alerts/read-all")
def alerts_read_all(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        c.execute("UPDATE mail.notify_log SET read_at=now() WHERE account=%s AND read_at IS NULL", (me,))
    return RedirectResponse("/alerts", 303)


@app.get("/api/alerts/count")
def api_alert_count(req: Request):
    me = current(req)
    if not me:
        return _json_ascii({"unread": 0}, 401)
    with db() as c:
        n = c.execute("SELECT count(*) FROM mail.notify_log WHERE account=%s AND read_at IS NULL AND title IS NOT NULL",
                      (me,)).fetchone()[0]
        last = c.execute("SELECT max(id) FROM mail.notify_log WHERE account=%s AND title IS NOT NULL "
                         "AND error IS DISTINCT FROM '중복 생략'", (me,)).fetchone()[0]
    return _json_ascii({"unread": n, "last_id": last or 0})


@app.post("/notify/pc-token")
def pc_token(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    token = "ztec_" + secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO mail.notify_setting(account) VALUES (%s) ON CONFLICT DO NOTHING", (me,))
        c.execute("UPDATE mail.notify_setting SET pc_token_hash=%s WHERE account=%s",
                  (hashlib.sha256(token.encode()).hexdigest(), me))
    audit(me, "pc_token_issue", ip=client_ip(req))
    return render(req, "pc_token.html", token=token)       # 토큰은 이 화면에서 한 번만 표시


@app.post("/notify/pc-token/revoke")
def pc_token_revoke(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        c.execute("UPDATE mail.notify_setting SET pc_token_hash=NULL WHERE account=%s", (me,))
    audit(me, "pc_token_revoke", ip=client_ip(req))
    return RedirectResponse("/alerts", 303)


@app.get("/api/pc/alerts")
def api_pc_alerts(req: Request):
    """PC 알림 프로그램용: 아직 바탕화면에 표시하지 않은 알림(24시간 이내)을 돌려주고 '표시됨' 처리"""
    tok = req.headers.get("x-token", "")
    if not tok.startswith("ztec_"):
        return _json_ascii({"error": "unauthorized"}, 401)
    h = hashlib.sha256(tok.encode()).hexdigest()
    with db() as c:
        r = c.execute("""SELECT s.account FROM mail.notify_setting s JOIN mail.account a ON a.account=s.account
                         WHERE s.pc_token_hash=%s AND a.consent_at IS NOT NULL""", (h,)).fetchone()
        if not r:
            return _json_ascii({"error": "unauthorized"}, 401)
        acct = r[0]
        c.execute("UPDATE mail.notify_setting SET pc_seen_at=now() WHERE account=%s", (acct,))
        rows = c.execute("""UPDATE mail.notify_log SET pc_at=now()
                 WHERE account=%s AND pc_at IS NULL AND title IS NOT NULL AND sent_at > now() - interval '24 hours'
                 RETURNING id, title, body, message_id, kind""", (acct,)).fetchall()
        unread = c.execute("SELECT count(*) FROM mail.notify_log WHERE account=%s AND read_at IS NULL "
                           "AND title IS NOT NULL", (acct,)).fetchone()[0]
    base = str(req.base_url).rstrip("/")
    items = [{"id": i, "title": t, "body": b, "kind": k, "url": f"{base}/alerts/open/{i}"}
             for i, t, b, mid, k in sorted(rows)]
    return _json_ascii({"account": acct, "unread": unread, "items": items})


# ---------------------------------------------------------------- 관리자 (내용 열람 불가, 현황만)
@app.get("/admin")
def admin(req: Request, msg: str = ""):
    me = current(req)
    if not me or me not in ADMINS:
        raise HTTPException(403)
    with db() as c:
        rows = c.execute("""SELECT a.account, a.display_name, a.enabled, a.consent_at, a.last_sync_at, a.last_error,
                    (SELECT count(*) FROM mail.message m WHERE m.account=a.account),
                    (SELECT count(*) FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id=m.id
                      WHERE m.account=a.account AND m.direction='in' AND r.message_id IS NULL)
                    FROM mail.account a ORDER BY a.consent_at DESC NULLS LAST""").fetchall()
        jobs = c.execute("""SELECT id, requested_by, scope, filter, total, done, changed, status, error, created_at,
                            finished_at FROM mail.reclass_job WHERE scope <> 'ids' ORDER BY id DESC LIMIT 12""").fetchall()
        defaults = c.execute("SELECT code, label, color, sort FROM mail.category ORDER BY sort").fetchall()
        sugg = c.execute("""SELECT split_part(lower(x.from_addr),'@',2) d, x.new_category, count(*) n,
                                   count(DISTINCT x.account) u, max(x.created_at)
                            FROM mail.correction x
                            WHERE x.new_category IS DISTINCT FROM x.old_category AND x.from_addr LIKE '%%@%%'
                              AND NOT EXISTS (SELECT 1 FROM mail.rule_extra e WHERE e.kind='domain'
                                    AND e.pattern = split_part(lower(x.from_addr),'@',2))
                            GROUP BY 1, 2 HAVING count(*) >= 1 ORDER BY n DESC, u DESC LIMIT 20""").fetchall()
        extras = c.execute("SELECT id, pattern, category, added_by, created_at FROM mail.rule_extra ORDER BY id DESC").fetchall()
        stale = c.execute("SELECT count(*) FROM mail.ai_result WHERE NOT manual AND rule_version IS DISTINCT FROM %s",
                          (current_rule_version(c),)).fetchone()[0]
    return render(req, "admin.html", rows=rows, jobs=jobs, defaults=defaults, sugg=sugg, extras=extras,
                  stale=stale, rule_ver=_RULEVER.get("v"), msg=msg, bl={d[0]: d[1] for d in defaults})


# ---------------------------------------------------------------- AI 요약 정리 (버튼을 누를 때만, 결과는 저장해 재사용)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen25-zt:latest")
DIGEST_MAX_CHARS = 8000
# 외부 Claude: 사내업무·DRM(L1)·미분류는 항상 사내 AI, 나머지(정부과제·외부협력·뉴스레터·시스템알림)는 Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
LOCAL_ONLY_CATEGORIES = {c.strip() for c in os.getenv("DIGEST_LOCAL_CATEGORIES", "사내업무").split(",") if c.strip()}
_DIGEST_BUSY = set()
DIGEST_SYSTEM = """당신은 회사 업무 메일을 정리하는 비서입니다. 메일 본문만 근거로, 없는 내용은 만들지 말고 한국어로 정리하세요.
반드시 아래 JSON 형식으로만 답하세요. 해당 내용이 없으면 빈 배열 [] 또는 빈 문자열.
{"one_line": "메일의 핵심을 한 문장(60자 이내)",
 "points": ["주요 내용 3~6개, 각 한 문장"],
 "todo": ["받는 사람(나)이 해야 할 일. 없으면 []"],
 "dates": [{"date": "YYYY-MM-DD", "what": "무엇의 날짜인지"}],
 "figures": ["금액·수량·사양 등 중요한 숫자 정보"],
 "people": ["언급된 담당자·기관 (이름/소속)"]}
[날짜 규칙]
- dates 에는 받는 사람이 챙겨야 할 날짜만: 마감·제출·신청 기한, 행사·회의·교육 일시, 납기·일정.
- 메일 발송일, 기사 작성일, 뉴스 날짜, 과거 사건 날짜는 넣지 마세요.
- 연도가 없으면 '보낸 날짜'의 연도를 쓰고(보낸 날짜보다 앞선 월이면 다음 해), 반드시 YYYY-MM-DD 로."""


def _has_ocr_table(c):
    return c.execute("SELECT to_regclass('mail.ocr') IS NOT NULL").fetchone()[0]


def _digest_table(c):
    c.execute("""CREATE TABLE IF NOT EXISTS mail.ai_digest (
                   message_id BIGINT PRIMARY KEY REFERENCES mail.message(id) ON DELETE CASCADE,
                   content TEXT NOT NULL, model TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def digest_route(category, sec_level):
    """'claude' 또는 'local' - 사내업무·L1(DRM)·미분류·API 키 없음 → local"""
    if not ANTHROPIC_API_KEY or not category or category in LOCAL_ONLY_CATEGORIES or (sec_level or "") == "L1":
        return "local"
    return "claude"


def _claude_text(user):
    import urllib.request
    payload = {"model": CLAUDE_MODEL, "max_tokens": 1500, "temperature": 0.1, "system": DIGEST_SYSTEM,
               "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=_json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                          "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as r:
        out = _json.loads(r.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")


def _llm_digest(subject, sender, body, route="local", sent=None):
    import urllib.request
    body = re.sub(r"\n{3,}", "\n\n", body or "").strip()
    if len(body) > DIGEST_MAX_CHARS:
        body = body[:DIGEST_MAX_CHARS] + "\n…(이하 생략)"
    user = (f"제목: {subject}\n보낸 사람: {sender}\n보낸 날짜: {sent.strftime('%Y-%m-%d') if sent else '미상'}"
            f"\n\n본문:\n{body}")
    if route == "claude":
        out = _claude_text(user)
    else:
        payload = {"model": LLM_MODEL, "stream": False, "format": "json",
                   "messages": [{"role": "system", "content": DIGEST_SYSTEM}, {"role": "user", "content": user}],
                   "options": {"temperature": 0.1, "num_ctx": int(os.getenv("NUM_CTX", "8192"))}}
        req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=_json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as r:
            out = _json.loads(r.read().decode("utf-8"))["message"]["content"]
    out = re.sub(r"(?s)<think>.*?</think>", "", out).strip()
    m = re.search(r"\{.*\}", out, re.S)
    d = _json.loads(m.group(0) if m else out)
    lst = lambda v: [str(x).strip() for x in (v if isinstance(v, list) else [v] if v else []) if str(x).strip()]
    dates = []
    sent_d = sent.astimezone(ZoneInfo("Asia/Seoul")).date() if sent else None
    for x in d.get("dates") or []:
        if not (isinstance(x, dict) and (x.get("date") or x.get("what"))):
            continue
        ds, what = str(x.get("date") or "").strip(), str(x.get("what") or "").strip()
        mm = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", ds)
        if mm and sent_d:
            try:
                dd = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
            except ValueError:
                dd = None
            if dd and (dd.month, dd.day) == (sent_d.month, sent_d.day) and re.search(r"발송|작성|보도|기사|발표|제시|게재", what):
                continue                      # 발송일·기사 날짜 → 제외
            if dd and dd.year < sent_d.year - 1 and dd.replace(year=sent_d.year) >= sent_d - timedelta(days=31):
                dd = dd.replace(year=sent_d.year)   # 연도 오인식(예: 2023 → 2026) 보정
                ds = dd.isoformat()
        dates.append({"date": ds, "what": what})
    return {"one_line": str(d.get("one_line") or "").strip(), "points": lst(d.get("points")),
            "todo": lst(d.get("todo")), "dates": dates, "figures": lst(d.get("figures")),
            "people": lst(d.get("people"))}


@app.post("/mail/{mid}/digest")
def mail_digest(req: Request, mid: int, csrf: str = Form("")):
    me = current(req)
    if not me:
        return _json_ascii({"error": "로그인이 필요합니다"}, 401)
    if not csrf or csrf != req.session.get("csrf"):
        return _json_ascii({"error": "화면을 새로고침한 뒤 다시 시도하세요"}, 403)
    with db() as c:
        m = c.execute("""SELECT m.subject, coalesce(m.from_name, m.from_addr), m.body_text, r.category, r.sec_level,
                         m.sent_at FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.id=%s AND m.account=%s""", (mid, me)).fetchone()
        o = c.execute("SELECT text FROM mail.ocr WHERE message_id=%s", (mid,)).fetchone() \
            if (m and _has_ocr_table(c)) else None
    if m and o and o[0]:
        m = (m[0], m[1], (m[2] or "") + "\n\n[본문 이미지 속 글자 (OCR, 오탈자 있을 수 있음)]\n" + o[0], m[3], m[4], m[5])
    if not m:
        return _json_ascii({"error": "메일을 찾을 수 없습니다"}, 404)
    if not (m[2] or "").strip():
        return _json_ascii({"error": "본문이 없어 정리할 내용이 없습니다 (첨부·DRM 메일일 수 있음)"}, 400)
    if mid in _DIGEST_BUSY:
        return _json_ascii({"error": "이미 정리 중입니다. 잠시 후 새로고침하세요"}, 409)
    _DIGEST_BUSY.add(mid)
    t0 = time.time()
    route = digest_route(m[3], m[4])
    used = CLAUDE_MODEL if route == "claude" else LLM_MODEL
    note = ""
    try:
        try:
            d = _llm_digest(m[0], m[1], m[2], route, m[5])
        except Exception as e:
            if route != "claude":
                raise
            # 외부 Claude 실패(네트워크·키·한도) → 사내 AI로 대신 정리
            note = f"외부 Claude 실패({str(e)[:60]}) → 사내 AI로 정리"
            route, used = "local", LLM_MODEL
            d = _llm_digest(m[0], m[1], m[2], "local", m[5])
    except Exception as e:
        return _json_ascii({"error": f"AI 응답 실패: {str(e)[:120]}"}, 502)
    finally:
        _DIGEST_BUSY.discard(mid)
    if route == "claude":
        audit(me, "digest_external", f"mail={mid} model={CLAUDE_MODEL} cat={m[3]}", req.client.host if req.client else None)
    with db() as c:
        _digest_table(c)
        c.execute("""INSERT INTO mail.ai_digest(message_id, content, model) VALUES (%s,%s,%s)
                     ON CONFLICT (message_id) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model,
                     created_at=now()""", (mid, _json.dumps(d, ensure_ascii=False), used))
    return _json_ascii({"ok": True, "sec": round(time.time() - t0, 1), "route": route, "note": note})


# ---------------------------------------------------------------- HTML 본문 보기 (이미지 메일)
import email as _email
import urllib.parse
from email import policy as _policy


def _load_eml(me, mid):
    with db() as c:
        r = c.execute("SELECT raw_path FROM mail.message WHERE id=%s AND account=%s", (mid, me)).fetchone()
    if not r or not r[0]:
        return None
    path = Path(r[0]).resolve()
    if DATA_DIR.resolve() not in path.parents or not path.exists():
        return None
    return _email.message_from_bytes(path.read_bytes(), policy=_policy.compat32)


def _part_text(part):
    raw = part.get_payload(decode=True) or b""
    cs = (part.get_content_charset() or "").lower().replace("ks_c_5601-1987", "cp949").replace("euc-kr", "cp949")
    for c in filter(None, (cs, "utf-8", "cp949")):
        try:
            return raw.decode(c)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _clean_html(h, mid):
    """스크립트·이벤트·폼·외부 프레임 제거 (+ 화면 쪽 iframe sandbox·CSP로 이중 차단)"""
    h = re.sub(r"(?is)<(script|iframe|object|applet|frameset)\b.*?</\1\s*>", "", h)
    h = re.sub(r"(?is)</?(script|iframe|object|embed|form|frame|frameset|applet|input|button|textarea)\b[^>]*>", "", h)
    h = re.sub(r"(?is)<(meta|base|link)\b[^>]*>", "", h)
    h = re.sub(r"(?i)\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*(javascript|vbscript):[^\"']*\2", r'\1="#"', h)
    h = re.sub(r"(?i)(src|background)\s*=\s*([\"'])cid:([^\"']+)\2",
               lambda m: f'{m.group(1)}="/mail/{mid}/cid/{urllib.parse.quote(m.group(3).strip("<>"))}"', h)
    head = ('<meta charset="utf-8"><base target="_blank">'
            '<style>body{font-family:"Malgun Gothic",sans-serif;font-size:14px;margin:8px;word-break:break-word}'
            'img{max-width:100%;height:auto}table{max-width:100%}</style>')
    return "<!DOCTYPE html><html><head>" + head + "</head><body>" + h + "</body></html>"


@app.get("/mail/{mid}/html")
def mail_html(req: Request, mid: int):
    me = current(req)
    if not me:
        return Response(status_code=401)
    msg = _load_eml(me, mid)
    html_parts = []
    if msg:
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename() \
                    and "attachment" not in (part.get("Content-Disposition") or "").lower():
                html_parts.append(_part_text(part))
    if not html_parts:
        return Response("<p style='font-family:sans-serif;color:#888'>HTML 본문이 없습니다.</p>",
                        media_type="text/html; charset=utf-8")
    body = "\n".join(re.sub(r"(?is)^.*?<body[^>]*>|</body>.*$", "", h) for h in html_parts)
    resp = Response(_clean_html(body, mid), media_type="text/html; charset=utf-8")
    # 스크립트 금지 · 이미지(사내 cid / 외부 http(s))와 인라인 스타일만 허용
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self' data: http: https:; "
                                               "style-src 'unsafe-inline' http: https:; font-src data: http: https:")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/mail/{mid}/cid/{cid:path}")
def mail_cid(req: Request, mid: int, cid: str):
    """본문에 들어 있는 이미지(cid:) - .eml 안에 있으면 그대로, 첨부로 분리 저장됐으면 저장소에서"""
    me = current(req)
    if not me:
        return Response(status_code=401)
    msg = _load_eml(me, mid)
    if not msg:
        raise HTTPException(404)
    want = cid.strip("<>").lower()
    for part in msg.walk():
        if part.is_multipart():
            continue
        pid = (part.get("Content-ID") or "").strip().strip("<>").lower()
        if pid != want:
            continue
        ctype = part.get_content_type() if part.get_content_maintype() == "image" else "application/octet-stream"
        sha = part.get("X-Ztec-Blob-SHA256")
        if sha:
            with db() as c:
                r = c.execute("""SELECT a.stored_path FROM mail.attachment a JOIN mail.message m ON m.id=a.message_id
                                 WHERE a.message_id=%s AND m.account=%s AND a.sha256=%s LIMIT 1""",
                              (mid, me, sha.strip())).fetchone()
            if not r or not r[0]:
                raise HTTPException(404)
            path = Path(r[0]).resolve()
            if DATA_DIR.resolve() not in path.parents or not path.exists():
                raise HTTPException(404)
            data = path.read_bytes()
        else:
            data = part.get_payload(decode=True) or b""
        return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400",
                                                         "X-Content-Type-Options": "nosniff"})
    raise HTTPException(404)


# ================================================================ P3: 재분류 · 분류 이름 편집 · 개인 규칙
import subprocess
import sys as _sys

CLASSIFY_PY = BASE.parent / "classifier" / "classify.py"
LOG_DIR = BASE.parent / "logs"
MATCH_TYPES = {"from_addr": "보낸 주소", "from_domain": "보낸 도메인", "subject": "제목 키워드"}
_RULEVER = {}


@app.on_event("startup")
def _p3_schema():
    try:
        with db() as c:
            c.execute((BASE / "schema_p3.sql").read_text(encoding="utf-8"))
    except Exception as e:
        print("schema_p3 적용 실패:", e)


def cat_info(account):
    """[{code,label,color,sort,hidden}] - 회사 기본값 + 개인 설정"""
    try:
        with db() as c:
            rows = c.execute("""SELECT k.code, coalesce(u.label, k.label), coalesce(u.color, k.color),
                                       coalesce(u.sort, k.sort), coalesce(u.hidden, false), k.label, k.color
                                FROM mail.category k LEFT JOIN mail.category_user u
                                  ON u.code=k.code AND u.account=%s ORDER BY 4, k.sort""", (account or "",)).fetchall()
    except Exception:
        rows = [(c, c, None, i, False, c, None) for i, c in enumerate(CATEGORIES)]
    return [dict(code=r[0], label=r[1], color=r[2], sort=r[3], hidden=r[4], base_label=r[5], base_color=r[6])
            for r in rows]


def current_rule_version(c):
    """classify.py 와 같은 방식으로 규칙 버전 계산 (분류기를 import 해서 사용)"""
    try:
        if str(CLASSIFY_PY.parent) not in _sys.path:
            _sys.path.insert(0, str(CLASSIFY_PY.parent))
        import classify as _K
        _K.load_ctx(c)
        _RULEVER["v"] = _K.CTX["version"]
    except Exception as e:
        _RULEVER["v"] = None
        print("규칙 버전 계산 실패:", e)
    return _RULEVER.get("v")


def spawn_job(job_id):
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / f"reclass_{job_id}.log", "ab")
    subprocess.Popen([_sys.executable, str(CLASSIFY_PY), "--job", str(job_id)], stdout=log, stderr=log,
                     cwd=str(BASE.parent), start_new_session=True)


def new_job(c, by, f, scope):
    return c.execute("INSERT INTO mail.reclass_job(requested_by, scope, filter) VALUES (%s,%s,%s) RETURNING id",
                     (by, scope, _json.dumps(f, ensure_ascii=False))).fetchone()[0]


def _domain(addr):
    return (addr or "").split("@")[-1].lower()


# ---------------------------------------------------------------- 1단계: 메일별 분류 수정 · AI 다시 분석
@app.post("/mail/{mid}/classify")
def mail_classify(req: Request, mid: int, category: str = Form(...), subtype: str = Form(""),
                  remember: str = Form("none"), csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    codes = {x["code"] for x in cat_info(me)}
    if category not in codes:
        raise HTTPException(400)
    subtype = subtype.strip()[:40] or None
    job = None
    with db() as c:
        m = c.execute("""SELECT m.from_addr, m.subject, r.category, r.subtype, r.sec_level FROM mail.message m
                         LEFT JOIN mail.ai_result r ON r.message_id=m.id WHERE m.id=%s AND m.account=%s""",
                      (mid, me)).fetchone()
        if not m:
            raise HTTPException(404)
        sec = m[4] if m[4] == "L1" else {"정부과제": "L3", "뉴스레터": "L3"}.get(category, "L2")
        c.execute("""INSERT INTO mail.ai_result(message_id, category, subtype, sec_level, summary, manual, manual_at,
                                                rule_hit, processed_at)
                     VALUES (%s,%s,%s,%s,%s,true,now(),'manual',now())
                     ON CONFLICT (message_id) DO UPDATE SET category=EXCLUDED.category, subtype=EXCLUDED.subtype,
                       sec_level=EXCLUDED.sec_level, manual=true, manual_at=now(), rule_hit='manual'""",
                  (mid, category, subtype, sec, m[1]))
        c.execute("""INSERT INTO mail.correction(account, message_id, from_addr, old_category, new_category,
                     old_subtype, new_subtype) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                  (me, mid, (m[0] or "").lower(), m[2], category, m[3], subtype))
        msg = "분류를 수정했습니다 (수동 확정 — 일괄 재분류에서 바뀌지 않음)"
        if remember in ("from_addr", "from_domain") and m[0]:
            pat = (m[0] or "").lower() if remember == "from_addr" else _domain(m[0])
            c.execute("""INSERT INTO mail.user_rule(account, match_type, pattern, category, subtype)
                         VALUES (%s,%s,%s,%s,%s) ON CONFLICT (account, match_type, pattern)
                         DO UPDATE SET category=EXCLUDED.category, subtype=EXCLUDED.subtype""",
                      (me, remember, pat, category, subtype))
            job = new_job(c, me, {"account": me, "user_rule": {"match_type": remember, "pattern": pat}}, "user_rule")
            msg += f" · 개인 규칙 저장: {MATCH_TYPES[remember]} '{pat}' → 기존 메일도 다시 분류 중"
    if job:
        spawn_job(job)
    audit(me, "classify_manual", f"mail={mid} {m[2]}→{category} remember={remember}", client_ip(req))
    return RedirectResponse(f"/mail/{mid}?msg={urllib.parse.quote(msg)}", 303)


@app.post("/mail/{mid}/reanalyze")
def mail_reanalyze(req: Request, mid: int, csrf: str = Form("")):
    me = current(req)
    if not me:
        return _json_ascii({"error": "로그인이 필요합니다"}, 401)
    if not csrf or csrf != req.session.get("csrf"):
        return _json_ascii({"error": "화면을 새로고침한 뒤 다시 시도하세요"}, 403)
    with db() as c:
        if not c.execute("SELECT 1 FROM mail.message WHERE id=%s AND account=%s", (mid, me)).fetchone():
            return _json_ascii({"error": "메일을 찾을 수 없습니다"}, 404)
        c.execute("""INSERT INTO mail.ai_result(message_id, category, summary, processed_at)
                     SELECT id, NULL, subject, now() FROM mail.message WHERE id=%s ON CONFLICT DO NOTHING""", (mid,))
        job = new_job(c, me, {"ids": [mid]}, "ids")
    t0 = time.time()
    try:
        r = subprocess.run([_sys.executable, str(CLASSIFY_PY), "--job", str(job)], cwd=str(BASE.parent),
                           capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return _json_ascii({"error": "AI 분석 시간 초과 (5분). 잠시 후 다시 시도하세요"}, 504)
    with db() as c:
        st = c.execute("SELECT status, error FROM mail.reclass_job WHERE id=%s", (job,)).fetchone()
    if not st or st[0] != "done":
        return _json_ascii({"error": "AI 분석 실패: " + ((st and st[1]) or r.stderr.decode("utf-8", "replace")[-200:])}, 500)
    audit(me, "reanalyze", f"mail={mid}", client_ip(req))
    if st[1]:
        return _json_ascii({"error": "AI 분석 실패 — 기존 결과를 그대로 두었습니다 (" + st[1][-100:] + ")"}, 502)
    return _json_ascii({"ok": True, "sec": round(time.time() - t0, 1)})


# ---------------------------------------------------------------- 2·3단계: 내 분류 설정 (이름·색·순서·숨김, 개인 규칙, 개인 세부분류)
@app.get("/settings/categories")
def categories_page(req: Request, msg: str = ""):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        rules = c.execute("""SELECT id, match_type, pattern, category, subtype, hits, created_at FROM mail.user_rule
                             WHERE account=%s ORDER BY id DESC""", (me,)).fetchall()
        subs = c.execute("""SELECT id, category, name, keywords FROM mail.user_subtype WHERE account=%s
                            ORDER BY category, name""", (me,)).fetchall()
        corr = c.execute("""SELECT x.created_at, m.subject, x.old_category, x.new_category, x.new_subtype, x.message_id
                            FROM mail.correction x LEFT JOIN mail.message m ON m.id=x.message_id
                            WHERE x.account=%s ORDER BY x.id DESC LIMIT 10""", (me,)).fetchall()
        running = c.execute("""SELECT id, total, done, status FROM mail.reclass_job WHERE requested_by=%s
                               AND scope='user_rule' ORDER BY id DESC LIMIT 1""", (me,)).fetchone()
    return render(req, "categories.html", rules=rules, subs=subs, corr=corr, running=running, msg=msg,
                  match_types=MATCH_TYPES, cats_all=cat_info(me))


@app.post("/settings/categories/save")
async def categories_save(req: Request):
    form = await req.form()
    check_csrf(req, form.get("csrf", ""))
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        for x in cat_info(me):
            code = x["code"]
            label = (form.get(f"label_{code}") or "").strip()[:20]
            color = (form.get(f"color_{code}") or "").strip()
            color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else None
            try:
                sort = int(form.get(f"sort_{code}") or x["sort"])
            except ValueError:
                sort = x["sort"]
            hidden = form.get(f"hidden_{code}") == "1"
            c.execute("""INSERT INTO mail.category_user(account, code, label, color, sort, hidden)
                         VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (account, code) DO UPDATE SET
                         label=EXCLUDED.label, color=EXCLUDED.color, sort=EXCLUDED.sort, hidden=EXCLUDED.hidden""",
                      (me, code, None if not label or label == x["base_label"] else label,
                       None if not color or color.lower() == (x["base_color"] or "").lower() else color, sort, hidden))
    return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote("분류 이름·색·순서를 저장했습니다 (재분류 불필요)"), 303)


@app.post("/settings/categories/reset")
def categories_reset(req: Request, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        c.execute("DELETE FROM mail.category_user WHERE account=%s", (me,))
    return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote("회사 기본값으로 되돌렸습니다"), 303)


@app.post("/settings/rule/add")
def user_rule_add(req: Request, match_type: str = Form(...), pattern: str = Form(...), category: str = Form(...),
                  subtype: str = Form(""), csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    pattern = pattern.strip().lower()
    if match_type == "from_domain":
        pattern = pattern.lstrip("@")
    if match_type not in MATCH_TYPES or not pattern or category not in {x["code"] for x in cat_info(me)}:
        return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote("조건과 분류를 확인하세요"), 303)
    with db() as c:
        c.execute("""INSERT INTO mail.user_rule(account, match_type, pattern, category, subtype) VALUES (%s,%s,%s,%s,%s)
                     ON CONFLICT (account, match_type, pattern) DO UPDATE SET category=EXCLUDED.category,
                     subtype=EXCLUDED.subtype""", (me, match_type, pattern[:120], category, subtype.strip()[:40] or None))
        job = new_job(c, me, {"account": me, "user_rule": {"match_type": match_type, "pattern": pattern}}, "user_rule")
    spawn_job(job)
    return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote("개인 규칙을 저장했습니다 · 기존 메일 다시 분류 중"), 303)


@app.post("/settings/rule/{rid}/delete")
def user_rule_delete(req: Request, rid: int, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        c.execute("DELETE FROM mail.user_rule WHERE id=%s AND account=%s", (rid, me))
    return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote(
        "규칙을 삭제했습니다 (이미 분류된 메일은 그대로 · 필요하면 메일에서 [AI 다시 분석])"), 303)


@app.post("/settings/subtype/add")
def user_subtype_add(req: Request, category: str = Form(...), name: str = Form(...), keywords: str = Form(...),
                     csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    name, keywords = name.strip()[:30], ",".join(k.strip() for k in keywords.split(",") if k.strip())[:300]
    if not name or not keywords or category not in {x["code"] for x in cat_info(me)}:
        return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote("분류·이름·키워드를 모두 입력하세요"), 303)
    with db() as c:
        c.execute("""INSERT INTO mail.user_subtype(account, category, name, keywords) VALUES (%s,%s,%s,%s)
                     ON CONFLICT (account, category, name) DO UPDATE SET keywords=EXCLUDED.keywords""",
                  (me, category, name, keywords))
        job = new_job(c, me, {"account": me, "category": category}, "user_rule")
    spawn_job(job)
    return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote(
        f"세부분류 '{name}'을 저장했습니다 · '{category}' 메일에 적용 중"), 303)


@app.post("/settings/subtype/{sid}/delete")
def user_subtype_delete(req: Request, sid: int, csrf: str = Form("")):
    check_csrf(req, csrf)
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    with db() as c:
        c.execute("DELETE FROM mail.user_subtype WHERE id=%s AND account=%s", (sid, me))
    return RedirectResponse("/settings/categories?msg=" + urllib.parse.quote("세부분류를 삭제했습니다"), 303)


@app.get("/api/job/{jid}")
def job_status(req: Request, jid: int):
    me = current(req)
    if not me:
        return _json_ascii({"error": "login"}, 401)
    with db() as c:
        r = c.execute("SELECT total, done, changed, status, requested_by FROM mail.reclass_job WHERE id=%s",
                      (jid,)).fetchone()
    if not r or (r[4] != me and me not in ADMINS):
        return _json_ascii({"error": "not found"}, 404)
    return _json_ascii({"total": r[0], "done": r[1], "changed": r[2], "status": r[3]})


# ---------------------------------------------------------------- 관리자: 일괄 재분류 · 기본 이름 · 규칙 제안
def _admin(req, csrf):
    check_csrf(req, csrf)
    me = current(req)
    if not me or me not in ADMINS:
        raise HTTPException(403)
    return me


def _reclass_filter(category, since, until, account, stale):
    return {k: v for k, v in dict(category=category or None, since=since or None, until=until or None,
                                  account=account or None, stale=(stale == "1") or None).items() if v}


@app.post("/admin/reclass/preview")
def admin_reclass_preview(req: Request, category: str = Form(""), since: str = Form(""), until: str = Form(""),
                          account: str = Form(""), stale: str = Form(""), csrf: str = Form("")):
    _admin(req, csrf)
    f = _reclass_filter(category, since, until, account, stale)
    with db() as c:
        current_rule_version(c)
        import classify as _K
        where, args = _K.job_where(f)
        n = c.execute("SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id WHERE " + where,
                      args).fetchone()[0]
        nm = c.execute("SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id WHERE "
                       + where.replace("coalesce(r.manual, false) = false", "r.manual"), args).fetchone()[0]
        llm = c.execute("SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id WHERE "
                        + where + " AND r.category NOT IN ('뉴스레터','시스템알림')", args).fetchone()[0]
    return _json_ascii({"count": n, "manual_protected": nm, "llm": llm,
                        "minutes": round((llm * 12 + (n - llm) * 0.2) / 60, 1)})


@app.post("/admin/reclass/run")
def admin_reclass_run(req: Request, category: str = Form(""), since: str = Form(""), until: str = Form(""),
                      account: str = Form(""), stale: str = Form(""), csrf: str = Form("")):
    me = _admin(req, csrf)
    f = _reclass_filter(category, since, until, account, stale)
    with db() as c:
        busy = c.execute("SELECT id FROM mail.reclass_job WHERE scope='admin' AND status IN ('queued','running')").fetchone()
        if busy:
            return RedirectResponse(f"/admin?msg=" + urllib.parse.quote(f"작업 #{busy[0]}이 진행 중입니다"), 303)
        job = new_job(c, me, f, "admin")
    spawn_job(job)
    audit(me, "reclass_batch", _json.dumps(f, ensure_ascii=False), client_ip(req))
    return RedirectResponse(f"/admin?msg=" + urllib.parse.quote(f"재분류 작업 #{job} 시작") + "#reclass", 303)


@app.post("/admin/reclass/{jid}/stop")
def admin_reclass_stop(req: Request, jid: int, csrf: str = Form("")):
    _admin(req, csrf)
    with db() as c:
        c.execute("UPDATE mail.reclass_job SET status='stop' WHERE id=%s AND status IN ('queued','running')", (jid,))
    return RedirectResponse("/admin#reclass", 303)


@app.post("/admin/categories")
async def admin_categories(req: Request):
    form = await req.form()
    _admin(req, form.get("csrf", ""))
    with db() as c:
        for code, label, color, sort in c.execute("SELECT code, label, color, sort FROM mail.category").fetchall():
            nl = (form.get(f"label_{code}") or "").strip()[:20] or label
            nc = form.get(f"color_{code}") or color
            nc = nc if re.fullmatch(r"#[0-9a-fA-F]{6}", nc) else color
            try:
                ns = int(form.get(f"sort_{code}") or sort)
            except ValueError:
                ns = sort
            c.execute("UPDATE mail.category SET label=%s, color=%s, sort=%s WHERE code=%s", (nl, nc, ns, code))
    return RedirectResponse("/admin?msg=" + urllib.parse.quote("회사 기본 분류 이름을 저장했습니다") + "#cats", 303)


@app.post("/admin/rule-extra/add")
def admin_rule_extra_add(req: Request, pattern: str = Form(...), category: str = Form(...), csrf: str = Form("")):
    me = _admin(req, csrf)
    pattern = pattern.strip().lower().lstrip("@")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", pattern):
        return RedirectResponse("/admin?msg=" + urllib.parse.quote("도메인 형식을 확인하세요") + "#rules", 303)
    with db() as c:
        c.execute("""INSERT INTO mail.rule_extra(kind, pattern, category, added_by) VALUES ('domain',%s,%s,%s)
                     ON CONFLICT (kind, pattern) DO UPDATE SET category=EXCLUDED.category""", (pattern, category, me))
    audit(me, "rule_extra_add", f"{pattern}→{category}", client_ip(req))
    return RedirectResponse("/admin?msg=" + urllib.parse.quote(
        f"공통 규칙 추가: {pattern} → {category} · 기존 메일 반영은 [일괄 재분류 › 규칙 변경분만]") + "#rules", 303)


@app.post("/admin/rule-extra/{rid}/delete")
def admin_rule_extra_delete(req: Request, rid: int, csrf: str = Form("")):
    _admin(req, csrf)
    with db() as c:
        c.execute("DELETE FROM mail.rule_extra WHERE id=%s", (rid,))
    return RedirectResponse("/admin#rules", 303)


# ================================================================ dashboard2 (리디자인 비교판: 사이드바 | 목록 | 읽기 창)
DAOU_MAIL_URL = os.getenv("DAOU_MAIL_URL", "https://gw.z-tec.co.kr/app/mail")
PAGE2 = 30
D2_VIEWS = {"all": "받은 메일", "action": "조치 필요", "deadline": "다가오는 마감", "event": "행사·회의"}


def _period_where(period, start, end):
    ps, pe, period = resolve_period(period, start, end)
    w, a = "", []
    if ps:
        w += " AND m.sent_at >= %s::date"
        a.append(ps)
    if pe:
        w += " AND m.sent_at < (%s::date + 1)"
        a.append(pe)
    return ps, pe, period, w, a


def _d2_sql(me, view, cat, sub, q, pw, pa):
    """dashboard2 목록 SQL → (sql, args, order). 마감·행사 보기는 기간과 무관하게 오늘 이후 전체"""
    sql = """SELECT m.id, m.sent_at, m.from_name, m.from_addr, m.subject, r.category, r.subtype, r.summary,
                    coalesce(r.action_needed,false), (SELECT count(*) FROM mail.attachment a WHERE a.message_id=m.id),
                    r.deadline, CASE WHEN r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                                THEN (r.extracted->>'event_date')::date END, r.customer, r.action
             FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id=m.id
             WHERE m.account=%s AND m.direction='in'"""
    args = [me]
    order = " ORDER BY m.sent_at DESC NULLS LAST, m.id DESC"
    if view == "deadline":
        sql += " AND r.deadline >= CURRENT_DATE"
        order = " ORDER BY r.deadline, m.sent_at DESC"
    elif view == "event":
        sql += " AND r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$' AND (r.extracted->>'event_date')::date >= CURRENT_DATE"
        order = " ORDER BY (r.extracted->>'event_date')::date, m.sent_at DESC"
    else:
        sql += pw
        args += pa
        if view == "action":
            sql += " AND r.action_needed"
    if cat == "미분류":
        sql += " AND r.category IS NULL"
    elif cat:
        sql += " AND r.category=%s"
        args.append(cat)
    if sub:
        sql += " AND r.subtype=%s"
        args.append(sub)
    if q:
        sql += " AND (m.subject ILIKE %s OR m.from_name ILIKE %s OR m.from_addr ILIKE %s OR r.summary ILIKE %s)"
        args += [f"%{q}%"] * 4
    return sql, args, order


def _d2_group(r, view, today):
    if view == "deadline":
        d = (r[10] - today).days
        return "이번 주" if d <= 7 else ("이번 달" if d <= 31 else "그 이후")
    if view == "event":
        d = (r[11] - today).days
        return "오늘" if d == 0 else ("이번 주" if d <= 7 else "그 이후")
    if not r[1]:
        return "날짜 없음"
    d = (today - r[1].astimezone(ZoneInfo("Asia/Seoul")).date()).days
    return "오늘" if d == 0 else "어제" if d == 1 else "이번 주" if d < 7 else "이번 달" if d < 31 else "이전"


def _d2_rows(me, view, cat, sub, q, period, start, end, offset):
    _, _, _, pw, pa = _period_where(period, start, end)
    sql, args, order = _d2_sql(me, view, cat, sub, q, pw, pa)
    with db() as c:
        rows = c.execute(sql + order + " LIMIT %s OFFSET %s", args + [PAGE2 + 1, max(offset, 0)]).fetchall()
    today = date.today()
    return [dict(r=r, g=_d2_group(r, view, today)) for r in rows[:PAGE2]], len(rows) > PAGE2


@app.get("/dashboard2")
def dashboard2(req: Request, view: str = "all", cat: str = "", sub: str = "", q: str = "",
               period: str = "", start: str = "", end: str = "", m: int = 0, welcome: int = 0):
    me = current(req)
    if not me:
        return RedirectResponse("/login", 303)
    view = view if view in D2_VIEWS else "all"
    ps, pe, period_key, pw, pa = _period_where(period, start, end)
    with db() as c:
        acct = c.execute("SELECT last_sync_at, last_error, display_name FROM mail.account WHERE account=%s",
                         (me,)).fetchone()
        stats = dict(c.execute(f"""SELECT coalesce(r.category,'미분류'), count(*) FROM mail.message m
                     LEFT JOIN mail.ai_result r ON r.message_id=m.id
                     WHERE m.account=%s AND m.direction='in' {pw} GROUP BY 1""", [me] + pa).fetchall())
        n_action = c.execute(f"""SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                     WHERE m.account=%s AND m.direction='in' AND r.action_needed {pw}""", [me] + pa).fetchone()[0]
        dl = c.execute("""SELECT count(*), min(r.deadline), count(*) FILTER (WHERE r.deadline <= CURRENT_DATE + 7)
                     FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                     WHERE m.account=%s AND r.deadline >= CURRENT_DATE""", (me,)).fetchone()
        n_event = c.execute("""SELECT count(*) FROM mail.message m JOIN mail.ai_result r ON r.message_id=m.id
                     WHERE m.account=%s AND r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                       AND (r.extracted->>'event_date')::date >= CURRENT_DATE""", (me,)).fetchone()[0]
        n_today = c.execute("""SELECT count(*) FROM mail.message WHERE account=%s AND direction='in'
                     AND sent_at >= CURRENT_DATE""", (me,)).fetchone()[0]
        subs = c.execute(f"""SELECT u.name, u.category, count(r.message_id) FROM mail.user_subtype u
                     LEFT JOIN (mail.ai_result r JOIN mail.message m ON m.id=r.message_id AND m.account=%s {pw})
                       ON r.subtype=u.name AND r.category=u.category
                     WHERE u.account=%s GROUP BY 1, 2 ORDER BY 2, 1""", [me] + pa + [me]).fetchall()
        alerts = c.execute("SELECT count(*) FROM mail.notify_log WHERE account=%s AND read_at IS NULL "
                           "AND title IS NOT NULL", (me,)).fetchone()[0]
        sql, args, _o = _d2_sql(me, view, cat, sub, q, pw, pa)
        cnt = c.execute(f"SELECT count(*) FROM ({sql}) x", args).fetchone()[0]
    rows, more = _d2_rows(me, view, cat, sub, q, period, start, end, 0)
    base = {"view": view if view != "all" else "", "cat": cat, "sub": sub, "q": q,
            "period": period_key if period_key != "custom" else "",
            "start": ps.isoformat() if (period_key == "custom" and ps) else "",
            "end": pe.isoformat() if (period_key == "custom" and pe) else ""}

    def qs(**kw):
        d = {**base, **kw}
        return "/dashboard2?" + urlencode({k: v for k, v in d.items() if v})

    title = (D2_VIEWS[view] if not (cat or sub) else (sub or None)) or \
        next((x["label"] for x in cat_info(me) if x["code"] == cat), cat or "미분류")
    sb = sidebar_ctx(me) or {}
    sb.update(view=view, cat=cat, sub=sub, total=sum(stats.values()), n_action=n_action, n_deadline=dl[0],
              n_event=n_event, alerts=alerts, stats=stats, subs=subs, acct=acct)
    return render(req, "dashboard2.html", sb=sb, acct=acct, stats=stats, total=sum(stats.values()), n_action=n_action,
                  dl=dl, n_event=n_event, n_today=n_today, subs=subs, alerts=alerts, rows=rows, more=more, cnt=cnt,
                  view=view, cat=cat, sub=sub, q=q, period=period_key, periods=PERIODS, ps=ps, pe=pe, qs=qs,
                  plabel=dict(PERIODS).get(period_key, "직접 지정"), title=title, sel=m, page=PAGE2, welcome=welcome,
                  more_qs=urlencode({k: v for k, v in base.items() if v}), views=D2_VIEWS)


@app.get("/dashboard2/rows")
def dashboard2_rows(req: Request, offset: int = 0, view: str = "all", cat: str = "", sub: str = "", q: str = "",
                    period: str = "", start: str = "", end: str = "", lg: str = ""):
    me = current(req)
    if not me:
        return Response(status_code=401)
    rows, more = _d2_rows(me, view if view in D2_VIEWS else "all", cat, sub, q, period, start, end, offset)
    resp = render(req, "_d2_rows.html", rows=rows, view=view, last_group=lg)
    resp.headers["X-Has-More"] = "1" if more else "0"
    return resp


@app.get("/dashboard2/read/{mid}")
def dashboard2_read(req: Request, mid: int):
    """읽기 창 (HTML 조각)"""
    me = current(req)
    if not me:
        return Response(status_code=401)
    with db() as c:
        m = c.execute("""SELECT m.id, m.sent_at, m.from_name, m.from_addr, m.to_addrs, m.cc_addrs, m.subject,
                         m.body_text, r.category, r.subtype, r.customer, r.summary, coalesce(r.action_needed,false),
                         r.action, r.deadline, CASE WHEN r.extracted->>'event_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                         THEN (r.extracted->>'event_date')::date END, r.sec_level, coalesce(m.has_html,false),
                         coalesce(r.manual,false), r.rule_hit
                         FROM mail.message m LEFT JOIN mail.ai_result r ON r.message_id=m.id
                         WHERE m.id=%s AND m.account=%s""", (mid, me)).fetchone()
        if not m:
            return Response("<div class='empty'>메일을 찾을 수 없습니다.</div>", media_type="text/html; charset=utf-8")
        atts = c.execute("SELECT id, filename, size_bytes, is_drm FROM mail.attachment WHERE message_id=%s ORDER BY id",
                         (mid,)).fetchall()
        _digest_table(c)
        dg = c.execute("SELECT content, model, created_at FROM mail.ai_digest WHERE message_id=%s", (mid,)).fetchone()
        ocr = c.execute("SELECT text, n_images FROM mail.ocr WHERE message_id=%s", (mid,)).fetchone() \
            if _has_ocr_table(c) else None
    digest = None
    if dg:
        try:
            digest = {**_json.loads(dg[0]), "_model": dg[1], "_at": dg[2]}
        except Exception:
            digest = None
    return render(req, "_d2_read.html", m=m, atts=atts, digest=digest, ocr=ocr, daou=DAOU_MAIL_URL,
                  dg_route=digest_route(m[8], m[16]))


# ---------------------------------------------------------------- 화면 배경 테마 (지텍 컬러 + 계절)
THEME_DIR = BASE / "themes"


@app.get("/theme/{name}.svg")
def theme_svg(name: str):
    if not re.fullmatch(r"[a-z0-9_-]{1,30}", name) or not (THEME_DIR / f"{name}.svg").exists():
        raise HTTPException(404)
    return Response((THEME_DIR / f"{name}.svg").read_bytes(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})
