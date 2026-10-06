"""Synthetic acceptance-checker tests; these are NOT physical sensor evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def good_log(count=50):
    lines = []
    for n in range(1, count + 1):
        lines += [f"sht45: [info] sample={n} temperature_c=25.0 humidity_pct=50.0",
                  f"bmp280: [info] sample={n} temperature_c=25.0 pressure_pa=100653.0"]
        for name, value in (("sht_temperature_c", 25.0), ("humidity_pct", 50.0),
                            ("bmp_temperature_c", 25.0), ("pressure_pa", 100653.0)):
            lines.append(f"{name}: [info] value={value}")
    for name in ("sht45", "bmp280"):
        lines.append(f"{name}: [info] sensor stopped samples={count} errors=0")
    lines.append("DCR_SENSOR_DEMO STOPPED PAL_allocations=0")
    return "\n".join(lines)


class CheckerTests(unittest.TestCase):
    def check_log(self, text, passes, seconds=60):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.log"
            output = Path(folder) / "checked.log"
            source.write_text(text, encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "tools/check-device.py"),
                                     "--sensors", "--sensor-seconds", str(seconds), "--input-log", str(source), "--output", str(output)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0 if passes else 1, result.stderr)
            self.assertEqual(json.loads(output.with_suffix(".json").read_text())["passed"], passes)
            self.assertEqual(source.read_text(encoding="utf-8"), text)

    def test_valid_synthetic_capture(self):
        self.check_log(good_log(), True)

    def test_short_capture_cannot_pass_soak(self):
        self.check_log(good_log(), False, seconds=300)

    def test_full_synthetic_soak(self):
        self.check_log(good_log(296), True, seconds=300)

    def test_bad_captures(self):
        valid = good_log()
        mutations = {
            "empty": "",
            "missing_sensor": valid.replace("bmp280:", "absent:"),
            "missing_logger": valid.replace("humidity_pct: [info] value=", "absent: [info] value="),
            "crc_error": valid + "\nsht45: [warn] sensor error=crc count=1",
            "pressure_range": valid.replace("pressure_pa=100653.0", "pressure_pa=200000.0"),
            "sequence_gap": valid.replace("sample=25 ", "sample=27 "),
            "no_shutdown": valid.replace("DCR_SENSOR_DEMO STOPPED", "unfinished"),
            "leak": valid.replace("PAL_allocations=0", "PAL_allocations=1"),
            "backpressure": valid + "\noutput backpressure",
            "panic": valid + "\nGuru Meditation",
            "prior_error": valid.replace("errors=0", "errors=1"),
            "double_boot": valid + "\nDCR_SENSOR_DEMO STOPPED PAL_allocations=0",
            "wrong_logger_value": valid.replace("pressure_pa: [info] value=100653.0", "pressure_pa: [info] value=25.0"),
            "nonfinite_logger": valid.replace("humidity_pct: [info] value=50.0", "humidity_pct: [info] value=nan"),
        }
        for name, text in mutations.items():
            with self.subTest(name=name):
                self.check_log(text, False)


if __name__ == "__main__":
    unittest.main()
