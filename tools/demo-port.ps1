function Resolve-DcrDemoPort {
    param(
        [string]$RequestedPort = 'auto',
        [object[]]$Devices = @()
    )
    if ($RequestedPort -eq 'auto') {
        $bridges = @($Devices | Where-Object { $_.PNPDeviceID -match 'VID_1A86&PID_55D3' })
        if ($bridges.Count -eq 1) { return $bridges[0].DeviceID }
        if ($bridges.Count -gt 1) {
            throw 'Multiple CH343 bridges detected. Select the DCR board explicitly with -Port COMx.'
        }
        $visible = ($Devices | ForEach-Object { $_.DeviceID }) -join ', '
        throw "No CH343 UART bridge detected (visible ports: $visible). Connect the board's UART USB connector, not its native USB/JTAG connector."
    }
    $device = @($Devices | Where-Object { $_.DeviceID -eq $RequestedPort })
    if ($device.Count -ne 1) { throw "Serial port $RequestedPort is not present. Reconnect the board and use -Port auto." }
    if ($device[0].PNPDeviceID -match 'VID_303A&PID_1001') {
        throw "$RequestedPort is native ESP32 USB/JTAG. This DCR firmware receives commands on UART0 through the CH343 bridge. Move the cable to the UART USB connector and use -Port auto."
    }
    return $device[0].DeviceID
}
