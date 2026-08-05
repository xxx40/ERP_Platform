param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8201
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$server = Join-Path $PSScriptRoot "scripts\database_showcase\server.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found: $python"
}

Write-Host "Read-only database showcase: http://127.0.0.1:$Port"
& $python $server --host 127.0.0.1 --port $Port
