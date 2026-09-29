param([string]$Python = 'python', [int]$Port = 8000, [switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '無法建立 Python 虛擬環境。' }
    $InstallDependencies = $true
}
if ($InstallDependencies) {
    & ./.venv/Scripts/python.exe -m pip install -r requirements.txt -c requirements.lock
    if ($LASTEXITCODE -ne 0) { throw '套件安裝失敗。' }
}
if (-not $env:PUBLIC_ORIGIN -and $Port -ne 8000) { $env:PUBLIC_ORIGIN = "http://127.0.0.1:$Port" }
& ./.venv/Scripts/python.exe -m app.doctor
if ($LASTEXITCODE -ne 0) { throw '環境檢查未通過。安裝缺少套件請加 -InstallDependencies。' }
& ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port
