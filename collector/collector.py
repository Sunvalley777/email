# -*- coding: utf-8 -*-
"""
지텍 AI 메일 관리 시스템 P0 - IMAP 수집기 (읽기 전용)

  python3 collector.py --init-db      # 스키마 생성 (최초 1회)
  python3 collector.py --check        # IMAP·DB 접속 확인
  python3 collector.py                # 신규 메일 수집 (systemd 타이머가 5분마다 실행)
  python3 collector.py --limit 50     # 폴더당 최대 50건만 (초기 시험용)
  python3 collector.py --stats        # 적재 현황

특징
- 폴더는 EXAMINE(읽기 전용)으로 열고 BODY.PEEK[]로 가져오므로 '읽음' 표시가 바뀌지 않음
- UIDVALIDITY + 마지막 UID 기준 증분 수집, 중복 적재 방지
- 원본 .eml과 첨부파일은 디스크(DATA_DIR)에, 메타데이터는 DB(mail 스키마)에 저장
"""
import argparse
import email
import hashlib
import html
import imaplib
import logging
import os
import re
import ssl
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime, parseaddr
from pathlib import Path

BASE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 설정
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

IMAP_HOST = os.getenv("IMAP_HOST", "gw.z-tec.co.kr")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASS = os.getenv("IMAP_PASS", "")
FOLDERS = [f.strip() for f in os.getenv("IMAP_FOLDERS", "Inbox,Sent").split(",") if f.strip()]
BATCH = int(os.getenv("FETCH_BATCH", "20"))
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE / "data")))
PG_DSN = os.getenv("PG_DSN", "")  # 예: host=127.0.0.1 port=5435 dbname=labrag user=... password=...
DRM_EXT = {".drm", ".nxl", ".dcr"}  # DRM 의심 확장자 (DocuRay 등 운영 환경에 맞게 조정)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("collector")


# ---------------------------------------------------------------- 한글 디코딩
CHARSET_ALIAS = {
    "ks_c_5601-1987": "cp949", "ks_c_5601": "cp949", "euc-kr": "cp949",
    "x-windows-949": "cp949", "unknown-8bit": "cp949", "": "utf-8",
}


def norm_charset(cs):
    cs = (cs or "").lower()
    return CHARSET_ALIAS.get(cs, cs)


def dec_header(value):
    if not value:
        return ""
    out = []
    try:
        for part, cs in decode_header(str(value)):
            if isinstance(part, bytes):
                for c in (norm_charset(cs), "utf-8", "cp949"):
                    try:
                        out.append(part.decode(c))
                        break
                    except (LookupError, UnicodeDecodeError):
                        continue
                else:
                    out.append(part.decode("utf-8", "replace"))
            else:
                out.append(part)
        return re.sub(r"\s+", " ", "".join(out)).strip()
    except Exception:
        try:
            return str(make_header(decode_header(str(value))))
        except Exception:
            return str(value)


def dec_payload(part):
    raw = part.get_payload(decode=True) or b""
    for c in (norm_charset(part.get_content_charset()), "utf-8", "cp949"):
        try:
            return raw.decode(c)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def addr_list(values):
    return [a.lower() for _, a in getaddresses([dec_header(v) for v in values if v]) if a]


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "noname").strip() or "noname"
    return name[:150]


# ---------------------------------------------------------------- 파싱
def parse_message(raw: bytes):
    msg = email.message_from_bytes(raw)
    from_name, from_addr = parseaddr(dec_header(msg.get("From")))
    refs = re.findall(r"<[^>]+>", msg.get("References", "") or "")
    msgid = (msg.get("Message-ID") or "").strip() or None
    in_reply = (msg.get("In-Reply-To") or "").strip() or None
    thread_key = refs[0] if refs else (in_reply or msgid)

    sent_at = None
    try:
        sent_at = parsedate_to_datetime(msg.get("Date"))
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    text_parts, html_parts, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        fname = part.get_filename()
        if fname or "attachment" in disp:
            data = part.get_payload(decode=True) or b""
            atts.append({
                "filename": dec_header(fname) if fname else "noname",
                "content_type": ctype,
                "data": data,
            })
        elif ctype == "text/plain":
            text_parts.append(dec_payload(part))
        elif ctype == "text/html":
            html_parts.append(dec_payload(part))

    if text_parts:
        body = "\n".join(text_parts).strip()
    else:
        body = html_to_text("\n".join(html_parts))

    return {
        "message_id": msgid,
        "in_reply_to": in_reply,
        "references_ids": refs,
        "thread_key": thread_key,
        "subject": dec_header(msg.get("Subject")),
        "from_name": from_name,
        "from_addr": (from_addr or "").lower(),
        "to_addrs": addr_list(msg.get_all("To", [])),
        "cc_addrs": addr_list(msg.get_all("Cc", [])),
        "sent_at": sent_at,
        "body_text": body,
        "has_html": bool(html_parts),
        "attachments": atts,
    }


# ---------------------------------------------------------------- DB
def db():
    import psycopg  # psycopg3
    if not PG_DSN or "여기에" in PG_DSN:
        sys.exit("[설정 오류] .env의 PG_DSN이 비어 있거나 템플릿 값('여기에_DB비밀번호') 그대로입니다.")
    return psycopg.connect(PG_DSN)


def init_db():
    sql = (BASE / "schema.sql").read_text(encoding="utf-8")
    with db() as conn:
        conn.execute(sql)
    log.info("스키마 생성 완료 (mail.*)")


def get_state(conn, folder):
    r = conn.execute(
        "SELECT uidvalidity, last_uid FROM mail.sync_state WHERE account=%s AND folder=%s",
        (IMAP_USER, folder)).fetchone()
    return r if r else (None, 0)


def set_state(conn, folder, uidvalidity, last_uid, count, err=None):
    conn.execute("""
        INSERT INTO mail.sync_state(account, folder, uidvalidity, last_uid, last_sync_at, last_count, last_error)
        VALUES (%s,%s,%s,%s,now(),%s,%s)
        ON CONFLICT (account, folder) DO UPDATE SET
          uidvalidity=EXCLUDED.uidvalidity, last_uid=EXCLUDED.last_uid,
          last_sync_at=now(), last_count=EXCLUDED.last_count, last_error=EXCLUDED.last_error
    """, (IMAP_USER, folder, uidvalidity, last_uid, count, err))


def blob_path(acct_dir, sha):
    """첨부 저장 위치: 내용(SHA-256) 기준 1회 저장 → 같은 파일이 여러 메일에 와도 디스크에는 1개"""
    return acct_dir / "blobs" / sha[:2] / sha


def put_blob(acct_dir, data):
    sha = hashlib.sha256(data).hexdigest()
    path = blob_path(acct_dir, sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return sha, path


def strip_eml(raw):
    """원본 .eml에서 첨부 내용만 빼고 저장 (헤더·본문은 그대로, 첨부 자리에는 SHA-256 참조)
    → 첨부가 .eml과 첨부 폴더에 두 번 저장되는 것을 방지. 실패하면 원본 그대로 반환"""
    try:
        msg = email.message_from_bytes(raw)
        changed = False
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = (part.get("Content-Disposition") or "").lower()
            if part.get_filename() or "attachment" in disp:
                data = part.get_payload(decode=True) or b""
                part["X-Ztec-Blob-SHA256"] = hashlib.sha256(data).hexdigest()
                part["X-Ztec-Blob-Size"] = str(len(data))
                part.set_payload("")
                changed = True
        return msg.as_bytes() if changed else raw
    except Exception as e:
        log.warning("첨부 분리 실패 → 원본 저장: %s", e)
        return raw


def save(conn, folder, uidvalidity, uid, raw, p):
    acct_dir = DATA_DIR / safe_name(IMAP_USER)
    raw_dir = acct_dir / "raw" / folder / str(uidvalidity)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{uid}.eml"
    raw_path.write_bytes(strip_eml(raw) if p["attachments"] else raw)

    direction = "out" if folder.lower() == "sent" or p["from_addr"] == IMAP_USER.lower() else "in"
    row = conn.execute("""
        INSERT INTO mail.message(account, folder, uidvalidity, uid, message_id, in_reply_to, references_ids,
          thread_key, subject, from_name, from_addr, to_addrs, cc_addrs, sent_at, body_text, has_html,
          size_bytes, raw_path, direction)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (account, folder, uidvalidity, uid) DO NOTHING
        RETURNING id
    """, (IMAP_USER, folder, uidvalidity, uid, p["message_id"], p["in_reply_to"], p["references_ids"],
          p["thread_key"], p["subject"], p["from_name"], p["from_addr"], p["to_addrs"], p["cc_addrs"],
          p["sent_at"], p["body_text"].replace("\x00", ""), p["has_html"], len(raw), str(raw_path),
          direction)).fetchone()
    if not row:
        return False
    mid = row[0]
    for a in p["attachments"]:
        data = a["data"]
        sha, path = put_blob(acct_dir, data)
        fname = safe_name(a["filename"])
        conn.execute("""
            INSERT INTO mail.attachment(message_id, filename, content_type, size_bytes, sha256, stored_path, is_drm)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (mid, a["filename"], a["content_type"], len(data), sha, str(path),
              Path(fname).suffix.lower() in DRM_EXT))
    return True


def dedupe():
    """기존 저장분 정리: 첨부 → blobs/ 로 1회 저장, .eml 에서 첨부 내용 제거, 빈 att/ 폴더 삭제"""
    def size_of(d):
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0
    before = size_of(DATA_DIR)
    moved = dup = 0
    with db() as conn:
        rows = conn.execute("""SELECT a.id, a.sha256, a.stored_path, m.account FROM mail.attachment a
                               JOIN mail.message m ON m.id = a.message_id""").fetchall()
        for aid, sha, path, acct in rows:
            src = Path(path)
            acct_dir = DATA_DIR / safe_name(acct)
            if "/blobs/" in str(src):
                continue
            dst = blob_path(acct_dir, sha)
            if dst.exists():
                if src.exists():
                    src.unlink()
                dup += 1
            elif src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
                moved += 1
            else:
                log.warning("첨부 파일 없음: %s", src)
                continue
            conn.execute("UPDATE mail.attachment SET stored_path=%s WHERE id=%s", (str(dst), aid))
        conn.commit()
        n_eml = 0
        for (raw_path,) in conn.execute("""SELECT DISTINCT m.raw_path FROM mail.message m
                                           JOIN mail.attachment a ON a.message_id = m.id""").fetchall():
            f = Path(raw_path)
            if f.exists():
                raw = f.read_bytes()
                if b"X-Ztec-Blob-SHA256" not in raw:
                    stripped = strip_eml(raw)
                    if len(stripped) < len(raw):
                        f.write_bytes(stripped)
                        n_eml += 1
    for d in sorted(DATA_DIR.glob("*/att/**/*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    for d in DATA_DIR.glob("*/att"):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    after = size_of(DATA_DIR)
    log.info("첨부 이동 %d건, 중복 제거 %d건, .eml 첨부 분리 %d건", moved, dup, n_eml)
    log.info("디스크 사용량 %.1f MB → %.1f MB (%.1f MB 절감)", before / 1e6, after / 1e6, (before - after) / 1e6)


# ---------------------------------------------------------------- IMAP
def imap_connect():
    if not (IMAP_USER and IMAP_PASS) or "여기에" in IMAP_PASS:
        raise RuntimeError("[설정 오류] IMAP 비밀번호가 비어 있거나 템플릿 값('여기에_비밀번호') 그대로입니다.")
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=60)
    if IMAP_PASS.isascii():
        m.login(IMAP_USER, IMAP_PASS)
    else:  # 한글 등 비ASCII 비밀번호 → AUTH=PLAIN(UTF-8) 사용
        m.authenticate("PLAIN", lambda _: f"\0{IMAP_USER}\0{IMAP_PASS}".encode("utf-8"))
    return m


def examine(m, folder):
    typ, data = m.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"폴더 열기 실패: {folder} {data}")
    uv = m.untagged_responses.get("UIDVALIDITY", [b"0"])[0]
    return int(uv), int(data[0])


def sync_folder(m, conn, folder, limit=None):
    uidvalidity, total = examine(m, folder)
    old_uv, last_uid = get_state(conn, folder)
    if old_uv is not None and old_uv != uidvalidity:
        log.warning("[%s] UIDVALIDITY 변경(%s→%s): 처음부터 재수집", folder, old_uv, uidvalidity)
        last_uid = 0

    typ, data = m.uid("SEARCH", None, f"UID {last_uid + 1}:*")
    uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)
    if limit:
        uids = uids[:limit]
    log.info("[%s] 전체 %d건, 신규 %d건 (last_uid=%d)", folder, total, len(uids), last_uid)

    saved = 0
    for i in range(0, len(uids), BATCH):
        chunk = uids[i:i + BATCH]
        typ, resp = m.uid("FETCH", ",".join(map(str, chunk)), "(UID BODY.PEEK[])")
        for item in resp:
            if not isinstance(item, tuple):
                continue
            mu = re.search(rb"UID (\d+)", item[0])
            if not mu:
                continue
            uid, raw = int(mu.group(1)), item[1]
            try:
                p = parse_message(raw)
                if save(conn, folder, uidvalidity, uid, raw, p):
                    saved += 1
            except Exception as e:
                log.exception("[%s] UID %d 처리 실패: %s", folder, uid, e)
        last_uid = max(last_uid, chunk[-1])
        set_state(conn, folder, uidvalidity, last_uid, saved)
        conn.commit()
        log.info("[%s] 진행 %d/%d", folder, min(i + BATCH, len(uids)), len(uids))

    set_state(conn, folder, uidvalidity, last_uid, saved)
    conn.commit()
    return saved


def run_one(limit=None):
    m = imap_connect()
    try:
        with db() as conn:
            for folder in FOLDERS:
                try:
                    n = sync_folder(m, conn, folder, limit)
                    log.info("[%s][%s] 신규 저장 %d건", IMAP_USER, folder, n)
                except Exception as e:
                    conn.rollback()
                    log.exception("[%s][%s] 동기화 실패", IMAP_USER, folder)
                    old_uv, last = get_state(conn, folder)
                    set_state(conn, folder, old_uv or 0, last, 0, str(e)[:500])
                    conn.commit()
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- 다중 계정 (웹 앱 등록 계정)
ENV_USER, ENV_PASS = IMAP_USER, IMAP_PASS
FERNET_KEY = os.getenv("FERNET_KEY", "")


def accounts():
    """웹 앱에서 동의·등록한 계정 + (.env에 설정된 기존 1인 계정)"""
    out = []
    try:
        with db() as conn:
            rows = conn.execute("SELECT account, enc_pass FROM mail.account "
                                "WHERE enabled AND enc_pass IS NOT NULL ORDER BY account").fetchall()
    except Exception as e:  # mail.account 테이블이 아직 없음 (웹 앱 미설치)
        log.debug("mail.account 조회 불가: %s", e)
        rows = []
    if rows:
        from cryptography.fernet import Fernet
        f = Fernet(FERNET_KEY.encode())
        for acct, enc in rows:
            try:
                out.append((acct, f.decrypt(enc.encode()).decode("utf-8")))
            except Exception:
                log.error("[%s] 비밀번호 복호화 실패 (FERNET_KEY 확인)", acct)
    names = {a for a, _ in out}
    if ENV_USER and ENV_PASS and "여기에" not in ENV_PASS and ENV_USER not in names:
        out.append((ENV_USER, ENV_PASS))
    return out


def mark_account(acct, ok, err=None, disable=False):
    try:
        with db() as conn:
            if disable:   # 비밀번호 오류 → 그룹웨어 계정 잠김 방지를 위해 즉시 수집 중단
                conn.execute("UPDATE mail.account SET enabled=false, enc_pass=NULL, last_error=%s, "
                             "last_sync_at=now() WHERE account=%s", (err, acct))
            else:
                conn.execute("UPDATE mail.account SET last_sync_at=now(), last_error=%s WHERE account=%s",
                             (None if ok else err, acct))
    except Exception:
        pass


def run(limit=None):
    global IMAP_USER, IMAP_PASS
    accts = accounts()
    if not accts:
        log.info("수집 대상 계정 없음")
        return
    for acct, pw in accts:
        IMAP_USER, IMAP_PASS = acct, pw
        try:
            run_one(limit)
            mark_account(acct, True)
        except imaplib.IMAP4.error as e:
            msg = "비밀번호 불일치 - 앱에 다시 로그인하면 수집이 재개됩니다"
            log.error("[%s] 로그인 실패: %s → 수집 중단", acct, e)
            mark_account(acct, False, msg, disable=(acct != ENV_USER))
        except Exception as e:
            log.exception("[%s] 수집 실패", acct)
            mark_account(acct, False, str(e)[:300])


def check():
    m = imap_connect()
    for f in FOLDERS:
        uv, n = examine(m, f)
        log.info("IMAP OK  %-6s UIDVALIDITY=%d  메일 %d건", f, uv, n)
    m.logout()
    with db() as conn:
        v = conn.execute("SELECT version()").fetchone()[0]
        log.info("DB OK    %s", v.split(",")[0])


def stats():
    with db() as conn:
        for r in conn.execute("""
            SELECT folder, count(*), min(sent_at)::date, max(sent_at)::date,
                   (SELECT count(*) FROM mail.attachment a JOIN mail.message x ON a.message_id=x.id
                     WHERE x.folder=m.folder AND x.account=m.account)
            FROM mail.message m WHERE account=%s GROUP BY folder, account""", (IMAP_USER,)):
            print(f"{r[0]:6s} 메일 {r[1]:5d}건  기간 {r[2]} ~ {r[3]}  첨부 {r[4]}건")
        for r in conn.execute("SELECT folder,last_uid,last_sync_at,last_error FROM mail.sync_state WHERE account=%s",
                              (IMAP_USER,)):
            print(f"상태 {r[0]:6s} last_uid={r[1]}  마지막 동기화 {r[2]}  오류={r[3] or '-'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="지텍 메일 수집기 P0")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dedupe", action="store_true", help="기존 첨부 중복 저장 정리 (1회)")
    a = ap.parse_args()
    if a.init_db:
        init_db()
    elif a.check:
        check()
    elif a.stats:
        stats()
    elif a.dedupe:
        dedupe()
    else:
        run(a.limit)
