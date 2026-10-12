#!/usr/bin/env python3
"""The boot unit stays in the repo. This host is not the phone kernel."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "load-qcomtee.sh"
UNIT = ROOT / "scripts" / "fp5-qtee-load.service"


class BootUnitTest(unittest.TestCase):
    def test_wrong_kernel_exits_before_insmod(self) -> None:
        proc = subprocess.run(
            ["sh", str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("kernel ", proc.stdout)
        self.assertNotIn("INSMOD_OK", proc.stdout + proc.stderr)
        self.assertNotIn("insmod", proc.stdout + proc.stderr)

    def test_script_still_refuses_the_old_driver(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("7.2.0-nfc-test+", text)
        self.assertIn("qsee_fingerpr is loaded", text)
        self.assertIn("qcomtee already loaded", text)
        self.assertIn("sudo -n", text)
        self.assertIn('id -u', text)
        # The kernel check stays ahead of any insmod.
        self.assertLess(text.index("exit 1"), text.index("insmod"))

    def test_unit_is_a_system_oneshot_and_not_a_user_unit(self) -> None:
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", text)
        self.assertIn("RemainAfterExit=yes", text)
        self.assertIn("After=local-fs.target", text)
        self.assertIn(
            "ExecStart=/home/user/fp5-qtee-keep/load-qcomtee.sh", text
        )
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertNotIn("%h", text)
        self.assertNotIn("default.target", text)


if __name__ == "__main__":
    unittest.main()
