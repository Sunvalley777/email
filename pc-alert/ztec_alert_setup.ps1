# ===================================================================
#  지텍 AI 메일 비서 - PC 바탕화면 알림 설치
#  실행:  powershell -ExecutionPolicy Bypass -File ztec_alert_setup.ps1
#  제거:  powershell -ExecutionPolicy Bypass -File ztec_alert_setup.ps1 -Uninstall
#  - 관리자 권한 불필요, 현재 사용자에게만 설치
#  - 토큰은 Windows 사용자 암호화(DPAPI)로 저장, 서버 인증서는 지문(thumbprint)으로 고정
# ===================================================================
param([switch]$Uninstall, [string]$Server = "https://172.1.2.42:8443")

$ErrorActionPreference = "Stop"
$Dir      = Join-Path $env:LOCALAPPDATA "ZtecMailAlert"
$Agent    = Join-Path $Dir "ztec_alert.ps1"
$Config   = Join-Path $Dir "config.json"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "지텍 메일 알림.lnk"

function Stop-Agent {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like "*ztec_alert.ps1*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Stop-Agent
    Remove-Item $Shortcut -ErrorAction SilentlyContinue
    Remove-Item $Dir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "제거 완료 (웹 앱 알림센터에서 토큰도 '해제'하세요)" -ForegroundColor Green
    exit 0
}

Write-Host "`n지텍 AI 메일 비서 - PC 알림 설치" -ForegroundColor Cyan
Write-Host "서버: $Server"

# 1) 서버 인증서 지문 확인 (자체 서명 인증서 → 지문으로 고정)
$uri = [Uri]$Server
$tcp = New-Object Net.Sockets.TcpClient($uri.Host, $uri.Port)
$ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true } -as [Net.Security.RemoteCertificateValidationCallback]))
$ssl.AuthenticateAsClient($uri.Host)
$pin = (New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)).Thumbprint
$ssl.Dispose(); $tcp.Dispose()
Write-Host "서버 인증서 지문: $pin"

# 2) 토큰 입력 (화면에 표시 안 됨)
$sec = Read-Host "웹 앱 [알림함] 화면의 [PC 알림 토큰 발급]에서 받은 토큰을 붙여넣으세요" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if (-not $plain.StartsWith("ztec_")) { Write-Host "토큰 형식이 올바르지 않습니다 (ztec_ 로 시작)" -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
@{ server = $Server; pin = $pin; token = (ConvertFrom-SecureString $sec); interval = 60 } |
    ConvertTo-Json | Set-Content -Path $Config -Encoding UTF8

# 3) 알림 프로그램 저장
$AgentCode = @'
# 지텍 AI 메일 비서 - PC 알림 프로그램 (ztec_alert_setup.ps1 이 설치)
param([switch]$Test)
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Dir "agent.log"
function Write-Log($m) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 512KB) { Remove-Item $Log }
    Add-Content -Path $Log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m) -Encoding UTF8
}

# 중복 실행 방지
$mutex = New-Object Threading.Mutex($false, "Local\ZtecMailAlert")
if (-not $Test -and -not $mutex.WaitOne(0)) { exit 0 }

$cfg = Get-Content (Join-Path $Dir "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$sec = ConvertTo-SecureString $cfg.token
$token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

# 서버 인증서: 정상 인증서이거나, 설치 때 저장한 지문과 같을 때만 허용
Add-Type -TypeDefinition @"
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class ZtecPin {
    public static string Pin;
    public static bool Check(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) {
        if (e == SslPolicyErrors.None) return true;
        return c != null && string.Equals(c.GetCertHashString(), Pin, System.StringComparison.OrdinalIgnoreCase);
    }
}
"@
[ZtecPin]::Pin = $cfg.pin
[Net.ServicePointManager]::ServerCertificateValidationCallback =
    [Delegate]::CreateDelegate([Net.Security.RemoteCertificateValidationCallback], [ZtecPin].GetMethod("Check"))
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Show-Toast($title, $body, $url) {
    $e = { param($s) [Security.SecurityElement]::Escape([string]$s) }
    $xml = @"
<toast activationType="protocol" launch="$(& $e $url)" scenario="default">
  <visual><binding template="ToastGeneric">
    <text>$(& $e $title)</text>
    <text>$(& $e $body)</text>
    <text placement="attribution">지텍 AI 메일 비서</text>
  </binding></visual>
  <audio src="ms-winsoundevent:Notification.Mail"/>
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show(
        [Windows.UI.Notifications.ToastNotification]::new($doc))
}

function Poll {
    $r = Invoke-RestMethod -Uri "$($cfg.server)/api/pc/alerts" -Headers @{ "X-Token" = $token } -TimeoutSec 20 -UseBasicParsing
    $items = @($r.items)
    if ($items.Count -gt 5) {
        Show-Toast "🔔 새 알림 $($items.Count)건" (($items | Select-Object -First 3 | ForEach-Object { $_.title }) -join "`n") "$($cfg.server)/alerts"
    } else {
        foreach ($i in $items) { Show-Toast $i.title $i.body $i.url }
    }
    if ($items.Count) { Write-Log "알림 $($items.Count)건 표시" }
    return $r
}

if ($Test) {
    try {
        $r = Poll
        Show-Toast "✅ PC 알림 연결 완료" "$($r.account)`n읽지 않은 알림 $($r.unread)건" "$($cfg.server)/alerts"
        Write-Log "연결 테스트 성공: $($r.account)"
        Write-Host "  연결 성공: $($r.account) (읽지 않은 알림 $($r.unread)건)"
        exit 0
    } catch { Write-Log "연결 테스트 실패: $_"; Write-Host "  실패: $_"; exit 1 }
}

Write-Log "시작 (주기 $($cfg.interval)초)"
$fail = 0
while ($true) {
    try { Poll | Out-Null; $fail = 0 }
    catch {
        $fail++
        if ($fail -le 3 -or $fail % 30 -eq 0) { Write-Log "접속 실패($fail): $_" }   # 사외·VPN 끊김 등은 조용히 재시도
        if ("$_" -match "401") { Write-Log "토큰이 해제되어 종료"; break }
    }
    Start-Sleep -Seconds ([int]$cfg.interval)
}
'@
Set-Content -Path $Agent -Value $AgentCode -Encoding UTF8

# 4) 연결 테스트 + 테스트 알림
Stop-Agent
& powershell -NoProfile -ExecutionPolicy Bypass -File $Agent -Test
if ($LASTEXITCODE -ne 0) { Write-Host "연결 테스트 실패 - 토큰 또는 서버 주소를 확인하세요 (로그: $Dir\agent.log)" -ForegroundColor Red; exit 1 }

# 5) 로그인 시 자동 실행 (시작프로그램 바로가기)
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($Shortcut)
$lnk.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent`""
$lnk.WindowStyle = 7
$lnk.Description = "지텍 AI 메일 비서 PC 알림"
$lnk.Save()

# 6) 지금 바로 시작
Start-Process -FilePath $lnk.TargetPath -ArgumentList $lnk.Arguments -WindowStyle Hidden
Write-Host "`n설치 완료 - 바탕화면 오른쪽 아래에 테스트 알림이 떴다면 정상입니다." -ForegroundColor Green
Write-Host "자동 실행: Windows 로그인 시 / 제거: -Uninstall 옵션으로 다시 실행"
