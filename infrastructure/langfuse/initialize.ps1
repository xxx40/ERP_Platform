$ErrorActionPreference = "Stop"

$stackDirectory = $PSScriptRoot
$environmentPath = Join-Path $stackDirectory ".env"
if (Test-Path -LiteralPath $environmentPath) {
    Write-Host "Langfuse local credentials already exist: $environmentPath"
    exit 0
}

function New-RandomHex([int]$byteCount) {
    $bytes = [byte[]]::new($byteCount)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

$publicKey = "pk-lf-local-$(New-RandomHex 16)"
$secretKey = "sk-lf-local-$(New-RandomHex 32)"
$lines = @(
    "NEXTAUTH_URL=http://localhost:3000"
    "NEXTAUTH_SECRET=$(New-RandomHex 32)"
    "SALT=$(New-RandomHex 32)"
    "ENCRYPTION_KEY=$(New-RandomHex 32)"
    "TELEMETRY_ENABLED=false"
    "POSTGRES_USER=langfuse"
    "POSTGRES_PASSWORD=$(New-RandomHex 24)"
    "POSTGRES_DB=langfuse"
    "CLICKHOUSE_USER=clickhouse"
    "CLICKHOUSE_PASSWORD=$(New-RandomHex 24)"
    "REDIS_AUTH=$(New-RandomHex 24)"
    "MINIO_ROOT_USER=langfuse"
    "MINIO_ROOT_PASSWORD=$(New-RandomHex 24)"
    "LANGFUSE_INIT_ORG_ID=erp-local"
    "LANGFUSE_INIT_ORG_NAME=ERP AI Platform"
    "LANGFUSE_INIT_PROJECT_ID=erp-assistant"
    "LANGFUSE_INIT_PROJECT_NAME=ERP Assistant"
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY=$publicKey"
    "LANGFUSE_INIT_PROJECT_SECRET_KEY=$secretKey"
    "LANGFUSE_INIT_USER_EMAIL=admin@erp.local"
    "LANGFUSE_INIT_USER_NAME=ERP Admin"
    "LANGFUSE_INIT_USER_PASSWORD=$(New-RandomHex 16)"
)
[IO.File]::WriteAllLines(
    $environmentPath,
    $lines,
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Created local-only Langfuse credentials: $environmentPath"
