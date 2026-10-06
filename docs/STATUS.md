# Current status

This describes the public team snapshot prepared October 6, 2026. New offline
results are in [TEST_RESULTS.md](TEST_RESULTS.md); the hardware measurements below
come from earlier runs, not a new hardware test during publication.

## Implemented

- C component registry, JSON configuration, lifecycle, typed signals/queues,
  graph validation, watchdog fault reporting, and allocation-failure tests.
- ESP32 and Windows platform support; POSIX code is present but not verified here.
- I2C bus workers, private client replies, SHT45/BMP280 acquisition, deadline-based
  sampling, CRC/identity/calibration checks, and bounded waits.
- Immutable `connection_locked` rules. Other proposed safety-rule types are not
  implemented and are rejected rather than silently accepted.
- Serial control, discovery, health/freshness checks, explicit sensor permissions,
  model tools, recording, and limited offline replay.
- A real-sensor/simulated-stage demo with independent result checks.

## Earlier hardware results

The private experiment records retain the raw measurements and failures:

- September 18: a five-minute sensor run recorded 296 reads per sensor without
  reported sensor faults. This is a bounded run, not a long-term reliability claim.
- September 19: three model-backed tasks with real sensor input and a simulated
  stage took 17.469, 17.719, and 17.656 seconds.
- September 30: moving to the UART connector fixed a failed serial connection.
  The model-backed cycle completed in 30.532 seconds with eight healthy components,
  unchanged configuration, and the same boot identifier.

These are whole-demo times, not model inference benchmarks. Sensor protocol
operation does not establish calibrated measurement accuracy. The named SHT45
part relies on the hardware identification; the protocol alone identifies SHT4x
compatibility.

## Still missing

- A real actuator, feedback, independent stop, and lost-link behavior.
- A second physical controller type and a second tested live model backend.
- Command/recovery state that persists across host restarts.
- General SPI/UART resource adapters and additional verified platform targets.
- The rest of the proposed safety-rule engine, with clearly defined behavior.
- Cold-power, absent-sensor, and longer-run physical tests. The deferred checks
  have not been counted as passes.
- Vision, learned motion policies, and world models.

The C runtime and host are prototypes, not a certified safety controller. Neither
Python nor a language model belongs in a hard real-time motor safety loop.

## Next steps

First agree on hardware and one measurable task. Then add independent feedback
and recovery, test the scripted baseline, and compare model runs under the same
conditions. Expansion ideas are in [TEAM_REVIEW.md](TEAM_REVIEW.md).

The detailed sponsor requirement mapping and original documents remain in the
private development archive. This public status page is not a claim that the
entire proposal has been completed.
