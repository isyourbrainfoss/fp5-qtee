"""Tests for fp5-cell-backoff's suspend re-arm (and that attempts schedule it).

Runs the real script with fake mmcli/nmcli/systemctl/loginctl/gsettings/
gdbus/busctl/systemd-run/systemd-inhibit/runuser in front of PATH and a fake
/sys tree. Nothing on the host is touched.

    python3 -m unittest scripts/cell-backoff/test_fp5_cell_backoff.py
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "fp5-cell-backoff"
# the phone runs it with busybox ash: FP5_TEST_SH="busybox sh"
SH = os.environ.get("FP5_TEST_SH", "sh").split()

FAKES = {
    # every fake appends its argv to $FAKE/calls
    "mmcli": r'''
case "$*" in
  "-L") echo "/org/freedesktop/ModemManager1/Modem/0 [fake]";;
  "-m any -K") echo "modem.3gpp.packet-service-state : $(cat "$FAKE/packet" 2>/dev/null || echo detached)"
               echo "modem.generic.state : registered";;
  *voice-list-calls*) [ -e "$FAKE/call" ] && echo "/org/freedesktop/ModemManager1/Call/1 incoming";;
esac
exit 0''',
    "nmcli": r'''case "$*" in *GENERAL.AUTOCONNECT*) echo no;; *GENERAL.STATE*) echo "30 (disconnected)";; esac; exit 0''',
    "systemctl": r'''case "$1" in is-active) exit 1;; show) echo inactive;; esac; exit 0''',
    "systemd-run": r'''exit 0''',
    "systemd-inhibit": r'''exit "$(cat "$FAKE/attempt_rc" 2>/dev/null || echo 1)"''',
    "loginctl": r'''
case "$1" in
  list-sessions) [ -e "$FAKE/session" ] && echo "2 10000 user seat0 tty7";;
  show-session) cat "$FAKE/session";;
esac
exit 0''',
    "runuser": r'''shift 3; exec "$@"''',   # -u NAME -- cmd...
    "sudo": r'''exit 1''',
    "gsettings": r'''
k=$3
f="$FAKE/gs_$k"
if [ -e "$f" ]; then cat "$f"; else
  case $k in *-type) echo "'suspend'";; *-timeout) echo 300;; esac
fi''',
    "gdbus": r'''[ -e "$FAKE/idle_ms" ] && echo "(uint64 $(cat "$FAKE/idle_ms"),)"; exit 0''',
    "busctl": r'''
case "$*" in
  *OnBattery*) echo "b $(cat "$FAKE/on_battery" 2>/dev/null || echo true)";;
  *BlockInhibited*) echo "s \"$(cat "$FAKE/block" 2>/dev/null)\"";;
esac
exit 0''',
    "logger": "exit 0",
    "ip": "exit 1",
    "flock": "exit 0",
}

SESSION_LOCKED = "Type=wayland\nActive=yes\nLockedHint=yes\nUser=10000\nName=user\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.fake = t / "fake"
        self.bin = t / "bin"
        self.sys = t / "sys"
        for d in (self.fake, self.bin, self.sys / "class/backlight/panel", self.sys / "power/suspend_stats"):
            d.mkdir(parents=True)
        for name, body in FAKES.items():
            p = self.bin / name
            p.write_text(f'#!/bin/sh\necho "{name} $*" >> "$FAKE/calls"\n{body}\n')
            p.chmod(0o755)
        self.set("bl", "4")   # panel off
        (self.sys / "power/suspend_stats/success").write_text("7\n")
        self.set("session", SESSION_LOCKED)
        self.set("idle_ms", "400000")
        self.env = dict(os.environ, FAKE=str(self.fake), FP5_CELL_TESTBIN=str(self.bin),
                        FP5_CELL_ST=str(t / "st"), FP5_CELL_SYS=str(self.sys),
                        FP5_CELL_LOCK=str(t / "lock"), FP5_CELL_NO_REARM=str(t / "no-rearm"))

    def tearDown(self):
        self.tmp.cleanup()

    def set(self, name, value):
        if name == "bl":
            (self.sys / "class/backlight/panel/bl_power").write_text(value + "\n")
        else:
            (self.fake / name).write_text(value + "\n")

    def run_script(self, *args):
        r = subprocess.run([*SH, str(SCRIPT), *args], env=self.env, capture_output=True,
                           text=True, timeout=60)
        calls = (self.fake / "calls").read_text() if (self.fake / "calls").exists() else ""
        return r, calls


class Rearm(Base):
    def rearm(self, count="7"):
        r, calls = self.run_script("rearm", count)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, calls

    def assert_suspends(self):
        out, calls = self.rearm()
        self.assertIn("systemctl suspend", calls, out)
        self.assertIn("suspending", out)

    def assert_veto(self, why, count="7"):
        out, calls = self.rearm(count)
        self.assertNotIn("systemctl suspend", calls, out)
        self.assertIn("no re-arm: " + why, out)

    def test_suspends_when_gsd_would_have(self):
        self.assert_suspends()

    def test_screen_on(self):
        self.set("bl", "0")
        self.assert_veto("screen on")

    def test_unlocked(self):
        self.set("session", SESSION_LOCKED.replace("LockedHint=yes", "LockedHint=no"))
        self.assert_veto("session not locked")

    def test_no_session(self):
        (self.fake / "session").unlink()
        self.assert_veto("no active graphical session")

    def test_inactive_or_x11_session_ignored(self):
        self.set("session", SESSION_LOCKED.replace("Active=yes", "Active=no"))
        self.assert_veto("no active graphical session")
        self.set("session", SESSION_LOCKED.replace("wayland", "tty"))
        self.assert_veto("no active graphical session")

    def test_not_idle_long_enough(self):
        self.set("idle_ms", "299999")
        self.assert_veto("idle 299s < 300 s")

    def test_idle_unknown(self):
        (self.fake / "idle_ms").unlink()
        self.assert_veto("idle time unknown")

    def test_call(self):
        self.set("call", "1")
        self.assert_veto("call present")

    def test_other_block_inhibitor(self):
        self.set("block", "shutdown:sleep")
        self.assert_veto("another sleep block inhibitor")
        self.set("block", "sleep")
        self.assert_veto("another sleep block inhibitor")

    def test_unrelated_block_inhibitors_ok(self):
        self.set("block", "handle-power-key:handle-suspend-key:handle-lid-switch")
        self.assert_suspends()

    def test_suspended_meanwhile(self):
        self.assert_veto("suspended since", count="6")
        self.assert_veto("suspended since", count="")

    def test_gsd_action_not_suspend(self):
        self.set("gs_sleep-inactive-battery-type", "'nothing'")
        self.assert_veto("gsd battery action is 'nothing'")

    def test_gsd_timeout_zero(self):
        self.set("gs_sleep-inactive-battery-timeout", "0")
        self.assert_veto("gsd battery timeout 0")

    def test_typed_timeout(self):
        self.set("gs_sleep-inactive-battery-timeout", "uint32 600")
        self.assert_veto("idle 400s < 600 s")

    def test_ac_uses_ac_settings(self):
        self.set("on_battery", "false")
        self.set("gs_sleep-inactive-ac-timeout", "900")
        self.assert_veto("idle 400s < 900 s")
        self.set("gs_sleep-inactive-ac-timeout", "300")
        self.assert_suspends()

    def test_kill_switch(self):
        Path(self.env["FP5_CELL_NO_REARM"]).write_text("")
        self.assert_veto("disabled")


    def test_check_is_dry(self):
        r, calls = self.run_script("rearm-check")
        self.assertIn("rearm: would suspend now", r.stdout)
        self.assertNotIn("systemctl suspend", calls)
        self.set("bl", "0")
        r, _ = self.run_script("rearm-check")
        self.assertIn("rearm: screen on", r.stdout)


class Schedule(Base):
    def test_failed_attempt_schedules_rearm(self):
        self.set("bl", "0")   # screen on: the timer path may attempt
        r, calls = self.run_script("timer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemd-inhibit --what=sleep --mode=block", calls)
        run = [l for l in calls.splitlines() if l.startswith("systemd-run")]
        self.assertEqual(len(run), 1, calls)
        self.assertIn("--unit=fp5-cell-rearm", run[0])
        self.assertIn("--on-active=20", run[0])
        self.assertTrue(run[0].endswith("fp5-cell-backoff rearm 7"), run[0])
        self.assertEqual((Path(self.env["FP5_CELL_ST"]) / "backoff").read_text().strip(), "900")

    def test_good_attempt_schedules_rearm(self):
        self.set("bl", "0")
        self.set("attempt_rc", "0")
        _r, calls = self.run_script("timer")
        self.assertEqual(sum(l.startswith("systemd-run") for l in calls.splitlines()), 1, calls)

    def test_no_attempt_no_rearm(self):
        # attached: no attempt, so nothing held sleep and nothing to re-arm
        self.set("packet", "attached")
        _r, calls = self.run_script("timer")
        self.assertNotIn("systemd-inhibit", calls)
        self.assertNotIn("systemd-run", calls)

    def test_kill_switch_no_timer(self):
        self.set("bl", "0")
        Path(self.env["FP5_CELL_NO_REARM"]).write_text("")
        _r, calls = self.run_script("timer")
        self.assertIn("systemd-inhibit", calls)
        self.assertNotIn("systemd-run", calls)


if __name__ == "__main__":
    unittest.main()
