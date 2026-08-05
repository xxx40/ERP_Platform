$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$erpEnvironmentPath = Join-Path $projectRoot ".env"
$values = @{}
foreach ($line in [IO.File]::ReadAllLines($erpEnvironmentPath)) {
    if ($line -match "^([^#=]+)=(.*)$") {
        $values[$matches[1]] = $matches[2]
    }
}
$baseUrl = ([string]$values["LANGFUSE_BASE_URL"]).TrimEnd("/")
$publicKey = [string]$values["LANGFUSE_PUBLIC_KEY"]
$secretKey = [string]$values["LANGFUSE_SECRET_KEY"]
if (-not $baseUrl -or -not $publicKey -or -not $secretKey) {
    throw "Langfuse Cloud is not configured. Run configure-cloud.ps1 first."
}

$authorization = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes("${publicKey}:${secretKey}")
)
$now = [DateTime]::UtcNow.ToString("o")
$payload = @{
    batch = @(
        @{
            id = [Guid]::NewGuid().ToString("N")
            timestamp = $now
            type = "trace-create"
            body = @{
                id = [Guid]::NewGuid().ToString("N")
                name = "erp-cloud-connectivity-check"
                environment = "development"
            }
        }
    )
} | ConvertTo-Json -Depth 8
$response = Invoke-WebRequest `
    -UseBasicParsing `
    -Method Post `
    -Uri "$baseUrl/api/public/ingestion" `
    -Headers @{ Authorization = "Basic $authorization" } `
    -ContentType "application/json" `
    -Body $payload `
    -TimeoutSec 15
if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
    throw "Langfuse ingestion returned HTTP $($response.StatusCode)."
}
Write-Host "Langfuse Cloud accepted the metadata-only connectivity trace (HTTP $($response.StatusCode))."
