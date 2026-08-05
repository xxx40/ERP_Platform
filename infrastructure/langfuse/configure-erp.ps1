$ErrorActionPreference = "Stop"

$langfuseEnvironmentPath = Join-Path $PSScriptRoot ".env"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$erpEnvironmentPath = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $langfuseEnvironmentPath)) {
    & (Join-Path $PSScriptRoot "initialize.ps1")
}
if (-not (Test-Path -LiteralPath $erpEnvironmentPath)) {
    throw "ERP environment file was not found: $erpEnvironmentPath"
}

$langfuseValues = @{}
foreach ($line in [IO.File]::ReadAllLines($langfuseEnvironmentPath)) {
    if ($line -match "^([^#=]+)=(.*)$") {
        $langfuseValues[$matches[1]] = $matches[2]
    }
}

$updates = [ordered]@{
    LANGFUSE_BASE_URL = "http://127.0.0.1:3000"
    LANGFUSE_PUBLIC_KEY = $langfuseValues["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
    LANGFUSE_SECRET_KEY = $langfuseValues["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
    LANGFUSE_ENVIRONMENT = "development"
    LANGFUSE_TIMEOUT_SECONDS = "2"
}
$remaining = [Collections.Generic.HashSet[string]]::new(
    [string[]]$updates.Keys,
    [StringComparer]::OrdinalIgnoreCase
)
$result = [Collections.Generic.List[string]]::new()
foreach ($line in [IO.File]::ReadAllLines($erpEnvironmentPath)) {
    if ($line -match "^([^#=]+)=") {
        $key = $matches[1]
        if ($updates.Contains($key)) {
            $result.Add("$key=$($updates[$key])")
            $remaining.Remove($key) | Out-Null
            continue
        }
    }
    $result.Add($line)
}
foreach ($key in $remaining) {
    $result.Add("$key=$($updates[$key])")
}
[IO.File]::WriteAllLines(
    $erpEnvironmentPath,
    $result,
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Configured ERP Langfuse exporter for http://127.0.0.1:3000"
