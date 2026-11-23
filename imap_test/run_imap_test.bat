@echo off
chcp 65001 > nul
cd /d "%~dp0"
python imap_test.py
if errorlevel 9009 (
  echo Python이 설치되어 있지 않거나 PATH에 없습니다. py imap_test.py 로 실행해 보세요.
  pause
)
