# Sensors and firmware

The existing setup uses an ESP32-S3, an SHT45, and a **BMP280**, not a BME280.
The pin choices carry over from the earlier working sensor project.

## Wiring

Disconnect power before changing wiring. Use GPIO labels, not header-position
numbers, and check the breakout's power-pin label. Use 3.3 V-compatible power
and pull-ups; never put 5 V on the ESP32's I2C pins. Both sensors need common GND.

| Sensor | SDA | SCL | Controller | Addresses checked |
|---|---|---|---|---|
| SHT45 | GPIO8 | GPIO9 | I2C0 | 0x44, 0x45 |
| BMP280 | GPIO10 | GPIO11 | I2C1 | 0x76, 0x77 |

Each bus runs at 100 kHz and needs external pull-ups to 3.3 V. Check whether the
breakouts already include them before adding more. Firmware disables weak
internal pull-ups. If the BMP280 breakout exposes CSB/SDO, CSB must select I2C
(high); SDO chooses 0x76 (low) or 0x77 (high).

## Readings and failure behavior

SHT45 publishes temperature in degrees C and relative humidity in percent.
BMP280 publishes temperature in degrees C and absolute pressure in Pa. Divide
pressure by 100 for hPa; it is not sea-level-corrected pressure.

The driver checks SHT4x CRC, BMP280 ID 0x58, calibration, conversion time, and
value ranges. BME280 ID 0x60 is rejected. Failed reads do not publish fresh zeros.
The last numeric value may remain visible, so consumers must check status and age.

| Status | Meaning |
|---|---|
| 0 | OK |
| 1 | I/O failure |
| 2 | CRC failure |
| 3 | Wrong device ID |
| 4 | Invalid calibration |
| 5 | Timeout |
| 6 | Out of range |

Status and numeric ports are not an atomic multi-value snapshot. Status 0 by
itself is not enough: the component must be running and its readings fresh.

Sampling defaults to 1000 ms; the control demo permits 250-2000 ms. Tasks retain
an absolute next deadline so conversion time does not accumulate as drift.
Overruns skip missed periods instead of producing a burst of catch-up readings.
SDK transfers and client waits are bounded. One failed sensor does not inherently
stop the other I2C controller.

## Firmware variants

| Build | Purpose |
|---|---|
| Default software demo | Host-style component fan-out on the ESP32 |
| `-Sensors` | Timed sensor diagnostic, normally 60 seconds, then clean shutdown |
| `-Control` | Continuously running sensor graph with host serial control |

Use **control firmware** for the Python demo. On Windows with the existing
ESP-IDF 5.5.3 tool layout:

```powershell
./tools/build-esp32.ps1 -Control
```

The helper accepts `-IdfPath` and `-ToolsPath`; inspect its Python-environment
path before using a differently installed toolchain. Building does not flash.
Alternatively, from an activated ESP-IDF shell at the repository root:

```powershell
idf.py -B build-control -D DCR_ESP32=ON -D DCR_CONTROL=ON -D DCR_SENSOR_DEMO=OFF -D IDF_TARGET=esp32s3 build
```

Check the board, flash size, image variant, and generated flash arguments before
flashing a new board. The existing Windows helper and recorded builds used
ESP-IDF 5.5.3. This public packaging pass does not claim a new firmware cross-build.

Use the CH343 **UART USB connector** for host control. Native USB/JTAG is not
the command transport in this firmware. See [the demo guide](MONDAY_DEMO.md).

## Physical checks still to do

Cold-power and sensor-absent tests need a person and a controlled wiring procedure.
Power down before removing or reconnecting a breakout; do not hot-plug a bare
breadboard. Record the other sensor's progress and restored operation separately.
Keep these tests marked pending until the captures establish what happened.

`tools/check-device.py --help` describes the timed-capture checker. Its live mode
resets the board; `--input-log` checks an existing capture without opening a port.
Use different input/output paths to preserve the original record.
