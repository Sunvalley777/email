# -*- coding: utf-8 -*-
"""
지텍 다우오피스 IMAP 접속 테스트 (읽기 전용)
- 실행: python imap_test.py  또는  run_imap_test.bat 더블클릭
- 비밀번호는 화면에 표시되지 않고, 이 PC 밖으로 전송·저장되지 않습니다.
"""
import imaplib
import getpass
import ssl
import sys

HOST = "gw.z-tec.co.kr"
PORT = 993
DEFAULT_USER = "jinyong.byun@z-tec.co.kr"


def main():
    user = input(f"메일 계정 [{DEFAULT_USER}]: ").strip() or DEFAULT_USER
    pw = getpass.getpass("메일 비밀번호(입력 내용은 표시되지 않음): ")

    print(f"\n[1/4] {HOST}:{PORT} SSL 접속 중...")
    try:
        m = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context())
    except ssl.SSLError as e:
        print("[실패] SSL 인증서 문제:", e)
        return 1
    except Exception as e:
        print("[실패] 서버 접속 불가:", type(e).__name__, e)
        return 1
    print("  서버 응답:", m.welcome.decode(errors="ignore"))

    print("[2/4] 로그인 시도...")
    try:
        m.login(user, pw)
    except imaplib.IMAP4.error as e:
        print("[실패] 로그인 거부:", e)
        print("  → 비밀번호 확인, 또는 다우오피스 > 메일 > 환경설정 > POP3/IMAP 사용 여부 확인")
        return 1
    print("  [OK] 로그인 성공")

    print("[3/4] 폴더 목록")
    typ, folders = m.list()
    print(f"  폴더 수: {len(folders)}")
    for f in folders[:20]:
        print("   ", f.decode(errors="ignore"))

    print("[4/4] 받은편지함 (읽기 전용)")
    typ, cnt = m.select("INBOX", readonly=True)
    print("  메일 수:", cnt[0].decode())
    typ, caps = m.capability()
    print("  서버 기능:", caps[0].decode(errors="ignore"))

    m.logout()
    print("\n=== 결과: IMAP 연동 가능 ===")
    return 0


if __name__ == "__main__":
    code = main()
    input("\n엔터를 누르면 종료합니다...")
    sys.exit(code)
