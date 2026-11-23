# -*- coding: utf-8 -*-
"""
지텍 AI 메일 비서 - 알림기 (1분 주기): 사내 알림(웹 알림센터·PC 바탕화면) + 텔레그램

  python3 notifier.py --check     # 봇 토큰·DB 확인
  python3 notifier.py --init-db   # 알림 테이블 생성
  python3 notifier.py             # ① 봇 연결 요청 처리 ② 새 메일 규칙 알림 ③ 마감 리마인드

동작
  ① 사용자가 웹 앱 [알림] → [텔레그램 연결] → 봇에서 '시작'을 누르면 대화 ID를 계정에 연결
  ② AI 분류가 끝난 새 메일을 사용자별 규칙과 비교 → 일치하면 '알림'으로 기록 (같은 공고 7일 내 1회)
     · 사내 알림: 즉시 웹 앱 알림센터(🔔)에 표시, PC 알림 프로그램이 바탕화면 알림으로 표시
     · 텔레그램: 연결한 사용자만, 조용한 시간·주말에는 모아 두었다가 끝나면 묶어서 전송
  ③ 매일 08시 이후 첫 실행 때 마감 D-3 / D-1 리마인드
  - 텔레그램(외부)으로는 보안등급 L1 메일의 제목·할 일을 보내지 않음. 사내 알림에는 제목 포함
"""
import argparse
import html
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

PG_DSN = os.getenv("PG_DSN", "")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.getenv("APP_URL", "https://172.1.2.42:8443").rstrip("/")
DAOU_URL = os.getenv("DAOU_MAIL_URL", "https://gw.z-tec.co.kr/app/mail")   # 다우오피스 웹 메일 (외부 접속 가능)
KST = ZoneInfo("Asia/Seoul")
LOOKBACK_HOURS = 72          # 조용한 시간·주말 동안 쌓인 메일을 놓치지 않도록
MAX_TRY = 5                  # 텔레그램 전송 실패 재시도 한도
DIGEST_OVER = 4              # 한 번에 이 건수를 넘으면 묶음 메시지 1건으로
DUP_DAYS = 7                 # 같은 제목(Re/Fwd 제거) 재알림 금지 기간

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("notifier")

DEFAULT_RULES = [
    dict(name="조치 필요 메일", action_only=True),
    dict(name="정부과제 공고·수요조사", category="정부과제", subtype_contains="공고,수요조사"),
]


# ---------------------------------------------------------------- 공통
def db():
    import psycopg
    if not PG_DSN:
        sys.exit("[설정 오류] PG_DSN 없음 (~/mail-collector/.env)")
    conn = psycopg.connect(PG_DSN)
    conn.execute("SET TIME ZONE 'Asia/Seoul'")
    return conn


def tg(method, **params):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 이 ~/mail-collector/.env 에 없습니다")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=40) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(out.get("description", "telegram error"))
    return out["result"]


def send(chat_id, text):
    return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
              disable_web_page_preview="true")


def esc(s):
    return html.escape(str(s or ""), quote=False)


def subject_key(s):
    return re.sub(r"^((re|fw|fwd)\s*:\s*)+", "", (s or "").strip(), flags=re.I).lower()


def is_quiet(st, now=None):
    now = now or datetime.now(KST)
    if st["weekend_quiet"] and now.weekday() >= 5:
        return True
    qs, qe, h = st["quiet_start"], st["quiet_end"], now.hour
    if qs == qe:
        return False
    return (h >= qs or h < qe) if qs > qe else (qs <= h < qe)


def kv_get(conn, k, default=None):
    r = conn.execute("SELECT value FROM mail.kv WHERE key=%s", (k,)).fetchone()
    return r[0] if r else default


def kv_set(conn, k, v):
    conn.execute("INSERT INTO mail.kv(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                 (k, str(v)))


# ---------------------------------------------------------------- ① 봇 연결
def handle_updates(conn):
    offset = int(kv_get(conn, "tg_offset", "0"))
    try:
        updates = tg("getUpdates", offset=offset, timeout=0, allowed_updates='["message"]')
    except Exception as e:
        log.warning("getUpdates 실패: %s", e)
        return
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        cid = chat.get("id")
        if not cid or chat.get("type") != "private":
            continue
        name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username")
        m = re.match(r"^/start\s+([A-Za-z0-9_-]{6,})$", text)
        try:
            if m:
                r = conn.execute("""UPDATE mail.notify_setting SET tg_chat_id=%s, tg_name=%s, tg_linked_at=now(),
                                    link_code=NULL, enabled=true
                                    WHERE link_code=%s AND link_code_at > now() - interval '15 minutes'
                                    RETURNING account""", (cid, name, m.group(1))).fetchone()
                if r:
                    acct = r[0]
                    if not conn.execute("SELECT 1 FROM mail.notify_rule WHERE account=%s LIMIT 1", (acct,)).fetchone():
                        for d in DEFAULT_RULES:
                            conn.execute("""INSERT INTO mail.notify_rule(account,name,category,subtype_contains,action_only)
                                            VALUES (%s,%s,%s,%s,%s)""",
                                         (acct, d["name"], d.get("category"), d.get("subtype_contains"),
                                          d.get("action_only", False)))
                    send(cid, f"✅ <b>지텍 AI 메일 비서</b> 연결 완료\n계정: {esc(acct)}\n\n"
                              f"알림 규칙은 웹 앱 [알림 규칙·연결]에서 바꿀 수 있습니다.\n연결 해제: /stop")
                    log.info("텔레그램 연결: %s ← chat %s", acct, cid)
                else:
                    have = conn.execute("SELECT account FROM mail.notify_setting WHERE tg_chat_id=%s", (cid,)).fetchone()
                    if have:              # 이미 연결된 상태에서 [시작]을 한 번 더 누른 경우
                        send(cid, f"✅ 이미 연결되어 있습니다.\n계정: {esc(have[0])}\n\n연결 해제: /stop")
                    else:
                        send(cid, "⚠️ 연결 코드가 만료되었거나 이미 사용되었습니다. (코드는 15분 동안 1번만 사용 가능)\n\n"
                                  "1. 웹 앱 [알림 규칙·연결] 화면을 엽니다\n"
                                  "2. [텔레그램 연결 시작] 또는 [새 QR 받기]를 누릅니다\n"
                                  "3. 새로 나온 QR을 휴대폰 카메라로 찍고 [시작]을 누릅니다")
            elif text == "/stop":
                n = conn.execute("UPDATE mail.notify_setting SET tg_chat_id=NULL, tg_linked_at=NULL "
                                 "WHERE tg_chat_id=%s", (cid,)).rowcount
                send(cid, "🔕 알림 연결을 해제했습니다." if n else "연결된 계정이 없습니다.")
            else:
                send(cid, "지텍 AI 메일 비서 알림 봇입니다.\n연결은 웹 앱 [알림 규칙·연결]에서 QR로 시작하세요.\n연결 해제: /stop")
        except Exception as e:
            log.warning("update 처리 실패: %s", e)
        conn.commit()
    kv_set(conn, "tg_offset", offset)
    conn.commit()


# ---------------------------------------------------------------- ② 규칙 매칭
def split(s):
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def match(rule, m):
    if rule["from_contains"]:
        hay = f"{m['from_addr']} {m['from_name']}".lower()
        if not any(k in hay for k in split(rule["from_contains"])):
            return False
    if rule["subject_contains"]:
        if not any(k in (m["subject"] or "").lower() for k in split(rule["subject_contains"])):
            return False
    if rule["category"] and rule["category"] != m["category"]:
        return False
    if rule["subtype_contains"]:
        if not any(k in (m["subtype"] or "").lower() for k in split(rule["subtype_contains"])):
            return False
    if rule["action_only"] and not m["action_needed"]:
        return False
    if rule["deadline_days"] is not None:
        if not m["deadline"] or (m["deadline"] - date.today()).days > rule["deadline_days"]:
            return False
    return True


def fmt_one(m, level, rule_name=None, remind=None):
    lv = "minimal" if m["sec_level"] == "L1" else level
    cat = L(m["category"]) + (f"·{m['subtype']}" if m["subtype"] and m["subtype"] != m["category"] else "")
    head = f"⏰ <b>마감 D-{remind}</b>" if remind is not None else "📬 <b>새 메일</b>"
    lines = [f"{head}  [{esc(cat)}]", f"보낸 곳: {esc(m['customer'] or m['from_name'] or m['from_addr'])}"]
    if lv == "full":
        lines.append(f"제목: {esc(m['subject'])}")
    if lv in ("summary", "full") and m["action_needed"] and m["action"]:
        lines.append(f"할 일: {esc(m['action'])}")
    if m["deadline"]:
        d = (m["deadline"] - date.today()).days
        lines.append(f"마감: {m['deadline']} (D-{d})" if d > 0 else f"마감: {m['deadline']} (오늘)")
    if rule_name and remind is None:
        lines.append(f"<i>규칙: {esc(rule_name)}</i>")
    lines.append(f'🔗 <a href="{DAOU_URL}">메일 열기</a> (DAOU OFFICE)')
    return "\n".join(lines)


def fmt_digest(items, title):
    lines = [f"{title} <b>{len(items)}건</b>"]
    for m, _ in items[:15]:
        org = m["customer"] or m["from_name"] or m["from_addr"]
        dl = f" · 마감 {m['deadline']:%m-%d}" if m["deadline"] else ""
        lines.append(f"• [{esc(L(m['category']))}] {esc(org)}{dl}")
    if len(items) > 15:
        lines.append(f"… 외 {len(items) - 15}건")
    lines.append(f'🔗 <a href="{DAOU_URL}">메일 열기</a> (DAOU OFFICE)')
    return "\n".join(lines)


COLS = ["id", "from_addr", "from_name", "subject", "category", "subtype", "customer", "action_needed", "action",
        "deadline", "sec_level"]
SEL = """SELECT m.id, m.from_addr, m.from_name, m.subject, r.category, r.subtype, r.customer, r.action_needed,
                r.action, r.deadline, r.sec_level
         FROM mail.message m JOIN mail.ai_result r ON r.message_id = m.id"""


def ensure_settings(conn):
    """동의한 모든 사용자에게 알림 설정 + 기본 규칙 생성 (최초 1회) → 텔레그램 없이도 사내 알림 동작"""
    rows = conn.execute("""INSERT INTO mail.notify_setting(account)
                           SELECT a.account FROM mail.account a WHERE a.consent_at IS NOT NULL
                           ON CONFLICT DO NOTHING RETURNING account""").fetchall()
    for (acct,) in rows:
        if not conn.execute("SELECT 1 FROM mail.notify_rule WHERE account=%s LIMIT 1", (acct,)).fetchone():
            for d in DEFAULT_RULES:
                conn.execute("""INSERT INTO mail.notify_rule(account,name,category,subtype_contains,action_only)
                                VALUES (%s,%s,%s,%s,%s)""",
                             (acct, d["name"], d.get("category"), d.get("subtype_contains"), d.get("action_only", False)))
        log.info("[%s] 알림 설정·기본 규칙 생성", acct)
    conn.commit()


def settings(conn):
    cols = ["account", "tg_chat_id", "tg_linked_at", "enabled", "content_level", "quiet_start", "quiet_end",
            "weekend_quiet", "remind_days", "created_at"]
    rows = conn.execute(f"SELECT {','.join(cols)} FROM mail.notify_setting s WHERE enabled "
                        "AND EXISTS (SELECT 1 FROM mail.account a WHERE a.account=s.account AND a.consent_at IS NOT NULL)"
                        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def internal_text(m, remind=None):
    """사내 알림(웹·PC)용 제목/본문 - 사내망 안에서만 표시되므로 메일 제목 포함"""
    cat = L(m["category"]) + (f"·{m['subtype']}" if m["subtype"] and m["subtype"] != m["category"] else "")
    org = m["customer"] or m["from_name"] or m["from_addr"]
    title = (f"⏰ 마감 D-{remind} · " if remind is not None else "📬 ") + f"[{cat}] {org}"
    body = [m["subject"] or ""]
    if m["action_needed"] and m["action"]:
        body.append(f"할 일: {m['action']}")
    if m["deadline"]:
        body.append(f"마감: {m['deadline']}")
    return title, "\n".join(body)


def record(conn, st, m, kind, rule, skey, suppressed=False):
    """알림 결정 기록 = 사내 알림 생성. 텔레그램은 연결된 경우 'pending'으로 큐잉"""
    remind = int(kind.split("-")[1]) if kind.startswith("remind") else None
    title, body = internal_text(m, remind)
    if suppressed:
        conn.execute("""INSERT INTO mail.notify_log(account,message_id,kind,rule_name,subject_key,ok,error,title,body,
                        tg,read_at,pc_at) VALUES (%s,%s,%s,%s,%s,true,'중복 생략',%s,%s,'skip',now(),now())
                        ON CONFLICT DO NOTHING""", (st["account"], m["id"], kind, rule, skey, title, body))
        return False
    conn.execute("""INSERT INTO mail.notify_log(account,message_id,kind,rule_name,subject_key,ok,title,body,tg)
                    VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,%s) ON CONFLICT DO NOTHING""",
                 (st["account"], m["id"], kind, rule, skey, title, body,
                  "pending" if st["tg_chat_id"] else "none"))
    return True


def decide_new(conn, st):
    floor = datetime.now(KST) - timedelta(hours=LOOKBACK_HOURS)
    since = max(st["created_at"] or floor, floor)
    rows = conn.execute(SEL + """ WHERE m.account=%s AND m.direction='in' AND m.collected_at >= %s
                 AND NOT EXISTS (SELECT 1 FROM mail.notify_log l WHERE l.account=m.account AND l.message_id=m.id
                                 AND l.kind IN ('new','dup'))
                 ORDER BY m.sent_at""", (st["account"], since)).fetchall()
    if not rows:
        return 0
    rules = [dict(zip(["name", "from_contains", "subject_contains", "category", "subtype_contains", "action_only",
                       "deadline_days"], r)) for r in conn.execute(
        """SELECT name, from_contains, subject_contains, category, subtype_contains, action_only, deadline_days
           FROM mail.notify_rule WHERE account=%s AND enabled ORDER BY id""", (st["account"],)).fetchall()]
    n, seen = 0, set()
    for r in rows:
        m = dict(zip(COLS, r))
        hit = next((ru["name"] for ru in rules if match(ru, m)), None)
        if not hit:
            continue
        sk = subject_key(m["subject"])
        recent = conn.execute("""SELECT 1 FROM mail.notify_log WHERE account=%s AND subject_key=%s AND kind='new'
                                 AND sent_at > now() - make_interval(days => %s) LIMIT 1""",
                              (st["account"], sk, DUP_DAYS)).fetchone()
        if recent or sk in seen:            # 같은 공고 재발송 → 알리지 않음
            conn.execute("""INSERT INTO mail.notify_log(account,message_id,kind,rule_name,subject_key,ok,tg,read_at,pc_at)
                            VALUES (%s,%s,'dup',%s,%s,true,'skip',now(),now()) ON CONFLICT DO NOTHING""",
                         (st["account"], m["id"], hit, sk))
            continue
        seen.add(sk)
        n += record(conn, st, m, "new", hit, sk)
    conn.commit()
    return n


def decide_remind(conn, st):
    if datetime.now(KST).hour < 8:
        return 0
    n = 0
    for d in sorted({int(x) for x in re.findall(r"\d+", st["remind_days"] or "")}, reverse=True):
        kind = f"remind-{d}"
        rows = conn.execute(SEL + """ WHERE m.account=%s AND r.deadline = CURRENT_DATE + %s
                     AND r.category NOT IN ('뉴스레터', '시스템알림')
                     AND NOT EXISTS (SELECT 1 FROM mail.notify_log l WHERE l.account=m.account
                                     AND l.message_id=m.id AND l.kind=%s)
                     ORDER BY m.sent_at DESC""", (st["account"], d, kind)).fetchall()
        seen = set()
        for r in rows:
            m = dict(zip(COLS, r))
            sk = subject_key(m["subject"])
            today_new = conn.execute("""SELECT 1 FROM mail.notify_log WHERE account=%s AND subject_key=%s
                                        AND kind IN ('new','dup') AND sent_at::date = CURRENT_DATE LIMIT 1""",
                                     (st["account"], sk)).fetchone()
            sup = bool(sk in seen or today_new)   # 같은 공고 중복 또는 오늘 이미 '새 메일'로 알림
            seen.add(sk)
            n += record(conn, st, m, kind, None, sk, suppressed=sup)
    conn.commit()
    return n


def send_tg(conn, st):
    """텔레그램 큐(pending) 전송 - 조용한 시간에는 보류, 24시간 지난 보류분은 만료"""
    if not st["tg_chat_id"]:
        return 0
    conn.execute("""UPDATE mail.notify_log SET tg='expired' WHERE account=%s AND tg='pending'
                    AND sent_at < now() - interval '24 hours'""", (st["account"],))
    conn.commit()
    if is_quiet(st):
        return 0
    rows = conn.execute("""SELECT l.id, l.kind, l.rule_name, """ + SEL.split("SELECT", 1)[1].split("FROM")[0] +
                        """ FROM mail.notify_log l JOIN mail.message m ON m.id=l.message_id
                            JOIN mail.ai_result r ON r.message_id=m.id
                            WHERE l.account=%s AND l.tg='pending' ORDER BY l.id""", (st["account"],)).fetchall()
    groups = {}
    for r in rows:
        lid, kind, rule = r[0], r[1], r[2]
        groups.setdefault(kind, []).append((lid, dict(zip(COLS, r[3:])), rule))
    total = 0

    def ok(ids):
        conn.execute("UPDATE mail.notify_log SET tg='sent', ok=true, tg_at=now(), tg_error=NULL "
                     "WHERE id = ANY(%s)", (ids,))
        conn.commit()

    def fail(ids, e):
        """실패 사유·횟수 기록. 연결 끊김이면 False(중단), 아니면 True(다른 알림 계속)"""
        err = str(e)[:300]
        log.warning("[%s] 텔레그램 전송 실패: %s", st["account"], err)
        if "chat not found" in err or "blocked" in err:
            conn.execute("UPDATE mail.notify_setting SET tg_chat_id=NULL WHERE account=%s", (st["account"],))
            conn.execute("UPDATE mail.notify_log SET tg='none', tg_error=%s WHERE account=%s AND tg='pending'",
                         ("텔레그램 연결 끊김 (봇 차단·대화 삭제)", st["account"]))
            conn.commit()
            return False
        conn.execute("""UPDATE mail.notify_log SET tg_error=%s, tg_try=tg_try+1,
                        tg = CASE WHEN tg_try+1 >= %s THEN 'fail' ELSE tg END WHERE id = ANY(%s)""",
                     (err, MAX_TRY, ids))
        conn.commit()
        return True                        # 실패분은 1분 뒤 재시도 (5회까지)

    for kind, items in sorted(groups.items(), key=lambda kv: kv[0] != "new"):
        remind = int(kind.split("-")[1]) if kind.startswith("remind") else None
        if len(items) > DIGEST_OVER:
            title = "📬 새 알림 메일" if remind is None else f"⏰ 마감 D-{remind}"
            ids = [i for i, _, _ in items]
            try:
                send(st["tg_chat_id"], fmt_digest([(m, rule) for _, m, rule in items], title))
            except Exception as e:
                if not fail(ids, e):
                    return total
                continue
            ok(ids)
            total += len(items)
        else:
            for lid, m, rule in items:     # 한 건씩 → 한 건 실패가 다른 알림을 막지 않음
                try:
                    send(st["tg_chat_id"], fmt_one(m, st["content_level"], rule, remind))
                except Exception as e:
                    if not fail([lid], e):
                        return total
                    continue
                ok([lid])
                total += 1
    return total


LABELS = {"acct": None, "map": {}}


def L(code):
    """분류 내부 코드 → 사용자 화면 이름 (개인 이름 > 회사 기본 이름)"""
    return LABELS["map"].get(code, code or "미분류")


def load_labels(conn, account):
    LABELS["acct"] = account
    try:
        LABELS["map"] = dict(conn.execute("""SELECT k.code, coalesce(u.label, k.label) FROM mail.category k
                     LEFT JOIN mail.category_user u ON u.code=k.code AND u.account=%s""", (account,)).fetchall())
    except Exception:
        conn.rollback()
        LABELS["map"] = {}


def run():
    with db() as conn:
        if TOKEN:
            handle_updates(conn)
        ensure_settings(conn)
        for st in settings(conn):
            load_labels(conn, st["account"])
            n1 = decide_new(conn, st)
            n2 = decide_remind(conn, st)
            n3 = send_tg(conn, st) if TOKEN else 0
            if n1 or n2 or n3:
                log.info("[%s] 새 알림 %d · 리마인드 %d · 텔레그램 전송 %d", st["account"], n1, n2, n3)


def check():
    if TOKEN:
        me = tg("getMe")
        log.info("봇 OK  @%s (%s)", me["username"], me["first_name"])
    else:
        log.info("텔레그램 봇 토큰 없음 → 사내 알림만 동작")
    with db() as conn:
        n = conn.execute("SELECT count(*) FROM mail.notify_setting WHERE tg_chat_id IS NOT NULL").fetchone()[0]
        log.info("DB OK  텔레그램 연결 사용자 %d명", n)


def init_db():
    with db() as conn:
        conn.execute((BASE / "schema_notify.sql").read_text(encoding="utf-8"))
    log.info("알림 스키마 생성 완료")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="지텍 메일 텔레그램 알림기")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--init-db", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.init_db:
        init_db()
    else:
        run()
