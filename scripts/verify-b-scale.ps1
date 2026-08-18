# B단계 — 실운용 규모 검증.
# A단계(verify-a.ps1)는 240슬롯 축소판으로 기능을 확인한다. 이 스크립트는 실제 운용값인
# 19,800슬롯(10분, ~594윈도우) 보정을 통째로 돌려 소요 시간과 메모리를 측정한다.
# 클라우드 인스턴스 사양을 정하기 전에 반드시 한 번 통과시킬 것.

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $projectRoot

$secrets = @{}
if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
        $pair = $_ -split '=', 2
        $secrets[$pair[0].Trim()] = $pair[1].Trim()
    }
}
$token   = $secrets['CSI_TOKEN']
$adminPw = $secrets['ADMIN_PASSWORD']
$dbPw    = if ($secrets['DB_PASSWORD']) { $secrets['DB_PASSWORD'] } else { 'csi_c6_dev' }
$base    = 'http://127.0.0.1:8180'
$slots   = if ($env:SCALE_SLOTS) { [int]$env:SCALE_SLOTS } else { 19800 }

# 운용값으로 스택을 세운다 (A단계가 남긴 240 설정을 덮어쓴다)
Remove-Item Env:CALIBRATION_SLOTS -ErrorAction SilentlyContinue
docker compose up -d --wait | Out-Null
$workerSlots = [int](docker exec csi-c6-worker printenv CALIBRATION_SLOTS)
if ($workerSlots -ne $slots) { throw "worker CALIBRATION_SLOTS=$workerSlots, expected $slots (.env를 확인하세요)" }

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
if ($adminPw) {
    try {
        Invoke-WebRequest -Uri "$base/login" -Method Post -Body @{ password = $adminPw } `
            -WebSession $session -UseBasicParsing | Out-Null
    } catch {}
}
function Api($method, $path) { Invoke-RestMethod -Method $method -Uri "$base$path" -WebSession $session }
$replayToken = if ($token) { @('--token', $token) } else { @() }

try { Api Post '/api/control/standby' | Out-Null } catch {}
$baseSeq = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() % 100000000)
Api Post '/api/control/calibration/start?location=scale-test' | Out-Null

Write-Host "적재 중 — $slots 슬롯 (최대 속도)..."
$ingestWatch = [Diagnostics.Stopwatch]::StartNew()
python scripts/replay_c6_sample.py --slots $slots --start-seq $baseSeq @replayToken
$ingestWatch.Stop()

$rows = [int](docker exec csi-c6-db mysql -N -ucsi_c6 "-p$dbPw" csi_c6 -e "SELECT COUNT(*) FROM reports WHERE seq >= $baseSeq")
Write-Host ("적재 완료: {0:N0}행 / {1:N1}초 ({2:N0}행/s)" -f $rows, $ingestWatch.Elapsed.TotalSeconds, ($rows / $ingestWatch.Elapsed.TotalSeconds))

Write-Host "보정 처리 대기 중 (워커가 전 세션을 로드해 특징을 계산한다)..."
$calWatch = [Diagnostics.Stopwatch]::StartNew()
$peakMemMb = 0
$deadline = (Get-Date).AddMinutes(10)
do {
    Start-Sleep -Seconds 3
    $mem = (docker stats csi-c6-worker --no-stream --format '{{.MemUsage}}') -split '/' | Select-Object -First 1
    if ($mem -match '([\d.]+)\s*([GM])iB') {
        $mb = [double]$matches[1] * $(if ($matches[2] -eq 'G') { 1024 } else { 1 })
        if ($mb -gt $peakMemMb) { $peakMemMb = $mb }
    }
    $state = Api Get '/api/system'
} while ($state.mode -eq 'CALIBRATION' -and (Get-Date) -lt $deadline)
$calWatch.Stop()

if ($state.mode -ne 'STANDBY') { throw "보정이 10분 안에 끝나지 않음: mode=$($state.mode) error=$($state.last_error)" }
$cal = (Api Get '/api/system')
$row = docker exec csi-c6-db mysql -N -ucsi_c6 "-p$dbPw" csi_c6 -e `
    "SELECT status,window_count,ROUND(threshold,4),ROUND(empty_motion_q95,4) FROM calibrations ORDER BY calibration_id DESC LIMIT 1"
$parts = $row -split "`t"
if ($parts[0] -ne 'VALID') { throw "보정 실패: $row / $($state.last_error)" }

Write-Host ""
Write-Host ("B단계 PASS — 보정 #{0}" -f $cal.active_calibration_id)
Write-Host ("  윈도우 수      : {0}" -f $parts[1])
Write-Host ("  임계값         : {0}" -f $parts[2])
Write-Host ("  부재 에너지q95 : {0}" -f $parts[3])
Write-Host ("  보정 처리 시간 : {0:N1}초" -f $calWatch.Elapsed.TotalSeconds)
Write-Host ("  워커 최대 메모리: {0:N0} MB" -f $peakMemMb)
Write-Host ""
Write-Host "클라우드 사양 기준: 위 최대 메모리 + MySQL(~1GB) + admin JVM(~512MB)에 여유를 더할 것."
