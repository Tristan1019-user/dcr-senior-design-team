# Dynamic Component Runtime

This is the starting point for our Vanderbilt senior design project. I put this
repo together so we can try the demo, look through the code, and decide what to
build next.

The idea is to let different devices describe what they can do and work together
through a common interface. An AI model can help choose actions, but it does not
get direct access to GPIO or replace the device's control loop. The host checks
requests, and the device handles its own timing and I/O.

Right now, that works with an ESP32-S3, two sensors, a Python host, and a simulated
position stage. It is a useful starting point for connecting more hardware, not
a finished robot-control system.

## Start here

- [Set up and run the tests](docs/SETUP.md)
- [Run the demo](docs/MONDAY_DEMO.md)
- [What I want us to discuss](docs/TEAM_REVIEW.md)
- [How the parts fit together](docs/HOW_IT_WORKS.md)
- [What works and what is still missing](docs/STATUS.md)

Once the Python environment is set up, this runs without any hardware or model:

```powershell
./tools/run-monday-demo.ps1 -Mock -Cycle
```

## What is working

- **C runtime:** components are compiled into the firmware and connected through
  JSON configuration. They have start/stop behavior, typed signals and queues,
  timeout handling, and checks on how they can be connected. This does not load
  new executable code at runtime.
- **Sensors:** SHT45 temperature/humidity and BMP280 temperature/pressure, each
  on its own I2C controller. One sensor can be stopped and restarted while the
  other keeps running.
- **Python host:** discovers supported device functions, reads values and their
  age, checks health, records runs, and allows specific sensor operations when
  explicitly enabled.
- **Model connection:** an OpenAI-compatible server can use the same checked
  tools as the scripted demo. The endpoint and model are configurable.
- **End-to-end demo:** a sensor reading determines a target for a simulated
  stage. The host checks the result instead of treating the model's answer as
  proof that something happened.

The sensor readings can be real; the stage is currently simulated. There is no
physical actuator or trained movement policy yet. A failed connection does not
silently switch the demo to fake data.

## Why these choices

C and FreeRTOS keep device timing and resource ownership on the ESP32. Python
makes it easier to connect devices, test workflows, and swap model backends.
Typed messages give both sides something concrete to check. Keeping the model
outside the low-level control loop also lets us compare it with a scripted
baseline and see where it actually helps.

## Where to look in the code

| Folder | Contents |
|---|---|
| `dcr/` | C runtime, lifecycle, configuration, ports, and connection checks |
| `components/` | Sensors, I2C workers, software components, and serial control |
| `platform/` | Windows/POSIX and ESP32 platform support |
| `main/` | Host and firmware entry points |
| `examples/` | JSON component configurations |
| `host/dcr_host/` | Python device interfaces, model tools, recording, and demo |
| `host/tests/`, `tests/` | Python, native C, and capture-checker tests |
| `tools/` | Build, demo, and test scripts |
| `docs/` | Setup, wiring, design notes, protocol, and next steps |

The verified host setup is Windows. There is a POSIX implementation, but this
release has not been tested on Linux. See [test results](docs/TEST_RESULTS.md)
for the checks run on this copy.

## Next step

I would like us to pick one real actuator with feedback and one task we can
measure. Then we can compare scripted and model-driven runs, disconnect
something deliberately under controlled conditions, and see whether the system
recovers correctly. A second kind of controller would be more useful than just
adding more sensors to this board.

This is the public team copy. The separate private repo keeps development
experiments and raw records. Sponsor PDFs, firmware backups, private endpoint
details, and old presentation exports are not included here. No open-source
license has been selected for our code yet; we should agree on that with the
team and sponsor. The vendored cJSON license is kept in `third_party/cjson/`.
