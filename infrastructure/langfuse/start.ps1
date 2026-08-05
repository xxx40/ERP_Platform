$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop is not installed or docker.exe is not on PATH."
}

& (Join-Path $PSScriptRoot "initialize.ps1")
& (Join-Path $PSScriptRoot "configure-erp.ps1")

docker compose `
    --env-file (Join-Path $PSScriptRoot ".env") `
    -f (Join-Path $PSScriptRoot "compose.yaml") `
    up -d
if ($LASTEXITCODE -ne 0) {
    throw "Langfuse Docker Compose failed with exit code $LASTEXITCODE."
}

Write-Host "Langfuse is starting at http://127.0.0.1:3000"
Write-Host "Run .\infrastructure\langfuse\show-credentials.ps1 to view the local login and API keys."
