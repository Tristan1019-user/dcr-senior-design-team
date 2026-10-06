param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'host/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run the Python setup in docs/SETUP.md first.' }
$output = Join-Path $root ('output/offline-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $output | Out-Null

function Invoke-Check([string]$Name, [string]$Executable, [string[]]$Arguments) {
    $command = Get-Command $Executable -ErrorAction Stop
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell treats even successful unittest stderr as an error.
        $ErrorActionPreference = 'Continue'
        & $command @Arguments *> (Join-Path $output "$Name.log")
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($code -ne 0) {
        Get-Content -LiteralPath (Join-Path $output "$Name.log") -Tail 40
        throw "$Name failed; logs: $output"
    }
    Write-Host "$Name PASS"
}

Push-Location $root
try {
    Invoke-Check 'configure' 'cmake' @('-S', '.', '-B', 'build-host', '-G', 'Visual Studio 17 2022', '-A', 'x64')
    Invoke-Check 'build' 'cmake' @('--build', 'build-host', '--config', 'Debug', '-j', '4')
    if (-not (Test-Path -LiteralPath 'build-host/Debug/dcr_control_demo.exe')) {
        throw 'Native integration executable missing; Python tests would skip it.'
    }
    Invoke-Check 'native' 'ctest' @('--test-dir', 'build-host', '-C', 'Debug', '--output-on-failure')
    Invoke-Check 'python' $python @('-m', 'pytest', '-q', 'host/tests', '--junitxml', (Join-Path $output 'python.xml'))
    [xml]$tests = Get-Content -Raw -LiteralPath (Join-Path $output 'python.xml')
    if (@($tests.SelectNodes('//testcase/skipped')).Count -ne 0) { throw 'Python tests skipped; inspect python.xml.' }
    Invoke-Check 'capture-checker' $python @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_device_checker.py')
    Invoke-Check 'port-selection' 'powershell' @('-NoProfile', '-File', './tools/test-demo-port.ps1')
    Invoke-Check 'offline-demo' 'powershell' @('-NoProfile', '-File', './tools/run-monday-demo.ps1', '-Mock', '-Cycle')
    Write-Host "Offline checks passed. Logs: $output"
} finally {
    Pop-Location
}
