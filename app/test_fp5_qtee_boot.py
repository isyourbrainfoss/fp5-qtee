#!/usr/bin/env python3
"""The boot unit stays in the repo. This host is not the phone kernel."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "load-qcomtee.sh"
UNIT = ROOT / "scripts" / "fp5-qtee-load.service"


class BootUnitTest(unittest.TestCase):
    def test_wrong_kernel_exits_before_insmod(self) -> None:
        env = dict(os.environ, QCOMTEE_BRIDGE_KO="/nonexistent/qcomtee-bridge.ko")
        proc = subprocess.run(
            ["sh", str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("kernel has no qcomtee", proc.stdout)
        self.assertNotIn("INSMOD_OK", proc.stdout + proc.stderr)
        self.assertNotIn("insmod", proc.stdout + proc.stderr)

    def test_plan(self) -> None:
        def plan(*args: str) -> str:
            proc = subprocess.run(
                ["sh", str(SCRIPT), "--plan", *args],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        # bridge_ko fingerpr module tee0 driver class
        self.assertEqual(plan("1", "0", "0", "0", "0", "1"), "insmod")
        self.assertEqual(plan("0", "0", "0", "1", "1", "1"), "builtin")
        self.assertEqual(plan("0", "0", "0", "0", "1", "1"), "builtin")
        self.assertEqual(plan("1", "1", "0", "1", "1", "1"), "refuse-fingerpr")
        self.assertEqual(plan("0", "0", "1", "1", "1", "1"), "already")
        # OPTEE's /sys/class/tee without qcomtee is not enough
        self.assertEqual(plan("0", "0", "0", "0", "0", "1"), "refuse-kernel")
        self.assertEqual(plan("0", "0", "0", "0", "0", "0"), "refuse-kernel")

    def test_script_still_refuses_the_old_driver(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("uname", text)
        self.assertIn("qsee_fingerpr is loaded", text)
        self.assertIn("qcomtee already loaded", text)
        self.assertIn("qcomtee built-in", text)
        self.assertIn("sudo -n", text)
        self.assertIn("id -u", text)

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
