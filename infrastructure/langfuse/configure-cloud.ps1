param(
    [ValidateSet("eu", "us")]
    [string]$Region = "eu",
    [string]$PublicKey,
    [string]$SecretKey
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$erpEnvironmentPath = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $erpEnvironmentPath)) {
    throw "ERP environment file was not found: $erpEnvironmentPath"
}

if (-not $PublicKey) {
    $PublicKey = Read-Host "Langfuse project public key (pk-lf-...)"
}
if (-not $SecretKey) {
    $secureSecret = Read-Host "Langfuse project secret key (sk-lf-...)" -AsSecureString
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
    try {
        $SecretKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
}
if ($PublicKey -notmatch "^pk-lf-") {
    throw "Invalid Langfuse public key prefix. Expected pk-lf-."
}
if ($SecretKey -notmatch "^sk-lf-") {
    throw "Invalid Langfuse secret key prefix. Expected sk-lf-."
}

$baseUrl = if ($Region -eq "us") {
    "https://us.cloud.langfuse.com"
}
else {
    "https://cloud.langfuse.com"
}
$updates = [ordered]@{
    LANGFUSE_BASE_URL = $baseUrl
    LANGFUSE_PUBLIC_KEY = $PublicKey.Trim()
    LANGFUSE_SECRET_KEY = $SecretKey.Trim()
    LANGFUSE_ENVIRONMENT = "development"
    LANGFUSE_TIMEOUT_SECONDS = "5"
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
Write-Host "Configured Langfuse Cloud ($Region) without printing either API key."
