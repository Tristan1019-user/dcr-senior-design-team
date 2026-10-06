# Setup

The tested setup is Windows, Python 3.12, CMake 3.20 or newer, and Visual Studio
2022 with **Desktop development with C++** installed. ESP-IDF 5.5.3 is needed
only for firmware builds. You do not need a board, GPU, model server, or account
to run the offline demo and tests.

## Get the code and Python packages

```powershell
git clone https://github.com/Tristan1019-user/dcr-senior-design-team.git
cd dcr-senior-design-team
py -3.12 -m venv host/.venv
./host/.venv/Scripts/python.exe -m pip install -e ./host "pytest>=8,<10" "pytest-asyncio>=0.24,<2"
```

If the Python launcher is not installed, use the path to your Python 3.12
executable instead of `py -3.12`. With uv, `uv sync --project host --frozen` is an
alternative; `host/uv.lock` contains the resolved dependencies. Use one approach,
not both. Calling the interpreter directly avoids needing to activate the venv.

## Check the whole offline setup

From the repo root:

```powershell
powershell -NoProfile -File ./tools/verify-offline.ps1
```

This builds the C runtime, runs the C and Python tests, checks the serial-port
selector with fake port records, and runs a fully simulated demo. It does not
open a serial port, call a model, or flash firmware. Results go to a new
`output/offline-*` folder, and the demo prints its own recording folder.

The individual commands are:

```powershell
cmake -S . -B build-host -G "Visual Studio 17 2022" -A x64
cmake --build build-host --config Debug -j 4
ctest --test-dir build-host -C Debug --output-on-failure
./host/.venv/Scripts/python.exe -m pytest -q host/tests
./host/.venv/Scripts/python.exe -m unittest discover -s tests -p test_device_checker.py
powershell -NoProfile -File ./tools/test-demo-port.ps1
./tools/run-monday-demo.ps1 -Mock -Cycle
```

Build before running Python tests. The native integration test specifically uses
`build-host/Debug/dcr_control_demo.exe`; without it, that test skips. Other build
generators need a corresponding test-path change. The POSIX code is included,
but Linux is not a verified setup for this release.

If Windows blocks a local PowerShell script, review it first, then run it with
`powershell -NoProfile -ExecutionPolicy Bypass -File <script-path>`. That applies
to the launched process, not a machine-wide policy change.

## Add hardware or a model later

Follow the [demo guide](MONDAY_DEMO.md) for a board that already has the control
firmware. See [wiring and firmware](SENSOR_DEMO.md) for a fresh board. A successful
offline test does not establish that a new board is wired or flashed correctly.
