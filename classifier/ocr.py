"""
지텍 AI 메일 비서 - 이미지 메일 OCR (KEIT 안내·공고 포스터 등)

본문 글자가 적고 이미지가 있는 메일에서 이미지 속 글자를 읽어 AI 분류·요약에 함께 넣는다.
  - 대상 이미지: 본문에 붙은 이미지(cid:) · 본문의 외부 이미지(http/https) · 이미지 첨부(jpg/png 등)
  - 작은 이미지(로고·아이콘·여백)는 건너뜀, 메일당 최대 OCR_MAX_IMAGES 장
  - 처리: 사내 서버의 Tesseract (kor+eng) - 외부 OCR 서비스로 보내지 않음
  - 서버 준비:  sudo apt install -y tesseract-ocr tesseract-ocr-kor
"""
import email
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from email import policy
from pathlib import Path

log = logging.getLogger("ocr")

OCR_LANG = os.getenv("OCR_LANG", "kor+eng")
OCR_MIN_TEXT = int(os.getenv("OCR_MIN_TEXT", "400"))       # 본문 글자가 이보다 적으면 OCR
OCR_MAX_IMAGES = int(os.getenv("OCR_MAX_IMAGES", "6"))
OCR_MIN_SIDE = int(os.getenv("OCR_MIN_SIDE", "180"))       # 가로·세로 중 짧은 쪽이 이보다 작으면 로고·아이콘으로 보고 생략
OCR_MAX_BYTES = 8 * 1024 * 1024
OCR_FETCH_EXTERNAL = os.getenv("OCR_FETCH_EXTERNAL", "1") == "1"   # 본문의 외부 이미지 내려받기
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")


def available():
    return shutil.which("tesseract") is not None


def need_ocr(body_text, has_html, n_image_atts=0):
    text = re.sub(r"\s+", "", body_text or "")
    return (has_html or n_image_atts > 0) and len(text) < OCR_MIN_TEXT


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ztec-mail-ocr)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(OCR_MAX_BYTES + 1)
    return data if len(data) <= OCR_MAX_BYTES else None


def collect_images(raw_path, blob_lookup):
    """메일에서 OCR 할 이미지 바이트 목록 [(이름, bytes)] - blob_lookup(sha256) → 저장된 첨부 경로"""
    if not raw_path or not Path(raw_path).exists():
        return []
    msg = email.message_from_bytes(Path(raw_path).read_bytes(), policy=policy.compat32)
    parts, html = {}, []

    def payload(part):
        sha = (part.get("X-Ztec-Blob-SHA256") or "").strip()
        if sha:
            p = blob_lookup(sha)
            return Path(p).read_bytes() if p and Path(p).exists() else None
        return part.get_payload(decode=True)

    out, seen = [], set()

    def add(name, data):
        if not data or len(data) > OCR_MAX_BYTES:
            return
        h = hashlib.sha1(data).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append((name, data))

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        cid = (part.get("Content-ID") or "").strip().strip("<>").lower()
        if ctype == "text/html" and not part.get_filename():
            raw = part.get_payload(decode=True) or b""
            cs = (part.get_content_charset() or "utf-8").replace("ks_c_5601-1987", "cp949")
            try:
                html.append(raw.decode(cs, "replace"))
            except LookupError:
                html.append(raw.decode("utf-8", "replace"))
        elif cid:
            parts[cid] = part
        fname = (part.get_filename() or "").lower()
        if not cid and (ctype.startswith("image/") or fname.endswith(IMG_EXT)):
            add(fname or "첨부이미지", payload(part))          # 이미지 첨부 (포스터 등)

    # 본문 순서대로: cid 이미지 → 외부 이미지
    for src in re.findall(r"""(?is)<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", "\n".join(html)):
        if len(out) >= OCR_MAX_IMAGES * 2:
            break
        src = src.strip()
        if src.lower().startswith("cid:"):
            p = parts.get(src[4:].strip("<>").lower())
            if p is not None:
                add(src, payload(p))
        elif OCR_FETCH_EXTERNAL and re.match(r"(?i)https?://", src):
            try:
                add(src, _fetch(src))
            except Exception as e:
                log.info("외부 이미지 받기 실패 %s: %s", src[:80], e)
    return out


def _prep(data):
    """흑백 변환 + 작은 이미지 확대 → PNG 바이트. 로고·아이콘 크기면 None"""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    try:
        im.seek(0)                                   # GIF 첫 장면
    except Exception:
        pass
    w, h = im.size
    if min(w, h) < OCR_MIN_SIDE:
        return None
    im = ImageOps.exif_transpose(im).convert("L")
    if w < 1400:                                     # 작은 글자 인식률 개선
        r = 1400 / w
        im = im.resize((int(w * r), int(h * r)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def ocr_bytes(data):
    png = _prep(data)
    if png is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(png)
        f.flush()
        r = subprocess.run(["tesseract", f.name, "stdout", "-l", OCR_LANG, "--psm", "3"],
                           capture_output=True, timeout=120)
    txt = r.stdout.decode("utf-8", "replace")
    txt = re.sub(r"[ \t]+", " ", txt)
    # 잡음 줄 제거: 한글·영문·숫자가 4자 미만인 줄 (표 테두리·아이콘 오인식)
    keep = [l.strip() for l in txt.splitlines() if len(re.findall(r"[가-힣A-Za-z0-9]", l)) >= 4]
    return "\n".join(keep)


def ocr_mail(raw_path, blob_lookup):
    """→ (글자, 처리한 이미지 수, 오류)"""
    if not available():
        return "", 0, "tesseract 미설치 (sudo apt install -y tesseract-ocr tesseract-ocr-kor)"
    texts, n, err = [], 0, None
    for name, data in collect_images(raw_path, blob_lookup):
        if n >= OCR_MAX_IMAGES:
            break
        try:
            t = ocr_bytes(data)
        except Exception as e:
            err = f"{name[:60]}: {str(e)[:120]}"
            continue
        if t is None:                                # 작은 이미지
            continue
        n += 1
        if len(re.sub(r"\s", "", t)) >= 10:
            texts.append(t)
    return "\n\n".join(texts)[:12000], n, err
