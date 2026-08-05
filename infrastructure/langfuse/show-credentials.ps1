$ErrorActionPreference = "Stop"

$environmentPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $environmentPath)) {
    throw "Credentials do not exist. Run initialize.ps1 first."
}
$values = @{}
foreach ($line in [IO.File]::ReadAllLines($environmentPath)) {
    if ($line -match "^([^#=]+)=(.*)$") {
        $values[$matches[1]] = $matches[2]
    }
}

[pscustomobject]@{
    Url = "http://127.0.0.1:3000"
    Email = $values["LANGFUSE_INIT_USER_EMAIL"]
    Password = $values["LANGFUSE_INIT_USER_PASSWORD"]
    PublicKey = $values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
    SecretKey = $values["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
} | Format-List
