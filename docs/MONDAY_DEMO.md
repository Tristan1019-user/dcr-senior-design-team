# Demo guide

All commands below run from the repository root after [setup](SETUP.md).
The script keeps its original `run-monday-demo.ps1` name so existing commands
still work.

## Without hardware

```powershell
./tools/run-monday-demo.ps1 -Mock -Cycle
```

Both sensors and the stage are simulated. This is the easiest way to learn the
flow and check the host installation. It does not test a physical board.

## With the ESP32-S3

Use a board with the **control firmware**, not the timed sensor-only image. Plug
into the **CH343 UART connector**, not the native USB/JTAG connector. Close any
serial monitor first; only one program can own the port.

```powershell
./tools/run-monday-demo.ps1
```

The launcher selects a single matching CH343 bridge. If more than one is present,
specify the correct port, for example `-Port COM5`. COM numbers are machine-specific.
See [wiring](SENSOR_DEMO.md) before connecting a new setup.

This reads the physical sensors and uses the simulated stage. Add `-Cycle` only
when you intend to stop, initialize, and restart SHT45 while checking that BMP280
keeps sampling. It does not reflash the board or intentionally reset it.

## With a model

Set these **before** launching the demo. Replace both placeholders with the
endpoint and model ID supplied by the server operator:

```powershell
$env:DCR_MODEL_BASE_URL = 'http://model-host.example:8080/v1'
$env:DCR_MODEL_NAME = 'your-served-model-id'
./tools/run-monday-demo.ps1 -Mode model
```

Add `-Mock` for simulated sensors or `-Cycle` for the sensor lifecycle test.
An API key, if required, belongs in `DCR_MODEL_API_KEY`, not a committed file.
`.env.example` is a reference and is not loaded automatically. A served alias
identifies the server's model selection, not independently verified model weights.
Do not send private observations to an outside service without reviewing them.

## What to show

1. The host discovers the available devices and readings.
2. It reads temperature with units, age, health, and a sequence number.
3. The scripted workflow or model chooses a stage target from that reading.
4. The action is checked, applied to the **simulated** stage, and read back.
5. The run also checks rejection of an invalid policy request and offline replay
   of the recorded prefix. `-Cycle` adds the sensor independence check.

Open the printed recording folder and inspect `result.json`. Look for
`passed: true`, the correct `sensor_source`, and `physical_actuation: false`.
`trained_policy` and `world_model` should also be false in the current demo.
Model mode additionally checks the actual tool trace; a confident text answer
alone is not a pass.

## When something fails

- Failure during `connect`: check the UART connector, port, control firmware,
  and whether another serial program has the port open.
- Model configuration failure: check the variables in the same shell that
  launches the script. Setting them afterward does not change a completed run.
- Uncertain command outcome: inspect the current device state and saved result.
  Do not repeatedly replay physical commands to force a pass.
- No board available: use `-Mock` explicitly and present it as simulation.

Each run creates a new folder under `docs/evidence/`. Review it before sharing;
raw recordings can contain device identifiers and local details.
