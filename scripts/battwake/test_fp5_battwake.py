"""Tests for fp5-battwake-apply on a fake /sys tree (nothing on the host is touched).

    python3 -m unittest scripts/battwake/test_fp5_battwake.py
    FP5_TEST_SH="busybox sh" ...   # as on the phone
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "fp5-battwake-apply"
SH = os.environ.get("FP5_TEST_SH", "sh").split()


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.bat = t / "sys/class/power_supply/qcom-battmgr-bat"
        (self.bat / "power").mkdir(parents=True)
        self.bin = t / "bin"
        self.bin.mkdir()
        (self.bin / "logger").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "logger").chmod(0o755)
        self.conf = t / "conf"
        self.env = dict(os.environ, FP5_BATTWAKE_SYS=str(t / "sys"), FP5_BATTWAKE_CONF=str(self.conf),
                        PATH=f"{self.bin}:{os.environ['PATH']}")
        self.state("enabled", 69)

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, wakeup, cap):
        (self.bat / "power/wakeup").write_text(wakeup + "\n")
        (self.bat / "capacity").write_text(f"{cap}\n")

    def run_apply(self, *args):
        r = subprocess.run([*SH, str(SCRIPT), *args], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return (self.bat / "power/wakeup").read_text().strip(), r.stdout

    def test_auto_disables_above_low(self):
        self.assertEqual(self.run_apply()[0], "disabled")

    def test_auto_enables_at_low(self):
        self.state("disabled", 10)
        self.assertEqual(self.run_apply("auto")[0], "enabled")

    def test_low_configurable(self):
        self.conf.write_text("LOW=5\n")
        self.state("enabled", 8)
        self.assertEqual(self.run_apply()[0], "disabled")

    def test_explicit(self):
        self.state("disabled", 3)
        self.assertEqual(self.run_apply("disable")[0], "disabled")
        self.assertEqual(self.run_apply("enable")[0], "enabled")

    def test_status_is_dry(self):
        w, out = self.run_apply("status")
        self.assertEqual(w, "enabled")
        self.assertIn("wakeup=enabled capacity=69%", out)

    def test_bad_capacity_treated_as_full(self):
        self.state("enabled", "x")
        self.assertEqual(self.run_apply()[0], "disabled")

    def test_missing_supply_is_noop(self):
        import shutil
        shutil.rmtree(self.bat)
        r = subprocess.run([*SH, str(SCRIPT)], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
