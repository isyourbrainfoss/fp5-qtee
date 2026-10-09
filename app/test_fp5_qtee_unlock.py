#!/usr/bin/env python3
"""Decisions for the Phosh unlock watcher. Run: python3 test_fp5_qtee_unlock.py"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fp5_qtee_unlock
from fp5_qtee_unlock import (
    LockSignals,
    Throttle,
    UnlockPolicy,
    UnlockWatcher,
    WarmLoop,
    WarmSession,
    armed,
    choose_phosh,
    feedback_args,
    hit_fid,
    is_down,
    is_locked,
    lock_signal,
    lockout_state_for,
    miss_kind,
    parse_locked,

    notify_args,
    pump_lines,
    screen_is_on,
    wake_press,
)


LIVE: list["FakeProc"] = []


class FakeProc:
    """A session that prints the given lines, then waits silently."""

    def __init__(self, lines: list[str], then_exit: bool = False) -> None:
        self._r, self._w = os.pipe()
        with os.fdopen(os.dup(self._w), "w") as w:
            w.write("".join(lines))
        if then_exit:
            os.close(self._w)
            self._w = -1
        self.stdout = os.fdopen(self._r, "r")
        LIVE.append(self)
        self.stopped = False
        self.pid = -1

    def poll(self) -> int | None:
        return 0 if self.stopped else None

    def stop(self) -> None:
        self.stopped = True
        if self._w >= 0:
            os.close(self._w)
            self._w = -1


class UnlockDecisionTest(unittest.TestCase):
    def test_hit_fid(self) -> None:
        line = "AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0"
        self.assertEqual(hit_fid(line), 1403494260)
        self.assertIsNone(hit_fid("AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11"))
        self.assertIsNone(hit_fid("AUTH HIT 1 itype=0x2 avgv=1 fid=0 rc=0"))
        self.assertIsNone(hit_fid("auth success score:0x00000026."))
        self.assertIsNone(hit_fid("AUTH HIT 1 fid=2863311530 rc=0"))

    def test_arm_line(self) -> None:
        self.assertTrue(armed("AUTH 1 arm mode=1 irq=4"))
        self.assertFalse(armed("AUTH 1 REAL DOWN itype=0x2"))

    def test_screen_gate(self) -> None:
        self.assertTrue(screen_is_on("On", "4"))
        self.assertFalse(screen_is_on("Off", "0"))
        self.assertTrue(screen_is_on(None, "0"))
        self.assertFalse(screen_is_on(None, "4"))
        self.assertFalse(screen_is_on(None, None))

    def test_choose_active_phosh(self) -> None:
        props = {
            "c30": {"Desktop": "", "Type": "tty", "Active": "yes"},
            "c4": {"Desktop": "phosh", "Type": "wayland", "Active": "yes"},
            "c5": {"Desktop": "phosh", "Type": "wayland", "Active": "no"},
        }
        self.assertEqual(choose_phosh(props), "c4")
        self.assertIsNone(choose_phosh({"c30": {"Desktop": "", "Type": "tty"}}))

    def test_locked_hint(self) -> None:
        self.assertTrue(is_locked("LockedHint=yes\n"))
        self.assertFalse(is_locked("LockedHint=no\n"))
        self.assertTrue(parse_locked("LockedHint=yes\n"))
        self.assertFalse(parse_locked("LockedHint=no\n"))
        self.assertIsNone(parse_locked(""))
        self.assertIsNone(parse_locked("Failed to get session\n"))


class LockedTriStateTest(unittest.TestCase):
    """locked() must say "unknown", never "unlocked", when loginctl fails."""

    def _run(self, rc: int = 0, out: str = "", exc: BaseException | None = None):
        def fake(*args, **kwargs):
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(args[0], rc, out, "")
        return mock.patch.object(fp5_qtee_unlock.subprocess, "run", fake)

    def test_timeout_is_unknown(self) -> None:
        with self._run(exc=subprocess.TimeoutExpired("loginctl", 5)):
            self.assertIsNone(fp5_qtee_unlock.locked("c4"))

    def test_empty_stdout_is_unknown(self) -> None:
        with self._run(out=""):
            self.assertIsNone(fp5_qtee_unlock.locked("c4"))

    def test_stale_session_id_is_unknown(self) -> None:
        with self._run(rc=1, out=""):
            self.assertIsNone(fp5_qtee_unlock.locked("c99"))

    def test_missing_loginctl_is_unknown(self) -> None:
        with self._run(exc=FileNotFoundError("loginctl")):
            self.assertIsNone(fp5_qtee_unlock.locked("c4"))

    def test_real_answers(self) -> None:
        with self._run(out="LockedHint=yes\n"):
            self.assertTrue(fp5_qtee_unlock.locked("c4"))
        with self._run(out="LockedHint=no\n"):
            self.assertIs(fp5_qtee_unlock.locked("c4"), False)


class MissTest(unittest.TestCase):
    def test_miss_kind(self) -> None:
        self.assertEqual(
            miss_kind("AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11"), "nomatch"
        )
        self.assertEqual(miss_kind("AUTH 1 empty avgv=0,0,612"), "partial")
        # The arm command failing is not a press.
        self.assertIsNone(miss_kind("AUTH FAIL 1 itype=0x0 esd=0 avgv=0 rc=-1 fid=0"))
        self.assertIsNone(miss_kind("AUTH 1 no finger"))
        self.assertIsNone(miss_kind("AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0"))
        self.assertIsNone(miss_kind("AUTH 1 REAL DOWN itype=0x2"))
        # The burst log is not a verdict. One AUTH FAIL later is the strike.
        self.assertIsNone(miss_kind("AUTH 1 burst avgv=328,521,638"))

    def test_feedback_args(self) -> None:
        self.assertEqual(
            feedback_args("bell-terminal"),
            ["fbcli", "-A", "org.fp5.qtee", "-E", "bell-terminal"],
        )
        self.assertIsNone(feedback_args(""))

    def test_notice_text_is_the_summary(self) -> None:
        # Phosh's lock screen shows only the summary; the sentence must be it.
        args = notify_args(fp5_qtee_unlock.MISS_TEXT["nomatch"], 2500, transient=True)
        self.assertEqual(args[0], "notify-send")
        self.assertEqual(args[-1], fp5_qtee_unlock.MISS_TEXT["nomatch"])
        self.assertNotIn("Fingerprint", args[-2:])
        self.assertIn("boolean:transient:true", args)
        self.assertEqual(notify_args("hi", 6000)[-1], "hi")

    def test_lockout_notice_is_the_summary(self) -> None:
        # Phosh shows the summary only. The 5th miss is the double click;
        # the lockout notice itself adds no waveform.
        for key in ("lockout", "lockout-permanent"):
            text = fp5_qtee_unlock.POLICY_TEXT[key]
            self.assertTrue(text.startswith("Too many attempts"))
            args = notify_args(text, 6000)
            self.assertEqual(args[-1], text)
            self.assertNotIn("-b", args)
        self.assertIn("30 seconds", fp5_qtee_unlock.POLICY_TEXT["lockout"])
        self.assertIn("PIN", fp5_qtee_unlock.POLICY_TEXT["lockout-permanent"])

    def test_tell_uses_the_android_waveform(self) -> None:
        played: list[str] = []
        watcher = UnlockWatcher()
        watcher.haptic = type("H", (), {"play": lambda _s, kind, **_k: played.append(kind)})()  # type: ignore[assignment]
        watcher.note = lambda _text: None  # type: ignore[method-assign]
        watcher._background = lambda _args: None  # type: ignore[method-assign]
        watcher.tell_miss("nomatch")
        watcher.tell_miss("partial")
        watcher.tell_hit()
        self.assertEqual(played, ["miss", "miss", "success"])


class PolicyTest(unittest.TestCase):
    def make(self, require_pin: bool = True, marker: Path | None = None) -> tuple[UnlockPolicy, list[float]]:
        now = [1000.0]
        return UnlockPolicy(require_pin=require_pin, marker=marker, clock=lambda: now[0]), now

    def test_pin_needed_after_boot(self) -> None:
        pol, _ = self.make()
        pol.observe_lock(True)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(False)  # PIN
        self.assertIsNone(pol.why_not())

    def test_first_sample_is_not_pin(self) -> None:
        pol, _ = self.make()
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(True)
        pol.observe_lock(False)
        self.assertIsNone(pol.why_not())

    def test_unknown_is_never_an_unlock(self) -> None:
        pol, _ = self.make()
        pol.observe_lock(None)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(True)
        pol.observe_lock(None)
        pol.observe_lock(False)  # no confirmed locked sample right before
        self.assertEqual(pol.why_not(), "pin-after-boot")

    def test_loginctl_timeout_does_not_clear_lockout(self) -> None:
        pol, now = self.make()
        pol.observe_lock(True)
        pol.observe_lock(False)  # PIN
        for _ in range(20):
            pol.on_miss()
            now[0] += 31
        self.assertEqual(pol.why_not(), "lockout-permanent")
        pol.observe_lock(True)
        for _ in range(5):
            pol.observe_lock(None)  # loginctl stuck
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "lockout-permanent")
        self.assertEqual(pol.failed, 20)

    def test_session_change_is_not_pin(self) -> None:
        pol, _ = self.make()
        pol.observe_lock(True)
        pol.forget_session()
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")

    def test_our_unlock_with_lagging_hint_is_not_pin(self) -> None:
        pol, now = self.make()
        pol.observe_lock(True)
        pol.observe_lock(False)  # PIN
        pin = pol.pin_at()
        now[0] += 100
        pol.observe_lock(True)
        pol.on_hit()
        now[0] += 0.5
        pol.observe_lock(True)  # Phosh has not cleared LockedHint yet
        now[0] += 0.5
        pol.observe_lock(False)
        self.assertEqual(pol.pin_at(), pin)
        # A later PIN unlock still counts.
        pol.observe_lock(True)
        now[0] += 1
        pol.observe_lock(False)
        self.assertNotEqual(pol.pin_at(), pin)

    def test_finger_unlock_is_not_pin(self) -> None:
        pol, now = self.make()
        pol.observe_lock(True)
        pol.observe_lock(False)  # PIN
        pol.observe_lock(True)
        now[0] += 71 * 3600
        pol.on_hit()
        pol.observe_lock(False)
        pol.observe_lock(True)
        now[0] += 2 * 3600
        self.assertEqual(pol.why_not(), "pin-72h")
        pol.observe_lock(False)  # PIN again
        self.assertIsNone(pol.why_not())

    def test_timed_and_permanent_lockout(self) -> None:
        pol, now = self.make(require_pin=False)
        for _ in range(4):
            pol.on_miss()
        self.assertIsNone(pol.why_not())
        pol.on_miss()
        self.assertEqual(pol.why_not(), "lockout")
        now[0] += 29
        self.assertEqual(pol.why_not(), "lockout")
        now[0] += 1
        self.assertIsNone(pol.why_not())
        for _ in range(15):
            pol.on_miss()
            now[0] += 31
        self.assertEqual(pol.failed, 20)
        self.assertEqual(pol.why_not(), "lockout-permanent")
        pol.observe_lock(True)
        pol.observe_lock(False)  # PIN clears it
        self.assertIsNone(pol.why_not())

    def test_hit_resets_count(self) -> None:
        pol, _ = self.make(require_pin=False)
        for _ in range(4):
            pol.on_miss()
        pol.on_hit()
        pol.on_miss()
        self.assertIsNone(pol.why_not())

    def test_marker_survives_restart_and_ignores_old_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pin-ok"
            pol, now = self.make(marker=marker)
            pol.observe_lock(True)
            pol.observe_lock(False)
            again, _ = self.make(marker=marker)
            self.assertIsNone(again.why_not())
            marker.write_text("999999.0\n")  # later than now: earlier boot
            self.assertEqual(again.why_not(), "pin-after-boot")
            marker.write_text("junk\n")
            self.assertEqual(again.why_not(), "pin-after-boot")

    def test_restart_keeps_lockout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "fp5-qtee-pin-ok"
            state = lockout_state_for(marker)
            now = [1000.0]
            clock = lambda: now[0]
            pol = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            pol.observe_lock(True)
            pol.observe_lock(False)  # PIN
            for _ in range(5):
                pol.on_miss()
            self.assertEqual(pol.why_not(), "lockout")
            # Crash and Restart=on-failure: a new policy from the files.
            again = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            self.assertEqual(again.failed, 5)
            self.assertEqual(again.why_not(), "lockout")
            now[0] += 31
            self.assertIsNone(again.why_not())
            for _ in range(15):
                again.on_miss()
                now[0] += 31
            third = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            self.assertEqual(third.why_not(), "lockout-permanent")
            # Only a PIN clears it, and that is saved too.
            third.observe_lock(True)
            third.observe_lock(False)
            fourth = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            self.assertIsNone(fourth.why_not())
            self.assertEqual(oct(state.stat().st_mode & 0o777), "0o600")

    def test_lockout_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "fp5-qtee-pin-ok"
            state = lockout_state_for(marker)
            clock = lambda: 1000.0
            marker.write_text("500.0\n")
            # PIN marker but no state file: it was removed.
            pol = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            self.assertEqual(pol.why_not(), "lockout-permanent")
            state.write_text("garbage\n")
            pol = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            self.assertEqual(pol.why_not(), "lockout-permanent")
            # A lockout end far in the future is clamped to one lockout.
            state.write_text("5 999999.0\n")
            pol = UnlockPolicy(require_pin=True, marker=marker, clock=clock, state=state)
            self.assertEqual(pol.locked_until, 1000.0 + fp5_qtee_unlock.LOCKOUT_S)

    def test_unsaved_strike_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "missing-dir" / "fp5-qtee-lockout"
            pol = UnlockPolicy(require_pin=False, marker=None, clock=lambda: 1000.0, state=state)
            self.assertIsNone(pol.why_not())
            pol.on_miss()
            self.assertEqual(pol.why_not(), "lockout-permanent")

    def test_opt_out(self) -> None:
        pol, _ = self.make(require_pin=False)
        pol.observe_lock(True)
        self.assertIsNone(pol.why_not())


class PumpAndThrottleTest(unittest.TestCase):
    def test_pump_ends_with_none(self) -> None:
        q: queue.Queue[str | None] = queue.Queue()
        pump_lines(["a\n", "b\n"], q)
        self.assertEqual([q.get(), q.get(), q.get()], ["a\n", "b\n", None])

    def test_throttle(self) -> None:
        now = [0.0]
        calls = []

        def fetch() -> int:
            calls.append(1)
            return len(calls)

        t = Throttle(1.0, fetch, clock=lambda: now[0])
        self.assertEqual(t.get(), 1)
        now[0] = 0.5
        self.assertEqual(t.get(), 1)
        now[0] = 1.0
        self.assertEqual(t.get(), 2)
        t.forget()
        self.assertEqual(t.get(), 3)


class WakePressTest(unittest.TestCase):
    def test_window(self) -> None:
        self.assertTrue(wake_press(100.3, 100.0, 0.4))
        self.assertTrue(wake_press(99.9, 100.0, 0.4))  # panel poll lag
        self.assertTrue(wake_press(100.4, 100.0, 0.4))
        self.assertFalse(wake_press(100.41, 100.0, 0.4))
        self.assertFalse(wake_press(100.0, None, 0.4))
        self.assertFalse(wake_press(100.0, 100.0, 0.0))

    def test_default_is_named_and_about_400ms(self) -> None:
        self.assertAlmostEqual(fp5_qtee_unlock.WAKE_GRACE_S, 0.4)

    def test_down_line(self) -> None:
        self.assertTrue(is_down("AUTH 1 REAL DOWN itype=0x2\n"))
        self.assertFalse(is_down("AUTH 1 arm mode=1 irq=4"))


class FollowTest(unittest.TestCase):
    """The session is silent while it waits. The watcher must still stop it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(fp5_qtee_unlock, "LOG_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._close_procs)
        self.w = UnlockWatcher()
        self.w.notify = lambda text: None  # type: ignore[method-assign]
        self.unlocked: list[tuple[str, int]] = []
        self.w.unlock = lambda sid, fid: self.unlocked.append((sid, fid))  # type: ignore[method-assign]
        self.w._stop = lambda proc: proc.stop()  # type: ignore[method-assign,assignment]
        self.misses: list[str] = []
        self.w.tell_miss = lambda kind: self.misses.append(kind)  # type: ignore[method-assign]
        self.w.tell_hit = lambda: None  # type: ignore[method-assign]
        self.w.feedback = lambda event: None  # type: ignore[method-assign]
        self.w.policy = UnlockPolicy(require_pin=False, marker=None)

    @staticmethod
    def _close_procs() -> None:
        while LIVE:
            proc = LIVE.pop()
            proc.stop()
            proc.stdout.close()

    def test_stops_silent_session_when_panel_goes_off(self) -> None:
        proc = FakeProc(["AUTH 1 arm mode=1 irq=4\n"])
        checks = []

        def wanted() -> bool:
            checks.append(1)
            return len(checks) < 3

        t0 = time.monotonic()
        out = self.w.follow("c4", proc, wanted, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertTrue(proc.stopped)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(self.unlocked, [])

    def test_hit_unlocks(self) -> None:
        proc = FakeProc([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n",
        ])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "hit")
        self.assertEqual(self.unlocked, [("c4", 1403494260)])

    def test_hit_after_panel_off_does_not_unlock(self) -> None:
        proc = FakeProc([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n",
        ])
        out = self.w.follow("c4", proc, lambda: False, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.unlocked, [])

    def test_hit_is_rechecked_before_unlock(self) -> None:
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow("c4", proc, lambda: False, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.unlocked, [])

    def test_exit_without_hit_is_miss(self) -> None:
        proc = FakeProc(
            ["AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n", "session_exit:2\n"],
            then_exit=True,
        )
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "miss")
        self.assertEqual(self.unlocked, [])
        self.assertEqual(self.misses, ["nomatch"])

    def test_hit_refused_during_lockout(self) -> None:
        for _ in range(5):
            self.w.policy.on_miss()
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.unlocked, [])

    def test_partial_does_not_count(self) -> None:
        for _ in range(4):
            self.w.policy.on_miss()
        proc = FakeProc(["AUTH 1 empty avgv=0,0,0\n", "session_exit:2\n"], then_exit=True)
        self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(self.w.policy.failed, 4)
        self.assertIsNone(self.w.policy.why_not())

    def test_miss_counts_even_after_panel_off(self) -> None:
        proc = FakeProc(
            ["AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n", "session_exit:2\n"],
            then_exit=True,
        )
        self.w.follow("c4", proc, lambda: False, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.misses, [])

    def test_unknown_lock_state_stops(self) -> None:
        lock = Throttle(60.0, lambda: None)
        with mock.patch.object(fp5_qtee_unlock, "panel_on", lambda: True):
            self.assertFalse(self.w.still_wanted("c4", lock))

    def test_lock_state_timeout_keeps_lockout(self) -> None:
        self.w.policy.observe_lock(True)
        for _ in range(20):
            self.w.policy.on_miss()
        self.w.policy.observe_lock(True)
        with mock.patch.object(fp5_qtee_unlock, "locked", lambda sid: None):
            self.assertIsNone(self.w.lock_state("c4"))
        self.assertEqual(self.w.policy.why_not(), "lockout-permanent")
        with mock.patch.object(fp5_qtee_unlock, "locked", lambda sid: False):
            self.w.lock_state("c4")
        self.assertEqual(self.w.policy.why_not(), "lockout-permanent")

    def _wake_case(self, lines: list[str], down_after: float) -> str:
        now = [1000.0]
        self.w._clock = lambda: now[0]  # type: ignore[assignment]
        self.w.panel_on_at = 1000.0
        proc = FakeProc(lines, then_exit=True)
        orig = self.w.note
        self.notes: list[str] = []
        self.w.note = lambda text: self.notes.append(text)  # type: ignore[method-assign]
        now[0] = 1000.0 + down_after
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.w.note = orig  # type: ignore[method-assign]
        return out

    def test_wake_press_miss_is_ignored(self) -> None:
        out = self._wake_case([
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], 0.15)
        self.assertEqual(out, "miss")
        self.assertEqual(self.w.policy.failed, 0)
        self.assertEqual(self.misses, [])
        self.assertTrue(any("wake press" in n for n in self.notes))

    def test_wake_press_partial_is_silent(self) -> None:
        self._wake_case(["AUTH 1 REAL DOWN itype=0x2\n",
                         "AUTH 1 empty avgv=0,0,0\n", "session_exit:2\n"], 0.1)
        self.assertEqual(self.misses, [])

    def test_wake_press_hit_still_unlocks(self) -> None:
        out = self._wake_case([
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n",
        ], 0.1)
        self.assertEqual(out, "hit")
        self.assertEqual(self.unlocked, [("c4", 1403494260)])

    def test_miss_after_wake_window_is_a_strike(self) -> None:
        self._wake_case([
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], 1.5)
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.misses, ["nomatch"])

    def test_partial_press_is_told(self) -> None:
        proc = FakeProc(["AUTH 1 empty avgv=0,0,0\n", "session_exit:2\n"], then_exit=True)
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "miss")
        self.assertEqual(self.misses, ["partial"])

    def test_lock_hint_is_throttled(self) -> None:
        asks = []
        lock = Throttle(60.0, lambda: asks.append(1) or True)
        with mock.patch.object(fp5_qtee_unlock, "panel_on", lambda: True):
            for _ in range(5):
                self.assertTrue(self.w.still_wanted("c4", lock))
        self.assertEqual(len(asks), 1)
        with mock.patch.object(fp5_qtee_unlock, "panel_on", lambda: False):
            self.assertFalse(self.w.still_wanted("c4", lock))


class FakeWarm:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.sent: list[str] = []
        self.idle = False
        self.cancel_sent = False
        self.closed = False
        self.dead = False

    def say(self, *lines: str) -> None:
        for line in lines:
            self.lines.put(line + "\n")

    def send(self, cmd: str) -> None:
        self.sent.append(cmd)

    def log(self, line: str) -> None:
        pass

    def alive(self) -> bool:
        return not self.dead

    def close(self) -> None:
        self.closed = True


class WarmLoopTest(unittest.TestCase):
    HIT = "AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0"
    FAIL = "AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11"

    def setUp(self) -> None:
        self.now = [0.0]
        self.panel = [True]
        self.lock = [True]
        self.lock_asks = 0
        self.w = UnlockWatcher()
        self.w.policy = UnlockPolicy(require_pin=False, marker=None,
                                     clock=lambda: self.now[0])
        self.events: list[tuple] = []
        self.w.notify = lambda text: self.events.append(("notify", text))  # type: ignore[method-assign]
        self.w.note = lambda text: None  # type: ignore[method-assign]
        self.w.unlock = lambda sid, fid: self.events.append(("unlock", sid, fid))  # type: ignore[method-assign]
        self.w.feedback = lambda event: None  # type: ignore[method-assign]
        self.w.tell_hit = lambda: self.events.append(("haptic", "success"))  # type: ignore[method-assign]
        self.w.tell_miss = lambda kind: self.events.append(("miss", kind))  # type: ignore[method-assign]
        self.w.session_id = lambda: "c4"  # type: ignore[method-assign]
        self.started: list[FakeWarm] = []

        def start_warm() -> FakeWarm:
            warm = FakeWarm()
            self.started.append(warm)
            return warm

        self.w.start_warm = start_warm  # type: ignore[method-assign,assignment]

        def is_locked(sid: str) -> bool | None:
            self.lock_asks += 1
            return self.lock[0]

        self.loop = WarmLoop(self.w, panel=lambda: self.panel[0],
                             is_locked=is_locked, clock=lambda: self.now[0])

    def step(self, dt: float = 0.2) -> None:
        self.loop.step()
        self.now[0] += dt

    def unlocks(self) -> list[tuple]:
        return [e for e in self.events if e[0] == "unlock"]

    def test_loads_while_dark_and_rechecks_lock_on_wake(self) -> None:
        self.step()                      # panel on, locked: starts cold
        warm = self.started[0]
        self.panel[0] = False
        warm.say("SERVE ready", "SERVE idle")
        self.step()
        self.assertEqual(warm.sent, [])  # dark: never armed
        asks = self.lock_asks
        self.panel[0] = True
        self.step()
        self.assertEqual(warm.sent, ["auth"])
        # LockedHint is read again right before the auth on wake.
        self.assertEqual(self.lock_asks, asks + 1)

    def test_wake_does_not_arm_when_unlocked_or_unknown(self) -> None:
        self.step()
        warm = self.started[0]
        self.panel[0] = False
        warm.say("SERVE ready", "SERVE idle")
        self.step()
        self.lock[0] = None              # loginctl timed out
        self.panel[0] = True
        self.step()
        self.assertEqual(warm.sent, [])
        self.assertFalse(warm.closed)    # session kept for the next try
        self.lock[0] = False             # PIN unlocked while dark
        self.step(1.1)
        self.step()
        self.assertEqual(warm.sent, [])
        self.assertTrue(warm.closed)

    def test_cancel_after_finger_down_never_unlocks(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.assertEqual(warm.sent, ["auth"])
        warm.say("AUTH 1 REAL DOWN itype=0x2")
        self.step()
        self.panel[0] = False            # panel off: cancel
        self.step()
        self.assertEqual(warm.sent, ["auth", "cancel"])
        # Even if the session still printed a hit for that attempt,
        # dark or after the panel came back on, it must not unlock.
        warm.say(self.HIT)
        self.step()
        self.panel[0] = True
        warm.say(self.HIT)
        self.step()
        self.assertEqual(self.unlocks(), [])

    def test_touch_while_panel_off_does_not_unlock_on_wake(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.panel[0] = False
        self.step()
        warm.say("AUTH 1 cancelled", "SERVE result 3", "SERVE idle")
        self.step()
        # A touch while dark was latched and comes out as a hit just as
        # the panel comes back on, before the new auth.
        warm.say(self.HIT, "SERVE result 0", "SERVE idle")
        self.panel[0] = True
        self.step()
        self.assertEqual(self.unlocks(), [])
        self.assertEqual(warm.sent, ["auth", "cancel", "auth"])
        # A hit for the new auth, armed on this wake, unlocks.
        warm.say(self.HIT, "SERVE result 0", "SERVE idle")
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])

    def test_hit_after_result_is_refused(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        warm.say("SERVE result 2", self.HIT)
        self.step()
        self.assertEqual(self.unlocks(), [])

    def test_miss_counts_even_if_panel_went_off(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.panel[0] = False
        warm.say(self.FAIL, "SERVE result 2", "SERVE idle")
        self.step()
        self.assertEqual(self.w.policy.failed, 1)
        self.assertNotIn(("miss", "nomatch"), self.events)

    def test_starts_session_after_panel_off_when_locked(self) -> None:
        self.lock[0] = False
        self.step()
        self.assertEqual(self.started, [])
        self.lock[0] = True
        self.panel[0] = False
        self.step()
        self.assertEqual(len(self.started), 1)

    def test_hit_unlocks_and_miss_rearms(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE ready", "SERVE idle")
        self.step()
        self.assertEqual(warm.sent, ["auth"])
        warm.say(self.FAIL, "SERVE result 2", "SERVE idle")
        self.step()
        self.assertIn(("miss", "nomatch"), self.events)
        self.assertEqual(warm.sent, ["auth", "auth"])
        warm.say(self.HIT, "SERVE result 0", "SERVE idle")
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])

    def test_hit_needs_logind_locked(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.lock[0] = False
        warm.say(self.HIT)
        self.step()
        self.assertEqual(self.unlocks(), [])

    def test_panel_off_cancels_once(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.panel[0] = False
        self.step()
        self.step()
        self.assertEqual(warm.sent, ["auth", "cancel"])
        warm.say("AUTH 1 cancelled", "SERVE result 3", "SERVE idle")
        self.step()
        warm.say(self.HIT)
        self.step()
        self.assertEqual(self.unlocks(), [])

    def test_pin_unlock_quits_session(self) -> None:
        self.step()
        warm = self.started[0]
        self.lock[0] = False
        self.step(1.1)
        self.step()
        self.assertTrue(warm.closed)
        self.assertIsNone(self.loop.warm)

    def test_lockout_stops_arming(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        for _ in range(5):
            self.step()
            warm.say(self.FAIL, "SERVE result 2", "SERVE idle")
        self.step()
        self.step()
        self.assertEqual(warm.sent.count("auth"), 5)
        self.assertEqual(self.w.policy.why_not(), "lockout")
        self.now[0] += 31
        self.step()
        self.assertEqual(warm.sent.count("auth"), 6)

    def test_dead_session_waits_before_restart(self) -> None:
        self.step()
        self.started[0].dead = True
        self.step()
        self.assertEqual(len(self.started), 1)
        self.now[0] += 5.1
        self.step()
        self.step()
        self.assertEqual(len(self.started), 2)


FAKE_SERVE = r"""
import sys
print("SERVE ready", flush=True)
n = 0
while True:
    print("SERVE idle", flush=True)
    line = sys.stdin.readline()
    if not line or line.strip() == "quit":
        break
    if line.strip() == "auth":
        n += 1
        print(f"AUTH {n} arm mode=1 irq=4", flush=True)
        print(f"SERVE result 2", flush=True)
print("session_exit:0", flush=True)
"""


class FakeSignals:
    def __init__(self) -> None:
        self.alive = True
        self.pending = False

    def poll(self) -> bool:
        seen, self.pending = self.pending, False
        return seen


class WarmRelockTest(WarmLoopTest.__base__):  # type: ignore[misc]
    """Unlock frees the sensor; the next lock reloads before the wake."""

    HIT = WarmLoopTest.HIT
    FAIL = WarmLoopTest.FAIL

    def setUp(self) -> None:
        WarmLoopTest.setUp(self)  # type: ignore[arg-type]
        self.sig = FakeSignals()
        self.loop.signals = self.sig  # type: ignore[assignment]

    step = WarmLoopTest.step
    unlocks = WarmLoopTest.unlocks

    def _unlock_by_finger(self) -> FakeWarm:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        warm.say(self.HIT, "SERVE result 0", "SERVE idle")
        self.lock[0] = True
        self.step()
        self.assertEqual(len(self.unlocks()), 1)
        self.lock[0] = False
        self.sig.pending = True
        self.step()
        return warm

    def test_unlock_quits_then_lock_signal_preloads_while_on(self) -> None:
        warm = self._unlock_by_finger()
        self.assertTrue(warm.closed)          # sensor released
        self.assertIsNone(self.loop.warm)
        for _ in range(20):                   # unlocked: no reload
            self.step()
        self.assertEqual(len(self.started), 1)
        self.lock[0] = True                   # Phosh locks, panel still on
        self.sig.pending = True
        self.step()
        self.assertEqual(len(self.started), 2)

    def test_late_lock_while_dark_preloads(self) -> None:
        self._unlock_by_finger()
        self.panel[0] = False
        for _ in range(100):                  # 20 s dark, still unlocked
            self.step()
        self.assertEqual(len(self.started), 1)
        asks = self.lock_asks
        self.lock[0] = True                   # lock long after blank
        self.sig.pending = True
        self.step()
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.lock_asks, asks + 1)
        warm = self.started[1]
        warm.say("SERVE ready", "SERVE idle")
        self.step()
        self.assertEqual(warm.sent, [])       # dark: loaded, not armed
        self.panel[0] = True
        self.step()
        self.assertEqual(warm.sent, ["auth"]) # warm on wake

    def test_unlocked_screen_on_polls_slowly_with_signals(self) -> None:
        self._unlock_by_finger()
        asks = self.lock_asks
        for _ in range(25):                   # 5 s
            self.step()
        self.assertLessEqual(self.lock_asks - asks, 1)

    def test_without_signals_dark_lock_is_still_found(self) -> None:
        self._unlock_by_finger()
        self.sig.alive = False
        self.panel[0] = False
        for _ in range(60):
            self.step()
        self.lock[0] = True
        for _ in range(60):                   # within DARK_LOCK_POLL_S
            self.step()
        self.assertEqual(len(self.started), 2)

    def test_wake_press_miss_is_ignored_but_hit_unlocks(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.panel[0] = False
        self.step()
        self.panel[0] = True                  # power key wakes the panel
        self.step()
        self.assertEqual(warm.sent[-1], "auth")
        warm.say("AUTH 1 REAL DOWN itype=0x2", self.FAIL,
                 "SERVE result 2", "SERVE idle")
        self.step()                           # 0.2 s after panel-on
        self.assertEqual(self.w.policy.failed, 0)
        self.assertNotIn(("miss", "nomatch"), self.events)
        self.now[0] += 1.0
        warm.say("AUTH 1 REAL DOWN itype=0x2", self.FAIL,
                 "SERVE result 2", "SERVE idle")
        self.step()                           # a real try later
        self.assertEqual(self.w.policy.failed, 1)
        self.assertIn(("miss", "nomatch"), self.events)

    def test_wake_press_hit_unlocks(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.panel[0] = False
        self.step()
        self.panel[0] = True
        self.step()
        warm.say("AUTH 1 REAL DOWN itype=0x2", self.HIT,
                 "SERVE result 0", "SERVE idle")
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])


class LockSignalTest(unittest.TestCase):
    def test_lines(self) -> None:
        self.assertTrue(lock_signal(
            "/org/freedesktop/login1/session/_32: org.freedesktop.DBus."
            "Properties.PropertiesChanged ('org.freedesktop.login1.Session',"
            " {'LockedHint': <true>}, @as [])"))
        self.assertTrue(lock_signal(
            "/org/freedesktop/login1/session/c4: "
            "org.freedesktop.login1.Session.Unlock ()"))
        self.assertFalse(lock_signal(
            "/org/freedesktop/login1: org.freedesktop.login1.Manager."
            "SessionNew ('5', objectpath '/org/freedesktop/login1/session/_35')"))

    def test_process_and_fallback(self) -> None:
        sig = LockSignals(["sh", "-c", "echo \"x {'LockedHint': <true>}\"; exec sleep 5"])
        self.addCleanup(sig.close)
        deadline = time.monotonic() + 3
        seen = False
        while time.monotonic() < deadline and not seen:
            seen = sig.poll()
            time.sleep(0.02)
        self.assertTrue(seen)
        self.assertTrue(sig.alive)
        self.assertFalse(sig.poll())
        gone = LockSignals(["/nonexistent/gdbus"])
        self.assertFalse(gone.alive)
        ended = LockSignals(["true"])
        self.addCleanup(ended.close)
        deadline = time.monotonic() + 3
        while ended.alive and time.monotonic() < deadline:
            ended.poll()
            time.sleep(0.02)
        self.assertFalse(ended.alive)


class WarmSessionPipeTest(unittest.TestCase):
    """Real pipes against a stand-in for the serve protocol."""

    def test_auth_then_quit(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", FAKE_SERVE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        warm = WarmSession(proc, None)

        def until(text: str) -> list[str]:
            got = []
            while True:
                line = warm.lines.get(timeout=5)
                self.assertIsNotNone(line)
                got.append(line.strip())
                if line.strip() == text:
                    return got

        until("SERVE idle")
        warm.send("auth")
        got = until("SERVE idle")
        self.assertIn("AUTH 1 arm mode=1 irq=4", got)
        warm.close()
        self.assertFalse(warm.alive())
        proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
