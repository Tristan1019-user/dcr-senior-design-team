# Tests for the public copy

Checked October 6, 2026, on Windows with Python 3.12.11, CMake, and the Visual
Studio 2022 x64 Debug build. The Python environment was created fresh in this
checkout; it was not borrowed from the private project.

| Check | Result |
|---|---|
| Native C runtime and sensor protocol tests | 8 of 8 CTest targets passed |
| Python host tests, including native-C integration | 175 passed, none skipped |
| Device-capture checker tests | 4 passed |
| Serial-port selection tests with fake port records | 8 passed |
| Simulated demo with sensor cycle | Passed |
| Python dependency check | No broken requirements |

The demo rejected an invalid policy request, preserved its configuration and
boot identifier, and passed its limited offline replay check. Both the sensor
input and stage were synthetic in this run.

Run the same checks from the repo root with:

```powershell
powershell -NoProfile -File ./tools/verify-offline.ps1
```

The script keeps new logs under `output/offline-*` and refuses to count skipped
Python tests as a full pass. Local logs are not committed because they contain
machine-specific paths. The first wrapper run exposed a Windows PowerShell
stderr-handling issue with unittest; the wrapper was corrected and the complete
run then passed. No runtime code was changed for that fix.

No board was opened, reset, or flashed, and no model service was contacted during
these checks. Firmware cross-builds, physical behavior, Linux, and model quality
are not established by this offline run. Earlier measured hardware results are
listed separately in [STATUS.md](STATUS.md).
