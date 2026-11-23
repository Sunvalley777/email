# 지텍 AI 메일 비서 - 서버 배포 스크립트
# 실행: PowerShell에서  C:\work\email\deploy.ps1
#  - collector, classifier, webapp, notifier 4개 폴더를 압축 파일 1개로 전송 → 서버에서 풀고 웹 앱 재시작
#  - SSH 키 등록 시 비밀번호 입력 없음 (sudo 비밀번호만 1회)
#  - Windows PowerShell 5.1 은 파이프(|)로 바이너리를 넘기면 깨지므로 임시 파일 방식 사용
$Server = "redmine@172.1.2.42"
$Local  = $PSScriptRoot          # 이 스크립트가 있는 폴더 (D:\ZtecAX\email)
$Tmp    = Join-Path $env:TEMP "mail_deploy.tar"

Write-Host "[1/3] 압축 (collector, classifier, webapp, notifier)" -ForegroundColor Cyan
if (Test-Path $Tmp) { Remove-Item $Tmp }
tar -cf $Tmp -C $Local --exclude "*.png" --exclude "*.pdf" --exclude "__pycache__" --exclude ".env" collector classifier webapp notifier
if ($LASTEXITCODE -ne 0) { Write-Host "압축 실패" -ForegroundColor Red; exit 1 }
Write-Host ("  {0:N0} KB" -f ((Get-Item $Tmp).Length / 1KB))

Write-Host "[2/3] 서버로 전송" -ForegroundColor Cyan
scp $Tmp "${Server}:~/mail_deploy.tar"
if ($LASTEXITCODE -ne 0) { Write-Host "전송 실패" -ForegroundColor Red; exit 1 }

Write-Host "[3/3] 서버에서 풀기 + 웹 앱 재시작 (sudo 비밀번호를 물으면 서버 계정 비밀번호 입력)" -ForegroundColor Cyan
ssh -t $Server "cd ~/mail-collector && rm -rf collector && tar -xf ~/mail_deploy.tar && cp -r collector/. . && rm -rf collector ~/mail_deploy.tar && find . -name '*.sh' -not -path './venv/*' -exec sed -i 's/\r$//' {} + && ./venv/bin/pip install -q -r requirements.txt -r webapp/requirements.txt && ./venv/bin/python notifier/notifier.py --init-db && echo '  배포 완료 (패키지·DB 스키마 확인 포함)' && sudo systemctl restart mail-web && echo -n '  mail-web: ' && systemctl is-active mail-web"
Remove-Item $Tmp -ErrorAction SilentlyContinue
