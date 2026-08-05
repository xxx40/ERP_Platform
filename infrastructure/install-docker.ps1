$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdministrator) {
    throw "Run this script from PowerShell as Administrator."
}

$installer = Join-Path -Path ${env:TEMP} -ChildPath "DockerDesktopInstaller.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Docker Desktop installer was not found: $installer"
}

$expectedHash = "a5b5837542f2f57fadbb09db90a60c84f8efc0a65f8d6dcd2e5b9fca3a2b87e6"
$actualHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "Docker Desktop installer SHA-256 verification failed."
}

Write-Host "Installer hash verified. Installing Docker Desktop..."
& $installer install --quiet --accept-license --backend=wsl-2
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop installation failed with exit code: $LASTEXITCODE"
}

Write-Host "Docker Desktop installation completed. Restart Windows if requested."
