$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $projectRoot

python -m unittest discover -s tests -v
python scripts/replay_c6_sample.py --validate-only --slots 240
docker compose config --quiet

$env:CALIBRATION_SLOTS = '240'
docker compose up -d --build --wait

try { Invoke-RestMethod -Method Post http://127.0.0.1:8180/api/control/standby | Out-Null } catch {}
$baseSeq = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() % 100000000)
Invoke-RestMethod -Method Post 'http://127.0.0.1:8180/api/control/calibration/start?location=A-stage-sample' | Out-Null
python scripts/replay_c6_sample.py --slots 240 --start-seq $baseSeq

$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $state = Invoke-RestMethod http://127.0.0.1:8180/api/system
} while ($state.mode -eq 'CALIBRATION' -and (Get-Date) -lt $deadline)
if ($state.mode -ne 'STANDBY' -or -not $state.active_calibration_id) {
    throw "Calibration did not complete: $($state | ConvertTo-Json -Compress)"
}

Invoke-RestMethod -Method Post http://127.0.0.1:8180/api/control/inference/start | Out-Null
$inferenceSeq = $baseSeq + 1000
python scripts/replay_c6_sample.py --slots 240 --start-seq $inferenceSeq
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $results = @(Invoke-RestMethod 'http://127.0.0.1:8180/api/inference?minutes=5')
    $matched = @($results | Where-Object { $_.seq_end -eq ($inferenceSeq + 239) })
} while ($matched.Count -eq 0 -and (Get-Date) -lt $deadline)
if ($matched.Count -eq 0) { throw 'RF inference result was not recorded' }

$before = [int](docker exec csi-c6-db mysql -N -ucsi_c6 -pcsi_c6_dev csi_c6 -e 'SELECT COUNT(*) FROM reports')
python -c "import socket,sys;sys.path.insert(0,'tests');from test_contract import make_c6_frame;s=socket.create_connection(('127.0.0.1',9600));s.sendall(make_c6_frame(csi_len=490));s.close()"
Start-Sleep -Seconds 1
$after = [int](docker exec csi-c6-db mysql -N -ucsi_c6 -pcsi_c6_dev csi_c6 -e 'SELECT COUNT(*) FROM reports')
if ($before -ne $after) { throw "C5 frame reached C6 DB: before=$before after=$after" }

$services = docker compose ps --format json | ConvertFrom-Json
$unhealthy = @($services | Where-Object { $_.Health -and $_.Health -ne 'healthy' })
if ($unhealthy.Count) { throw "Unhealthy services: $($unhealthy.Service -join ', ')" }

Write-Host "A-stage PASS: calibration=$($state.active_calibration_id), probability=$($matched[-1].probability), C5 rejected, services healthy"
