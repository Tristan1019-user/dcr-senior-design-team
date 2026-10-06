"""Check a DCR demo via serial reset or an existing log, without flash writes."""
import argparse
import json
import re
import time
from pathlib import Path

import serial


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port")
    source.add_argument("--input-log", type=Path, help="Validate an existing capture without touching hardware")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sensors", action="store_true", help="Check the 60-second physical sensor demo")
    parser.add_argument("--sensor-seconds", type=int, default=60)
    args = parser.parse_args()
    if not 60 <= args.sensor_seconds <= 3600:
        parser.error("--sensor-seconds must be 60..3600")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.input_log:
        if args.input_log.resolve() == args.output.resolve():
            parser.error("Use a separate output path to preserve the original capture")
        data = args.input_log.read_bytes()
    else:
        port = serial.Serial(port=None, baudrate=115200, timeout=0.25)
        port.dtr = False
        port.rts = False
        port.port = args.port
        data = bytearray()
        with port:
            port.reset_input_buffer()
            port.rts = True
            time.sleep(0.15)
            port.rts = False
            deadline = time.monotonic() + (args.sensor_seconds + 5 if args.sensors else 25)
            with args.output.open("wb") as capture:
                while time.monotonic() < deadline:
                    chunk = port.read(port.in_waiting or 1)
                    data.extend(chunk)
                    capture.write(chunk)
                    capture.flush()
    args.output.write_bytes(data)
    text = re.sub(r"\x1b\[[0-9;]*m", "", data.decode("utf-8", errors="replace"))
    if args.sensors:
        minimum_samples = int(args.sensor_seconds * 0.9) if args.sensor_seconds > 60 else 45
        samples = {}
        for name, field in (("sht45", "humidity_pct"), ("bmp280", "pressure_pa")):
            samples[name] = [(int(n), float(t), float(v)) for n, t, v in re.findall(
                rf"\b{name}: \[info\] sample=(\d+) temperature_c=([-\d.]+) {field}=([-\d.]+)", text)]
        expected_outputs = {
            "sht_temperature_c": [v[1] for v in samples["sht45"]],
            "humidity_pct": [v[2] for v in samples["sht45"]],
            "bmp_temperature_c": [v[1] for v in samples["bmp280"]],
            "pressure_pa": [v[2] for v in samples["bmp280"]],
        }
        outputs = {name: [float(v) for v in re.findall(
            rf"\b{name}: \[info\] value=([^\s]+)", text)] for name in expected_outputs}
        checks = {
            "clean_shutdown": text.count("DCR_SENSOR_DEMO STOPPED PAL_allocations=0") == 1,
            "no_faults": not re.search(r"Guru Meditation|panic|abort\(\)|\[error\]|DEMO FAIL|assert failed|sensor error=|backpressure", text, re.I),
            "sufficient_samples": all(len(v) >= minimum_samples for v in samples.values()),
            "consecutive": all(all(b[0] == a[0] + 1 for a, b in zip(v, v[1:])) for v in samples.values()),
            "humidity_range": all(-40 <= t <= 125 and 0 <= h <= 100 for _, t, h in samples["sht45"]),
            "pressure_range": all(-40 <= t <= 85 and 30000 <= p <= 110000 for _, t, p in samples["bmp280"]),
            "zero_sensor_errors": all(re.search(rf"\b{name}: \[info\] sensor stopped samples=\d+ errors=0\b", text) for name in samples),
            "all_numeric_outputs": all(len(re.findall(rf"\b{name}: \[info\] value=", text)) >= minimum_samples for name in
                ("sht_temperature_c", "humidity_pct", "bmp_temperature_c", "pressure_pa")),
            "outputs_match_acquisition": outputs == expected_outputs,
        }
        result = {"port": args.port, "checks": checks,
                  "sample_counts": {k: len(v) for k, v in samples.items()},
                  "passed": all(checks.values()),
                  "sensor_seconds": args.sensor_seconds,
                  "minimum_samples": minimum_samples,
                  "scope": "Bounded sensor demonstration; not calibrated accuracy, disconnect recovery or cold-power validation"}
        # regex matches are truthy objects, not JSON booleans.
        result["checks"] = {k: bool(v) for k, v in checks.items()}
        args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["passed"] else 1)
    streams = {}
    for name in ("raw_queue", "raw_signal", "scaled"):
        streams[name] = [float(v) for v in re.findall(
            rf"\b{name}: \[info\] value=([-\d.]+)", text)]
    checks = {
        "clean_shutdown": text.count("DCR_DEVICE_DEMO PASS PAL_allocations=0") == 1,
        "no_faults": not re.search(r"Guru Meditation|panic|abort\(\)|\[error\]|DCR_DEVICE_DEMO FAIL|assert failed", text, re.I),
        "sufficient_samples": all(len(v) >= 200 for v in streams.values()),
        "increasing": all(all(b > a for a, b in zip(v, v[1:])) for v in streams.values()),
        "ordered_queue": all(b == a + 1 for a, b in zip(streams["raw_queue"], streams["raw_queue"][1:])),
        "scaled_values": all(v > 0 and v % 2 == 0 and v / 2 in streams["raw_queue"] for v in streams["scaled"]),
        "signal_values": all(v in streams["raw_queue"] for v in streams["raw_signal"]),
    }
    result = {"port": args.port, "checks": checks,
              "sample_counts": {k: len(v) for k, v in streams.items()},
              "passed": all(checks.values()),
              "scope": "software demo after serial reset; not physical power-cycle or sensor validation"}
    args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
