param(
    [string]$IdfPath = "$HOME\Tools\esp-idf-v5.5.3",
    [string]$ToolsPath = "$HOME\Tools\espressif",
    [switch]$Sensors,
    [switch]$Control,
    [ValidateRange(60,3600)][int]$SensorSeconds = 60
)
$ErrorActionPreference = 'Stop'
if ($Sensors -and $Control) { throw 'Choose either -Sensors or -Control, not both.' }
$env:IDF_PATH = $IdfPath
$env:IDF_TOOLS_PATH = $ToolsPath
$python = Join-Path $ToolsPath 'python_env\idf5.5_py3.13_env\Scripts\python.exe'
if (!(Test-Path $python)) { throw 'Install ESP-IDF Python environment first; see README.' }
$priorPath = $env:PATH
$exports = & $python "$IdfPath\tools\idf_tools.py" export --format key-value
if ($LASTEXITCODE) { throw 'ESP-IDF tool export failed.' }
foreach ($line in $exports) {
    if ($line -match '^([A-Z_]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Replace('%PATH%', $priorPath), 'Process')
    }
}
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $build = if ($Sensors) { 'build-sensors' } elseif ($Control) { 'build-control' } else { 'build-esp32' }
    $sensorFlag = if ($Sensors) { 'ON' } else { 'OFF' }
    $controlFlag = if ($Control) { 'ON' } else { 'OFF' }
    & $python "$IdfPath\tools\idf.py" -B $build -D DCR_SENSOR_DEMO=$sensorFlag -D DCR_CONTROL=$controlFlag -D DCR_SENSOR_SECONDS=$SensorSeconds -D DCR_ESP32=ON -D IDF_TARGET=esp32s3 build
    if ($LASTEXITCODE) { throw 'ESP32-S3 build failed.' }
} finally { Pop-Location }
