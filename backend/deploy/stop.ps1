$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $ProjectRoot "deploy\uvicorn.pid"

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "No PID file found. Server may already be stopped."
    exit 0
}

$ProcessId = (Get-Content -LiteralPath $PidFile -Raw).Trim()

if ([string]::IsNullOrWhiteSpace($ProcessId)) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Empty PID file removed."
    exit 0
}

$Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue

if ($null -eq $Process) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Server process was not running. PID file removed."
    exit 0
}

Stop-Process -Id $ProcessId -Force
Remove-Item -LiteralPath $PidFile -Force

Write-Host "AliExpress FastAPI stopped. PID: $ProcessId"
