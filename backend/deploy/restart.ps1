$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$PidFile = Join-Path $ProjectRoot "deploy\uvicorn.pid"
$LogDir = Join-Path $ProjectRoot "logs"
$OutLog = Join-Path $LogDir "uvicorn-out.log"
$ErrLog = Join-Path $LogDir "uvicorn-error.log"

Set-Location $ProjectRoot

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual environment not found. Create it first from the project root: python -m venv venv"
}

& $VenvPython -m pip install -r requirements.txt

& (Join-Path $PSScriptRoot "stop.ps1")

$Process = Start-Process `
    -FilePath $VenvPython `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden `
    -PassThru

Set-Content -Path $PidFile -Value $Process.Id

Write-Host "AliExpress FastAPI started on http://localhost:8000"
Write-Host "PID: $($Process.Id)"
Write-Host "Logs: $LogDir"
