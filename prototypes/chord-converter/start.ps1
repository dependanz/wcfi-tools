param([int]$Port = 8818)
$ErrorActionPreference = 'Stop'
$previewPython = Join-Path $PSScriptRoot '../../.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $previewPython)) { throw 'Install wcfi-tools in its .venv first.' }
$previewRuntime = Join-Path $env:USERPROFILE '.wcfi/chord-converter'
New-Item -ItemType Directory -Force -Path $previewRuntime | Out-Null
$previewHealth = "http://127.0.0.1:$Port/healthz"
try { $previewResponse = Invoke-RestMethod $previewHealth -TimeoutSec 2 } catch { $previewResponse = $null }
if ($previewResponse.app -eq 'wcfi-chord-converter') {
    Write-Output "Already running: http://127.0.0.1:$Port/"
    exit 0
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is occupied by another service." }
$previewProcess = Start-Process -FilePath $previewPython -ArgumentList @('-B', '-u', ('"' + (Join-Path $PSScriptRoot 'server.py') + '"'), '--port', $Port) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $previewRuntime 'stdout.log') -RedirectStandardError (Join-Path $previewRuntime 'stderr.log')
@{pid=$previewProcess.Id; port=$Port; source=$PSScriptRoot; started=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $previewRuntime 'process.json')
for ($previewAttempt=0; $previewAttempt -lt 20; $previewAttempt++) {
    Start-Sleep -Milliseconds 250
    try { $previewResponse = Invoke-RestMethod $previewHealth -TimeoutSec 2 } catch { $previewResponse = $null }
    if ($previewResponse.app -eq 'wcfi-chord-converter') { Write-Output "Running: http://127.0.0.1:$Port/ (PID $($previewProcess.Id))"; exit 0 }
    if ($previewProcess.HasExited) { throw "Preview exited; inspect $previewRuntime/stderr.log" }
}
throw "Preview did not become ready; inspect $previewRuntime"
