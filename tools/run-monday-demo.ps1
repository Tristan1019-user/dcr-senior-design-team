param(
    [switch]$Mock,
    [switch]$Cycle,
    [ValidateSet('deterministic', 'model')][string]$Mode = 'deterministic',
    [string]$Port = 'auto'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'host/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project Python environment is missing; see docs/MONDAY_DEMO.md.' }
. (Join-Path $PSScriptRoot 'demo-port.ps1')
if (-not $Mock) {
    # Some CH343 drivers enumerate as PnP ports without a Win32_SerialPort entry.
    $devices = @(Get-CimInstance Win32_PnPEntity | Where-Object {
        $_.ConfigManagerErrorCode -eq 0 -and $_.Name -match '\((COM\d+)\)$'
    } | ForEach-Object {
        [pscustomobject]@{
            DeviceID = [regex]::Match($_.Name, '\((COM\d+)\)$').Groups[1].Value
            PNPDeviceID = $_.PNPDeviceID
        }
    })
    $Port = Resolve-DcrDemoPort -RequestedPort $Port -Devices $devices
    Write-Host "DCR UART port: $Port"
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$output = Join-Path $root "docs/evidence/monday-$stamp"
$arguments = @('-m', 'dcr_host.platform_demo', '--mode', $Mode, '--output', $output)
if ($Mock) { $arguments += '--mock' } else { $arguments += @('--port', $Port) }
if ($Cycle) { $arguments += '--cycle' }
if ($Mode -eq 'model' -and (-not $env:DCR_MODEL_BASE_URL -or -not $env:DCR_MODEL_NAME)) {
    throw 'Set DCR_MODEL_BASE_URL and DCR_MODEL_NAME explicitly first; no endpoint guessing.'
}
Push-Location (Join-Path $root 'host')
try {
    & $python @arguments
    $result = $LASTEXITCODE
    Write-Host "Evidence: $output"
    if ($result -ne 0) { throw 'Demo did not pass. Inspect result.json; do not repeatedly replay physical commands.' }
} finally {
    Pop-Location
}
