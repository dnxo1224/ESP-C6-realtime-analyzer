$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $projectRoot

# .env의 비밀값을 읽어 보안이 켜진 구성에서도 그대로 검증한다.
# (토큰/비밀번호가 비어 있으면 무마찰 로컬 구성으로 자동 축소된다)
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

python -m unittest discover -s tests -v
python scripts/replay_c6_sample.py --validate-only --slots 240
docker compose config --quiet

$env:CALIBRATION_SLOTS = '240'
docker compose up -d --build --wait

# admin 비밀번호가 설정돼 있으면 로그인 세션을 만들어 이후 호출에 재사용한다.
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
if ($adminPw) {
    # PS 5.1 주의: -UseBasicParsing이 없으면 비대화형에서 IE 엔진 프롬프트로 실패한다.
    # 또한 302를 POST로 재전송해 로그인 성공 후에도 404가 나므로 그 예외는 무시한다 (쿠키는 이미 세션에 담긴다).
    try {
        Invoke-WebRequest -Uri "$base/login" -Method Post -Body @{ password = $adminPw } `
            -WebSession $session -UseBasicParsing | Out-Null
    } catch {}
    $probe = try { (Invoke-WebRequest -Uri "$base/api/stats" -WebSession $session -UseBasicParsing).StatusCode } catch { 0 }
    if ($probe -ne 200) { throw 'admin login failed — check ADMIN_PASSWORD in .env' }
}
function Api($method, $path) { Invoke-RestMethod -Method $method -Uri "$base$path" -WebSession $session }

$replayToken = if ($token) { @('--token', $token) } else { @() }

try { Api Post '/api/control/standby' | Out-Null } catch {}
$baseSeq = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() % 100000000)
Api Post '/api/control/calibration/start?location=A-stage-sample' | Out-Null
python scripts/replay_c6_sample.py --slots 240 --start-seq $baseSeq @replayToken

$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $state = Api Get '/api/system'
} while ($state.mode -eq 'CALIBRATION' -and (Get-Date) -lt $deadline)
if ($state.mode -ne 'STANDBY' -or -not $state.active_calibration_id) {
    throw "Calibration did not complete: $($state | ConvertTo-Json -Compress)"
}

Api Post '/api/control/inference/start' | Out-Null
$inferenceSeq = $baseSeq + 1000
python scripts/replay_c6_sample.py --slots 240 --start-seq $inferenceSeq @replayToken
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $results = @(Api Get '/api/inference?minutes=5')
    $matched = @($results | Where-Object { $_.seq_end -eq ($inferenceSeq + 239) })
} while ($matched.Count -eq 0 -and (Get-Date) -lt $deadline)
if ($matched.Count -eq 0) { throw 'RF inference result was not recorded' }

# 음성 검증 1: 490바이트 C5 프레임은 DB에 닿지 못한다.
# 토큰이 있으면 먼저 인증해, 거부 사유가 '토큰 없음'이 아니라 '프레임 크기'임을 보장한다.
$before = [int](docker exec csi-c6-db mysql -N -ucsi_c6 "-p$dbPw" csi_c6 -e 'SELECT COUNT(*) FROM reports')
$env:CSI_TOKEN = $token
python -c "import os,socket,sys;sys.path.insert(0,'tests');from test_contract import make_c6_frame;t=os.environ.get('CSI_TOKEN','');s=socket.create_connection(('127.0.0.1',9600));t and s.sendall(('CSI-TOKEN '+t+'\n').encode());s.sendall(make_c6_frame(csi_len=490));s.close()"
Start-Sleep -Seconds 2
$after = [int](docker exec csi-c6-db mysql -N -ucsi_c6 "-p$dbPw" csi_c6 -e 'SELECT COUNT(*) FROM reports')
if ($before -ne $after) { throw "C5 frame reached C6 DB: before=$before after=$after" }

# 음성 검증 2: 토큰이 설정된 경우, 토큰 없는 스트림은 한 행도 적재되지 않는다.
if ($token) {
    python scripts/replay_c6_sample.py --slots 30 --start-seq ($baseSeq + 5000)
    Start-Sleep -Seconds 2
    $afterNoToken = [int](docker exec csi-c6-db mysql -N -ucsi_c6 "-p$dbPw" csi_c6 -e 'SELECT COUNT(*) FROM reports')
    if ($after -ne $afterNoToken) { throw "untokenized stream was ingested: $after -> $afterNoToken" }

    # 세션 없이 호출 — PS 5.1은 401을 예외로 던지므로 예외에서 상태 코드를 꺼낸다.
    # try/catch를 식으로 쓰면 값이 비어 나오는 경우가 있어 명시적 블록으로 둔다.
    $unauth = -1
    try {
        $unauthResponse = Invoke-WebRequest -Uri "$base/api/stats" -UseBasicParsing
        $unauth = [int]$unauthResponse.StatusCode
    } catch {
        if ($_.Exception.Response) { $unauth = [int]$_.Exception.Response.StatusCode }
    }
    if ($unauth -ne 401) { throw "unauthenticated /api/stats returned $unauth, expected 401" }
}

$services = docker compose ps --format json | ConvertFrom-Json
$unhealthy = @($services | Where-Object { $_.Health -and $_.Health -ne 'healthy' })
if ($unhealthy.Count) { throw "Unhealthy services: $($unhealthy.Service -join ', ')" }

$secured = if ($token -and $adminPw) { 'secured' } else { 'open (no secrets in .env)' }
Write-Host "A-stage PASS [$secured]: calibration=$($state.active_calibration_id), probability=$($matched[-1].probability), C5 rejected, services healthy"
