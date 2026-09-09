# 수집 세션 런처 — 값을 물어본 뒤 큐 스크립트와 수집기를 함께 띄운다.
# 더블클릭은 start_session.bat 으로, 터미널에서는:
#   powershell -ExecutionPolicy Bypass -File tools\start_session.ps1
#   powershell -ExecutionPolicy Bypass -File tools\start_session.ps1 -Test    # 4분 시험(데이터 저장됨)
param(
    [switch]$Test,      # 4분 시험: config=testrun, 수집기 280초
    [switch]$DryRun,    # 실행하지 않고 명령만 출력
    [string]$Port = 'COM4'
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))

function Ask($prompt, $default) {
    $v = Read-Host "$prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($v)) { $default } else { $v.Trim() }
}

Write-Host ""
Write-Host "=== CSI 수집 세션 ===" -ForegroundColor Cyan
Write-Host "Rx 4대 전원이 켜져 있고 Tx가 $Port 에 꽂혀 있어야 합니다." -ForegroundColor DarkGray
if (-not ([System.IO.Ports.SerialPort]::getportnames() -contains $Port)) {
    Write-Host "[!] $Port 가 없습니다. 연결된 포트: $([System.IO.Ports.SerialPort]::getportnames() -join ', ')" -ForegroundColor Red
    Read-Host "Enter를 누르면 종료"
    exit 1
}
Write-Host ""

if ($Test) {
    $mode = 'event'; $sno = 1; $subject = 'test'; $config = 'testrun'; $duration = 280
    Write-Host "시험 모드: event 세션 1, 4분 20초 뒤 수집기 자동 종료 (Q → Enter 로 언제든 중단)" -ForegroundColor Yellow
} else {
    $mode    = Ask "모드 (event / life / empty)" "event"
    $config  = Ask "배치 구성 id (예: o1_f01)" "o1_f01"
    $subject = Ask "피험자 라벨" "A"
    if ($mode -eq 'empty') {
        $minutes = [double](Ask "부재 수집 분" "15")
        $sno = 1
        $duration = [int]($minutes * 60 + 40)
    } else {
        $sno = [int](Ask "세션 번호" "1")
        $duration = 650
    }
}

$collector = "python tools\csi_session.py --port $Port --duration $duration --prep 0 --session-dir {session_dir}"
$args = @('tools\collect_session.py', '--mode', $mode, '--session-no', $sno,
          '--subject', $subject, '--config', $config, '--collector-cmd', $collector)
if ($mode -eq 'empty') { $args += @('--minutes', $minutes) }

Write-Host ""
Write-Host "실행: python $($args -join ' ')" -ForegroundColor DarkGray
Write-Host ""
if ($DryRun) { exit 0 }

Write-Host "화면이 전체화면으로 바뀝니다. 시작 직후 이 창의 Rx 4줄이 33Hz 근처로 차는지 확인하세요." -ForegroundColor Yellow
Write-Host "세션 중에는 다른 곳에 타이핑하지 마세요 (X/N/P/Q 키가 화면으로 들어갑니다)." -ForegroundColor Yellow
Start-Sleep -Seconds 2
& python @args
Write-Host ""
Read-Host "종료됐습니다. Enter를 누르면 창이 닫힙니다"
