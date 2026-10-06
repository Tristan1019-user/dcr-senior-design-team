$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'demo-port.ps1')
$bridge = [pscustomobject]@{ DeviceID = 'COM4'; PNPDeviceID = 'USB\VID_1A86&PID_55D3\TEST' }
$native = [pscustomobject]@{ DeviceID = 'COM5'; PNPDeviceID = 'USB\VID_303A&PID_1001\TEST' }
$second = [pscustomobject]@{ DeviceID = 'COM6'; PNPDeviceID = 'USB\VID_1A86&PID_55D3\SECOND' }
function Assert-Rejected {
    param([scriptblock]$Action, [string]$Expected)
    $message = $null
    try { & $Action | Out-Null } catch { $message = $_.Exception.Message }
    if (-not $message -or $message -notlike "*$Expected*") { throw "Expected rejection containing: $Expected" }
}
if ((Resolve-DcrDemoPort -Devices @($bridge, $native)) -ne 'COM4') { throw 'Wrong automatic bridge' }
if ((Resolve-DcrDemoPort -RequestedPort COM4 -Devices @($bridge, $native)) -ne 'COM4') { throw 'Wrong explicit bridge' }
if ((Resolve-DcrDemoPort -RequestedPort COM6 -Devices @($bridge, $second)) -ne 'COM6') { throw 'Explicit bridge selection failed' }
Assert-Rejected { Resolve-DcrDemoPort -Devices @() } 'No CH343'
Assert-Rejected { Resolve-DcrDemoPort -Devices @($native) } 'UART USB connector'
Assert-Rejected { Resolve-DcrDemoPort -Devices @($bridge, $second) } 'Multiple CH343'
Assert-Rejected { Resolve-DcrDemoPort -RequestedPort COM5 -Devices @($native) } 'native ESP32 USB/JTAG'
Assert-Rejected { Resolve-DcrDemoPort -RequestedPort COM9 -Devices @($bridge) } 'not present'
Write-Host '8 demo-port checks passed; no serial device opened.'
