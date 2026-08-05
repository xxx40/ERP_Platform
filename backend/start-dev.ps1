param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8001,
    [ValidateRange(1, 65535)]
    [int]$OrderServicePort = 8101
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found: $python"
}

$orderServiceRoot = Join-Path $projectRoot "purchase_order_service"
$orderServiceUrl = "http://127.0.0.1:$OrderServicePort"
$env:PURCHASE_ORDER_PROVIDER = "http"
$env:PURCHASE_ORDER_API_BASE_URL = $orderServiceUrl

$orderService = Start-Process `
    -FilePath $python `
    -ArgumentList @(
        "-m", "uvicorn", "order_service.main:app",
        "--host", "127.0.0.1",
        "--port", $OrderServicePort,
        "--app-dir", $orderServiceRoot
    ) `
    -WindowStyle Hidden `
    -PassThru

try {
    $ready = $false
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        if ($orderService.HasExited) {
            throw "Unified purchase data service exited during startup with code $($orderService.ExitCode)."
        }
        try {
            $health = Invoke-RestMethod -Uri "$orderServiceUrl/api/v1/health" -TimeoutSec 1
            if ($health.status -eq "ok" -and $health.service -eq "unified-purchase-data-api") {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw "Unified purchase data service did not become ready at $orderServiceUrl."
    }

    Write-Host "Unified purchase data service: $orderServiceUrl"
    Write-Host "ERP assistant backend: http://127.0.0.1:$BackendPort"
    Set-Location $PSScriptRoot
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port $BackendPort
}
finally {
    if (-not $orderService.HasExited) {
        Stop-Process -Id $orderService.Id
    }
}
