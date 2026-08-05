param(
    [ValidateRange(1, 65535)]
    [int]$Port = 5174,
    [string]$ApiBaseUrl = "http://127.0.0.1:8001"
)

$ErrorActionPreference = "Stop"
$env:VITE_API_BASE_URL = $ApiBaseUrl.TrimEnd("/")

Set-Location $PSScriptRoot
npm run dev -- --host 127.0.0.1 --port $Port
