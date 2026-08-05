$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$erpEnvironmentPath = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $erpEnvironmentPath)) {
    throw "ERP environment file was not found: $erpEnvironmentPath"
}

$keys = @("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
$result = foreach ($line in [IO.File]::ReadAllLines($erpEnvironmentPath)) {
    $matchedKey = $keys | Where-Object { $line -match "^$($_)=" } | Select-Object -First 1
    if ($matchedKey) {
        "$matchedKey="
    }
    else {
        $line
    }
}
[IO.File]::WriteAllLines(
    $erpEnvironmentPath,
    $result,
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Disabled ERP-to-Langfuse export. Local SQL tracing remains enabled."
