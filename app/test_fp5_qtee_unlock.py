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
    keep_session,
    miss_kind,
    parse_locked,

    notify_args,
    pump_lines,
    screen_is_on,
    armed_hold,
    listen_done_text,
    verify_score,
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
        self.assertEqual(fp5_qtee_unlock.hit_attempt(line), 1)
        self.assertEqual(
            fp5_qtee_unlock.hit_attempt(
                "AUTH HIT 2 itype=0x2 avgv=304 fid=1403494260 rc=0"
            ),
            2,
        )
        self.assertIsNone(fp5_qtee_unlock.hit_attempt("AUTH FAIL 1 itype=0x2"))
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
        self.assertIsNone(miss_kind("AUTH stay: all-zero miss"))
        self.assertIsNone(miss_kind("AUTH 1 skip falling avgv=158,158,165 first=241"))

    def test_keep_session_is_an_all_zero_nomatch(self) -> None:
        self.assertTrue(keep_session("nomatch", False))
        self.assertFalse(keep_session("nomatch", True))
        self.assertFalse(keep_session("partial", False))
        self.assertFalse(keep_session(None, False))

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
    def setUp(self) -> None:
        self._dirs: list[tempfile.TemporaryDirectory[str]] = []
        self.addCleanup(self._close_dirs)

    def _close_dirs(self) -> None:
        while self._dirs:
            self._dirs.pop().cleanup()

    def make(self, require_pin: bool = True, marker: Path | None = None) -> tuple[UnlockPolicy, list[float]]:
        now = [1000.0]
        if marker is None:
            tmp = tempfile.TemporaryDirectory()
            self._dirs.append(tmp)
            state: Path | None = Path(tmp.name) / "fp5-qtee-lockout"
        else:
            state = lockout_state_for(marker)
        return UnlockPolicy(
            require_pin=require_pin, marker=marker, clock=lambda: now[0], state=state,
        ), now

    def credit_pin(self, pol: UnlockPolicy, now: list[float]) -> None:
        """Locked, then unlocked, then still unlocked after the flicker window."""
        pol.observe_lock(True)
        pol.observe_lock(False)
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)

    def test_pin_needed_after_boot(self) -> None:
        pol, now = self.make()
        pol.observe_lock(True)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)
        self.assertIsNone(pol.why_not())

    def test_first_sample_is_not_pin(self) -> None:
        pol, now = self.make()
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        pol.observe_lock(True)
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
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
        self.credit_pin(pol, now)
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
        self.credit_pin(pol, now)
        pin = pol.pin_at()
        now[0] += 100
        pol.observe_lock(True)
        pol.on_hit()
        now[0] += 0.5
        pol.observe_lock(True)  # Phosh has not cleared LockedHint yet
        now[0] += 0.5
        pol.observe_lock(False)
        self.assertEqual(pol.pin_at(), pin)
        # Staying unlocked is still our unlock, not a new PIN.
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)
        self.assertEqual(pol.pin_at(), pin)
        # A later PIN unlock still counts, once it holds.
        pol.observe_lock(True)
        now[0] += 1
        pol.observe_lock(False)
        self.assertEqual(pol.pin_at(), pin)
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)
        self.assertNotEqual(pol.pin_at(), pin)

    def test_finger_unlock_is_not_pin(self) -> None:
        pol, now = self.make()
        self.credit_pin(pol, now)
        pol.observe_lock(True)
        now[0] += 71 * 3600
        pol.on_hit()
        pol.observe_lock(False)
        pol.observe_lock(True)
        now[0] += 2 * 3600
        self.assertEqual(pol.why_not(), "pin-72h")
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-72h")
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)
        self.assertIsNone(pol.why_not())

    def test_unlock_flicker_is_not_a_pin(self) -> None:
        pol, now = self.make()
        pol.observe_lock(True)
        pol.observe_lock(False)
        now[0] += 1.0
        pol.observe_lock(True)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        self.assertIsNone(pol.pin_at())
        pol.observe_lock(False)
        pol.forget_session()
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "pin-after-boot")
        self.credit_pin(pol, now)
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
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "lockout-permanent")
        now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
        pol.observe_lock(False)
        self.assertIsNone(pol.why_not())
        self.assertEqual(pol.failed, 0)

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
            self.credit_pin(pol, now)
            again = UnlockPolicy(
                require_pin=True, marker=marker, clock=lambda: now[0],
                state=lockout_state_for(marker),
            )
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
            self.credit_pin(pol, now)
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
            self.assertEqual(third.why_not(), "lockout-permanent")
            now[0] += fp5_qtee_unlock.LOCK_FLICKER_S
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

    def test_no_state_path_refuses_finger_unlock(self) -> None:
        pol = UnlockPolicy(require_pin=False, marker=None, clock=lambda: 1000.0, state=None)
        self.assertEqual(pol.why_not(), "no-state")
        pol.on_miss()
        pol.on_hit()
        pol.observe_lock(True)
        pol.observe_lock(False)
        self.assertEqual(pol.why_not(), "no-state")
        self.assertEqual(pol.failed, 0)
        pinned = UnlockPolicy(require_pin=True, marker=None, clock=lambda: 1000.0, state=None)
        self.assertEqual(pinned.why_not(), "no-state")
        self.assertIn("saved", fp5_qtee_unlock.POLICY_TEXT["no-state"])


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


class AwaitPanelTest(unittest.TestCase):
    def test_already_on(self) -> None:
        self.assertTrue(fp5_qtee_unlock.await_panel(
            lambda: True, 1.0, lambda: True, sleep=lambda _s: None,
            clock=lambda: 0.0,
        ))

    def test_comes_on_within_the_grace(self) -> None:
        now = [0.0]
        on = [False]

        def clock() -> float:
            return now[0]

        def sleep(dt: float) -> None:
            now[0] += dt
            if now[0] >= 0.2:
                on[0] = True

        self.assertTrue(fp5_qtee_unlock.await_panel(
            lambda: on[0], 1.0, lambda: True, sleep=sleep, clock=clock,
        ))
        self.assertLess(now[0], 1.0)

    def test_staying_off(self) -> None:
        now = [0.0]

        def sleep(dt: float) -> None:
            now[0] += dt

        self.assertFalse(fp5_qtee_unlock.await_panel(
            lambda: False, 1.0, lambda: True, sleep=sleep, clock=lambda: now[0],
        ))
        self.assertGreaterEqual(now[0], 1.0)

    def test_stops_when_no_longer_wanted(self) -> None:
        now = [0.0]

        def sleep(dt: float) -> None:
            now[0] += dt

        self.assertFalse(fp5_qtee_unlock.await_panel(
            lambda: False, 5.0, lambda: now[0] < 0.1,
            sleep=sleep, clock=lambda: now[0],
        ))
        self.assertLess(now[0], 1.0)

    def test_zero_wait_checks_once(self) -> None:
        self.assertFalse(fp5_qtee_unlock.await_panel(
            lambda: False, 0.0, lambda: True,
        ))


class PanelRiseTest(unittest.TestCase):
    def test_rise_before_arm_opens_the_wake_window(self) -> None:
        w = fp5_qtee_unlock.UnlockWatcher()
        w._panel_was_on = False
        self.assertTrue(w._note_panel(True, False))
        self.assertTrue(w._wake_open)

    def test_rise_after_arm_is_scored(self) -> None:
        w = fp5_qtee_unlock.UnlockWatcher()
        w._panel_was_on = False
        w._wake_open = False
        self.assertTrue(w._note_panel(True, True))
        self.assertFalse(w._wake_open)

    def test_already_on_at_start_is_not_a_wake(self) -> None:
        w = fp5_qtee_unlock.UnlockWatcher()
        self.assertIsNone(w._panel_was_on)
        self.assertFalse(w._note_panel(True, False))
        self.assertFalse(w._wake_open)


class WakePressTest(unittest.TestCase):
    def test_window(self) -> None:
        # Already down at the arm line, or the IRQ within ~300 ms.
        self.assertTrue(wake_press(100.0, 100.0, 1.0))
        self.assertTrue(wake_press(100.3, 100.0, 1.0))
        self.assertTrue(wake_press(101.0, 100.0, 1.0))
        self.assertFalse(wake_press(101.01, 100.0, 1.0))
        self.assertFalse(wake_press(100.0, None, 1.0))
        self.assertFalse(wake_press(100.0, 100.0, 0.0))

    def test_default_is_one_second_from_arm(self) -> None:
        # Fallback when the session cannot say the finger was already down.
        self.assertAlmostEqual(fp5_qtee_unlock.WAKE_GRACE_S, 1.0)

    def test_nonzero_score(self) -> None:
        line = "REPORT_EV5 qsee: FtVerifySubTemplate() score = 5, matchCnts = 13"
        self.assertEqual(verify_score(line), 5)
        self.assertEqual(
            verify_score("FtVerifySubTemplate() score = 0, matchCnts = 17"), 0
        )
        self.assertIsNone(verify_score("AUTH FAIL 1 itype=0x2 esd=0 avgv=259 fid=0 rc=-11"))

    def test_nonzero_score_is_not_ignored(self) -> None:
        ignore, claimed = fp5_qtee_unlock.consume_wake(
            False, 10.2, 10.0, 1.0, nonzero=True
        )
        self.assertFalse(ignore)
        self.assertTrue(claimed)

    def test_zero_score_wake_is_still_ignored(self) -> None:
        ignore, claimed = fp5_qtee_unlock.consume_wake(
            False, 10.2, 10.0, 1.0, nonzero=False
        )
        self.assertTrue(ignore)
        self.assertTrue(claimed)

    def test_flicker_hold(self) -> None:
        stop, since = armed_hold(True, "seat unlocked", None, 10.0)
        self.assertFalse(stop)
        self.assertEqual(since, 10.0)
        stop, since = armed_hold(True, "seat unlocked", 10.0, 11.9)
        self.assertFalse(stop)
        stop, since = armed_hold(True, "lock unknown", 10.0, 12.0)
        self.assertTrue(stop)
        self.assertEqual(since, 10.0)
        stop, _since = armed_hold(False, "seat unlocked", None, 10.0)
        self.assertTrue(stop)
        stop, since = armed_hold(True, "qsee_fingerpr loaded", 10.0, 10.1)
        self.assertTrue(stop)
        self.assertIsNone(since)
        stop, _since = armed_hold(True, "not wanted", None, 10.0)
        self.assertTrue(stop)

    def test_listen_stop_names_the_reason(self) -> None:
        self.assertEqual(
            listen_done_text("stop", "seat unlocked"),
            "listen done stop: seat unlocked",
        )
        self.assertEqual(
            listen_done_text("stop", None),
            "listen done stop: unspecified",
        )
        self.assertEqual(listen_done_text("miss", None), "listen done miss")
        self.assertEqual(
            listen_done_text("error", "qcomtee load failed"),
            "listen done error: qcomtee load failed",
        )

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
        self.w.unlock = lambda sid, fid: self.unlocked.append((sid, fid)) or True  # type: ignore[method-assign]
        self.w._stop = lambda proc: proc.stop()  # type: ignore[method-assign,assignment]
        self.misses: list[str] = []
        self.w.tell_miss = lambda kind: self.misses.append(kind)  # type: ignore[method-assign]
        self.w.tell_hit = lambda: None  # type: ignore[method-assign]
        self.w.feedback = lambda event: None  # type: ignore[method-assign]
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        self.w.policy = UnlockPolicy(
            require_pin=False, marker=None,
            state=Path(state_dir.name) / "fp5-qtee-lockout",
        )

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

    def _real_unlock(self, run_result: object) -> None:
        """Call the real unlock(), with subprocess.run replaced."""
        hits: list[str] = []
        self.w.tell_hit = lambda: hits.append("hit")  # type: ignore[method-assign]
        self.hits = hits
        self.w.policy.on_miss()
        self.w.policy.on_miss()
        self.failed_before = self.w.policy.failed
        self.w.unlock = lambda sid, fid: UnlockWatcher.unlock(self.w, sid, fid)  # type: ignore[method-assign]

        def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if isinstance(run_result, BaseException):
                raise run_result
            return run_result  # type: ignore[return-value]

        self._run_patch = mock.patch.object(fp5_qtee_unlock.subprocess, "run", run)
        self._run_patch.start()
        self.addCleanup(self._run_patch.stop)

    def test_loginctl_error_does_not_count_as_a_hit(self) -> None:
        self._real_unlock(subprocess.CompletedProcess(["loginctl"], 1))
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.w._listen_why, "unlock failed")
        self.assertEqual(self.hits, [])
        self.assertEqual(self.w.policy.failed, self.failed_before)
        self.assertEqual(self.unlocked, [])

    def test_loginctl_timeout_does_not_count_as_a_hit(self) -> None:
        self._real_unlock(subprocess.TimeoutExpired(["loginctl"], 5))
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.w._listen_why, "unlock failed")
        self.assertEqual(self.hits, [])
        self.assertEqual(self.w.policy.failed, self.failed_before)

    def test_loginctl_oserror_does_not_count_as_a_hit(self) -> None:
        self._real_unlock(OSError("loginctl missing"))
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.w._listen_why, "unlock failed")
        self.assertEqual(self.hits, [])
        self.assertEqual(self.w.policy.failed, self.failed_before)

    def test_loginctl_ok_resets_strikes_and_buzzes(self) -> None:
        self._real_unlock(subprocess.CompletedProcess(["loginctl"], 0))
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "hit")
        self.assertEqual(self.hits, ["hit"])
        self.assertEqual(self.w.policy.failed, 0)

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

    def test_all_zero_miss_keeps_the_session_for_the_next_press(self) -> None:
        notes: list[str] = []
        counted: list[int] = []

        def note(text: str) -> None:
            notes.append(text)
            counted.append(self.w.policy.failed)

        self.w.note = note  # type: ignore[method-assign]
        proc = FakeProc([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 0, matchCnts = 28\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=274 fid=0 rc=-11\n",
            "AUTH stay: all-zero miss\n",
            "AUTH 2 arm mode=1 irq=9\n",
            "AUTH 2 REAL DOWN itype=0x2\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 24, matchCnts = 8\n",
            "AUTH HIT 2 itype=0x2 avgv=292 fid=1979259351 rc=0\n",
        ])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "hit")
        self.assertEqual(counted, [1])
        self.assertEqual(self.w.policy.failed, 0)
        self.assertEqual(self.unlocked, [("c4", 1979259351)])
        self.assertTrue(any("same session" in n for n in notes))
        self.assertEqual(self.misses, ["nomatch"])

    def test_nonzero_miss_does_not_keep_the_session(self) -> None:
        notes: list[str] = []
        self.w.note = lambda text: notes.append(text)  # type: ignore[method-assign]
        proc = FakeProc([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 5, matchCnts = 36\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=283 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], then_exit=True)
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "miss")
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.unlocked, [])
        self.assertFalse(any("same session" in n for n in notes))

    def test_lockout_still_stops_an_all_zero_miss(self) -> None:
        notes: list[str] = []
        self.w.note = lambda text: notes.append(text)  # type: ignore[method-assign]
        for _ in range(4):
            self.w.policy.on_miss()
        proc = FakeProc([
            "AUTH 1 arm mode=1 irq=4\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 0, matchCnts = 8\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=274 fid=0 rc=-11\n",
        ])
        out = self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.w.policy.why_not(), "lockout")
        self.assertTrue(proc.stopped)
        self.assertEqual(self.unlocked, [])
        self.assertFalse(any("same session" in n for n in notes))

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

    def test_miss_counts_when_the_screen_was_on(self) -> None:
        # The seat can unlock as the line arrives. The press still counts.
        proc = FakeProc(
            ["AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n", "session_exit:2\n"],
            then_exit=True,
        )
        self.w.follow(  # type: ignore[arg-type]
            "c4", proc, lambda: False, poll_s=0.01,
            panel=lambda: True, wake_wait_s=0,
        )
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.misses, [])

    def test_miss_with_panel_off_is_not_a_strike(self) -> None:
        proc = FakeProc(
            ["AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n", "session_exit:2\n"],
            then_exit=True,
        )
        notes: list[str] = []
        slept: list[float] = []
        self.w.note = lambda text: notes.append(text)  # type: ignore[method-assign]
        self.w.follow(  # type: ignore[arg-type]
            "c4", proc, lambda: True, poll_s=0.01,
            panel=lambda: False, wake_wait_s=0, hit_wait_s=4.0,
            sleep=lambda dt: slept.append(dt), clock=lambda: 0.0,
        )
        self.assertEqual(self.w.policy.failed, 0)
        self.assertEqual(self.misses, [])
        self.assertEqual(self.unlocked, [])
        self.assertEqual(slept, [])
        self.assertTrue(any("panel off" in n for n in notes))

    def test_dark_hit_waits_for_the_panel_without_waking_it(self) -> None:
        # 21:42Z: AUTH HIT while DSI was still off. The old 1 s wait
        # dropped the fid and the next listen calibrated again.
        now = [0.0]
        on = [False]
        wakes: list[str] = []
        self.w.dark_arm = True
        self.w.wake_panel = lambda: wakes.append("wake")  # type: ignore[method-assign]
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=305 fid=1558157930 rc=0\n"])

        def sleep(dt: float) -> None:
            self.assertFalse(proc.stopped)
            now[0] += dt
            if now[0] >= 2.0:
                on[0] = True

        out = self.w.follow(  # type: ignore[arg-type]
            "c4", proc, lambda: True, poll_s=0.01,
            panel=lambda: on[0], hit_wait_s=4.0,
            sleep=sleep, clock=lambda: now[0],
        )
        self.assertEqual(out, "hit")
        self.assertEqual(self.unlocked, [("c4", 1558157930)])
        self.assertEqual(wakes, [])
        self.assertGreaterEqual(now[0], 2.0)
        self.assertLess(now[0], 4.0)

    def test_dark_hit_gives_up_after_four_seconds(self) -> None:
        now = [0.0]
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=305 fid=1558157930 rc=0\n"])

        def sleep(dt: float) -> None:
            self.assertFalse(proc.stopped)
            now[0] += dt

        out = self.w.follow(  # type: ignore[arg-type]
            "c4", proc, lambda: True, poll_s=0.01,
            panel=lambda: False, hit_wait_s=4.0,
            sleep=sleep, clock=lambda: now[0],
        )
        self.assertEqual(out, "stop")
        self.assertEqual(self.unlocked, [])
        self.assertGreaterEqual(now[0], 4.0)
        self.assertLess(now[0], 4.2)

    def test_hit_with_panel_staying_off_does_not_unlock(self) -> None:
        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow(  # type: ignore[arg-type]
            "c4", proc, lambda: True, poll_s=0.01,
            panel=lambda: False, wake_wait_s=0,
        )
        self.assertEqual(out, "stop")
        self.assertEqual(self.unlocked, [])

    def test_hit_unlocks_when_the_panel_comes_on(self) -> None:
        calls = [0]

        def panel() -> bool:
            calls[0] += 1
            return calls[0] >= 2

        proc = FakeProc(["AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n"])
        out = self.w.follow(  # type: ignore[arg-type]
            "c4", proc, lambda: True, poll_s=0.01,
            panel=panel, wake_wait_s=1.0,
        )
        self.assertEqual(out, "hit")
        self.assertEqual(self.unlocked, [("c4", 1403494260)])

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

    def _wake_case(self, lines: list[str], times: list[float],
                   reopen: bool = False) -> str:
        """times are successive _clock() reads: arm, then finger-down."""
        seq = list(times)
        idx = [0]

        def clock() -> float:
            i = min(idx[0], len(seq) - 1)
            idx[0] += 1
            return seq[i]

        self.w._clock = clock  # type: ignore[method-assign]
        if reopen:
            self.w._wake_open = True
            self.w._arm_at = None
            self.w._wake_claimed = False
        proc = FakeProc(lines, then_exit=True)
        self.notes: list[str] = []
        self.w.note = lambda text: self.notes.append(text)  # type: ignore[method-assign]
        return self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]

    def test_wake_press_miss_is_ignored(self) -> None:
        # 07:48:21Z: first listen, REAL DOWN with no leftover, within 1 s of arm.
        out = self._wake_case([
            "AUTH 1 arm mode=1 irq=428\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=241 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [22.0, 22.2], reopen=True)
        self.assertEqual(out, "miss")
        self.assertEqual(self.w.policy.failed, 0)
        self.assertEqual(self.misses, [])
        self.assertTrue(any("wake press" in n for n in self.notes))
        self.assertFalse(self.w._wake_open)

    def test_wake_nonzero_score_is_a_strike(self) -> None:
        # 20:18:02Z: one subtemplate scored 5 and the wake rule dropped it.
        out = self._wake_case([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 0, matchCnts = 12\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 5, matchCnts = 13\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=259 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [22.0, 22.2], reopen=True)
        self.assertEqual(out, "miss")
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.misses, ["nomatch"])
        self.assertFalse(any("ignored" in n for n in self.notes))

    def test_wake_all_zero_scores_stay_ignored(self) -> None:
        out = self._wake_case([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "REPORT_EV5 qsee: FtVerifySubTemplate() score = 0, matchCnts = 17\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=241 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [22.0, 22.2], reopen=True)
        self.assertEqual(out, "miss")
        self.assertEqual(self.w.policy.failed, 0)
        self.assertEqual(self.misses, [])
        self.assertTrue(any("wake press" in n for n in self.notes))

    def test_wake_press_partial_is_silent(self) -> None:
        self._wake_case([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH 1 empty avgv=0,0,0\n",
            "session_exit:2\n",
        ], [10.0, 10.1], reopen=True)
        self.assertEqual(self.misses, [])
        self.assertEqual(self.w.policy.failed, 0)

    def test_wake_press_hit_still_unlocks(self) -> None:
        out = self._wake_case([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0\n",
        ], [10.0, 10.1], reopen=True)
        self.assertEqual(out, "hit")
        self.assertEqual(self.unlocked, [("c4", 1403494260)])
        self.assertEqual(self.w.policy.failed, 0)

    def test_miss_seconds_after_arm_is_a_strike(self) -> None:
        # 08:08:43Z and 08:08:54Z: the finger arrived seconds after arm.
        self._wake_case([
            "AUTH 1 arm mode=1 irq=476\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [44.0, 46.8], reopen=True)
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.misses, ["nomatch"])
        self.assertFalse(any("wake press" in n for n in self.notes))

    def test_second_listen_quick_press_counts(self) -> None:
        # 07:48:24Z: next listen, no new panel-on, its own down was quick.
        self._wake_case([
            "AUTH 1 arm mode=1 irq=428\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=241 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [22.0, 22.2], reopen=True)
        self.assertEqual(self.w.policy.failed, 0)
        self._wake_case([
            "AUTH 1 arm mode=1 irq=439\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=236 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [24.5, 24.6])
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.misses, ["nomatch"])

    def test_stop_before_arm_keeps_the_wake_slot(self) -> None:
        # 08:09:09Z: panel off while the session was still starting.
        self.w._wake_open = True
        proc = FakeProc(["booting\n"], then_exit=True)
        self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertTrue(self.w._wake_open)
        self._wake_case([
            "AUTH 1 arm mode=1 irq=614\n",
            "AUTH 1 REAL DOWN itype=0x2\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], [15.1, 15.3])
        self.assertEqual(self.w.policy.failed, 0)

    def test_arm_does_not_notify(self) -> None:
        sent: list[str] = []
        self.w.notify = lambda text: sent.append(text)  # type: ignore[method-assign]
        proc = FakeProc([
            "AUTH 1 arm mode=1 irq=4\n",
            "AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11\n",
            "session_exit:2\n",
        ], then_exit=True)
        self.w.follow("c4", proc, lambda: True, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(sent, [])
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
            # Locked, screen off: keep the sensor armed for the wake press.
            self.assertTrue(self.w.still_wanted("c4", lock))
        self.assertEqual(len(asks), 1)

    def test_unlock_before_arm_stops_at_once(self) -> None:
        notes: list[str] = []
        self.w.note = lambda text: notes.append(text)  # type: ignore[method-assign]
        self.w._lock_state = lambda: False
        self.w._clock = lambda: 0.0  # type: ignore[method-assign]
        proc = FakeProc(["booting\n"])
        t0 = time.monotonic()
        out = self.w.follow("c4", proc, lambda: False, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.w._listen_why, "seat unlocked")
        self.assertFalse(any("holding" in n for n in notes))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_armed_flicker_holds_then_stops(self) -> None:
        ticks = [0.0]
        calls = [0]
        notes: list[str] = []
        self.w.note = lambda text: notes.append(text)  # type: ignore[method-assign]
        self.w._clock = lambda: ticks[0]  # type: ignore[method-assign]
        self.w._lock_state = lambda: False

        def wanted() -> bool:
            calls[0] += 1
            if calls[0] >= 2:
                ticks[0] = 2.0
            return False

        proc = FakeProc(["AUTH 1 arm mode=1 irq=4\n"])
        t0 = time.monotonic()
        out = self.w.follow("c4", proc, wanted, poll_s=0.01)  # type: ignore[arg-type]
        self.assertEqual(out, "stop")
        self.assertEqual(self.w._listen_why, "seat unlocked")
        self.assertTrue(any("holding armed session: seat unlocked" in n for n in notes))
        self.assertLess(time.monotonic() - t0, 1.0)


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
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        self.w.policy = UnlockPolicy(
            require_pin=False, marker=None, clock=lambda: self.now[0],
            state=Path(state_dir.name) / "fp5-qtee-lockout",
        )
        self.events: list[tuple] = []
        self.w.notify = lambda text: self.events.append(("notify", text))  # type: ignore[method-assign]
        self.w.note = lambda text: None  # type: ignore[method-assign]
        self.w.unlock = lambda sid, fid: self.events.append(("unlock", sid, fid)) or True  # type: ignore[method-assign]
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
        warm.say(
            "AUTH HIT 2 itype=0x2 avgv=304 fid=1403494260 rc=0",
            "SERVE result 0", "SERVE idle",
        )
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
        warm.say(
            "AUTH HIT 2 itype=0x2 avgv=304 fid=1403494260 rc=0",
            "SERVE result 0", "SERVE idle",
        )
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])

    def test_stale_hit_number_does_not_unlock(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.panel[0] = False
        self.step()
        warm.say("AUTH 1 cancelled", "SERVE result 3", "SERVE idle")
        self.step()
        self.panel[0] = True
        self.step()
        self.assertEqual(warm.sent, ["auth", "cancel", "auth"])
        warm.say("AUTH HIT 1 itype=0x2 avgv=304 fid=1403494260 rc=0")
        self.step()
        self.assertEqual(self.unlocks(), [])
        warm.say("AUTH HIT 2 itype=0x2 avgv=304 fid=1403494260 rc=0")
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])

    def test_failed_unlock_does_not_reset_or_buzz(self) -> None:
        self.w.policy.on_miss()
        self.w.policy.on_miss()
        failed = self.w.policy.failed
        buzzed: list[str] = []
        self.w.tell_hit = lambda: buzzed.append("hit")  # type: ignore[method-assign]
        self.w.unlock = lambda sid, fid: UnlockWatcher.unlock(self.w, sid, fid)  # type: ignore[method-assign]
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()

        def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["loginctl"], 1)

        with mock.patch.object(fp5_qtee_unlock.subprocess, "run", run):
            warm.say(self.HIT)
            self.step()
        self.assertEqual(self.unlocks(), [])
        self.assertEqual(buzzed, [])
        self.assertEqual(self.w.policy.failed, failed)
        self.assertTrue(self.loop.believed_locked)

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

    def arm_dark(self) -> None:
        wakes: list[str] = []
        self.wakes = wakes

        def is_locked(sid: str) -> bool | None:
            self.lock_asks += 1
            return self.lock[0]

        self.loop = WarmLoop(
            self.w,
            panel=lambda: self.panel[0],
            is_locked=is_locked,
            clock=lambda: self.now[0],
            dark_arm=True,
            wake=lambda: wakes.append("wake"),
        )

    def test_dark_arm_sends_auth_while_the_panel_is_off(self) -> None:
        self.arm_dark()
        self.panel[0] = False
        self.step()
        warm = self.started[0]
        warm.say("SERVE ready", "SERVE idle")
        self.step()
        self.assertEqual(warm.sent, ["auth"])
        self.assertEqual(self.wakes, [])

    def test_dark_miss_does_not_wake_or_unlock(self) -> None:
        self.arm_dark()
        self.panel[0] = False
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        warm.say(self.FAIL, "SERVE result 2", "SERVE idle")
        self.step()
        self.assertEqual(self.w.policy.failed, 1)
        self.assertEqual(self.wakes, [])
        self.assertEqual(self.unlocks(), [])
        self.assertIn(("miss", "nomatch"), self.events)

    def test_dark_hit_unlocks_without_waking_the_panel(self) -> None:
        self.arm_dark()
        self.panel[0] = False
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        warm.say(self.HIT)
        self.step()
        self.assertEqual(self.wakes, [])
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])


class DarkDecisionTest(unittest.TestCase):
    def test_should_arm_and_wake(self) -> None:
        self.assertTrue(fp5_qtee_unlock.should_arm(True, True, False))
        self.assertFalse(fp5_qtee_unlock.should_arm(False, True, False))
        self.assertTrue(fp5_qtee_unlock.should_arm(False, True, True))
        self.assertFalse(fp5_qtee_unlock.should_arm(True, False, True))
        self.assertFalse(fp5_qtee_unlock.should_wake_panel(False, False, True))
        self.assertFalse(fp5_qtee_unlock.should_wake_panel(True, False, False))
        self.assertFalse(fp5_qtee_unlock.should_wake_panel(True, True, True))
        self.assertFalse(fp5_qtee_unlock.should_wake_panel(True, False, True))

    def test_request_panel_on_unblanks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bl = root / "bl_power"
            bl.write_text("4")
            dpms_dir = root / "card0-DSI-1"
            dpms_dir.mkdir()
            dpms = dpms_dir / "dpms"
            dpms.write_text("Off\n")
            fp5_qtee_unlock.request_panel_on(bl, root)
            self.assertEqual(bl.read_text(), "0")
            self.assertEqual(dpms.read_text(), "on\n")


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


class _FakeSignals:
    alive = True

    def __init__(self) -> None:
        self.pending = False

    def poll(self) -> bool:
        fired, self.pending = self.pending, False
        return fired

    def take_sleep_events(self) -> list[bool]:
        return []

    def close(self) -> None:
        pass


class WarmPinCreditTest(unittest.TestCase):
    """A short PIN unlock (unlock, glance, lock) must refresh the PIN.

    Regression: with logind signals the unlocked poll was 15 s, so an
    unlock shorter than that never held for LOCK_FLICKER_S in two
    samples, the marker went stale and after 72 h the finger stayed off.
    """

    step = WarmLoopTest.step

    def setUp(self) -> None:
        WarmLoopTest.setUp(self)  # type: ignore[arg-type]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        marker = Path(tmp.name) / "fp5-qtee-pin-ok"
        marker.write_text("1.0\n")
        self.now[0] = 1.0 + 73 * 3600.0
        self.w.policy = UnlockPolicy(
            require_pin=True, marker=marker, clock=lambda: self.now[0],
            state=lockout_state_for(marker),
        )
        self.sig = _FakeSignals()
        self.loop.signals = self.sig  # type: ignore[assignment]

    def run_for(self, seconds: float, dt: float = 0.2) -> None:
        end = self.now[0] + seconds
        while self.now[0] < end:
            self.step(dt)

    def check_short_unlock(self, panel_on: bool) -> None:
        self.run_for(2.0)
        self.assertEqual(self.w.policy.why_not(), "pin-72h")
        self.lock[0] = False             # PIN entered
        self.sig.pending = True
        self.run_for(4.0)
        self.panel[0] = panel_on
        self.lock[0] = True              # locked again after 4 s
        self.sig.pending = True
        self.run_for(2.0)
        self.assertIsNone(self.w.policy.why_not())

    def test_short_pin_unlock_panel_on(self) -> None:
        self.check_short_unlock(True)

    def test_short_pin_unlock_then_dark(self) -> None:
        self.loop.dark_arm = True
        self.check_short_unlock(False)

    def test_flicker_is_still_not_a_pin(self) -> None:
        self.run_for(2.0)
        self.lock[0] = False
        self.sig.pending = True
        self.step(0.2)
        self.lock[0] = True
        self.sig.pending = True
        self.run_for(3.0)
        self.assertEqual(self.w.policy.why_not(), "pin-72h")


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
        warm.say("AUTH 1 arm mode=1 irq=428",
                 "AUTH 1 REAL DOWN itype=0x2", self.FAIL,
                 "SERVE result 2", "SERVE idle")
        self.step()                           # down at the arm line: wake finger
        self.assertEqual(self.w.policy.failed, 0)
        self.assertNotIn(("miss", "nomatch"), self.events)
        self.assertFalse(self.loop.wake_open)
        warm.say("AUTH 2 arm mode=1 irq=439",
                 "AUTH 1 REAL DOWN itype=0x2", self.FAIL,
                 "SERVE result 2", "SERVE idle")
        self.step()                           # next listen, still quick
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
        warm.say("AUTH 1 arm mode=1 irq=476",
                 "AUTH 1 REAL DOWN itype=0x2", self.HIT,
                 "SERVE result 0", "SERVE idle")
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])

    def test_auth_does_not_notify(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.assertEqual(warm.sent, ["auth"])
        self.assertEqual([e for e in self.events if e[0] == "notify"], [])


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



class FakeInhibitor:
    def __init__(self) -> None:
        self.taken = 0
        self.released = 0

    def take(self) -> bool:
        self.taken += 1
        return True

    def release(self) -> None:
        self.released += 1


class FakeSleepSignals(FakeSignals):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[bool] = []

    def take_sleep_events(self) -> list[bool]:
        out, self.events = self.events, []
        return out


class TouchWakeTest(WarmLoopTest.__base__):  # type: ignore[misc]
    HIT = WarmLoopTest.HIT
    step = WarmLoopTest.step
    unlocks = WarmLoopTest.unlocks

    def setUp(self) -> None:
        WarmLoopTest.setUp(self)  # type: ignore[arg-type]
        self.screen: list[str] = []
        self.w.screen_on = lambda: self.screen.append("on")  # type: ignore[method-assign]
        self.sig = FakeSleepSignals()

        def is_locked(sid: str) -> bool | None:
            return self.lock[0]

        self.loop = WarmLoop(self.w, panel=lambda: self.panel[0],
                             is_locked=is_locked, clock=lambda: self.now[0],
                             signals=self.sig,  # type: ignore[arg-type]
                             dark_arm=True, touch_wake=True)
        self.loop.nap = lambda s: None
        self.delay = FakeInhibitor()
        self.resume = FakeInhibitor()
        self.loop.delay_lock = self.delay  # type: ignore[assignment]
        self.loop.resume_lock = self.resume  # type: ignore[assignment]

    def test_dark_hit_unlocks_then_turns_screen_on(self) -> None:
        self.panel[0] = False
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.assertEqual(warm.sent, ["auth"])
        warm.say("AUTH 1 arm mode=1 irq=4", "AUTH 1 REAL DOWN itype=0x2", self.HIT)
        self.step()
        self.assertEqual(self.unlocks(), [("unlock", "c4", 1403494260)])
        self.assertEqual(self.screen, ["on"])
        self.assertEqual(self.resume.released, 1)

    def test_lit_hit_does_not_call_screen_on(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        warm.say(self.HIT)
        self.step()
        self.assertEqual(len(self.unlocks()), 1)
        self.assertEqual(self.screen, [])

    def test_sleep_arms_idle_session_then_releases_delay_lock(self) -> None:
        self.panel[0] = False
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle", "SERVE result 3")
        # A result came in: the session is idle, nothing armed yet.
        self.loop.drain(False)
        self.loop.armed = False
        warm.idle = True
        warm.say("AUTH 2 arm mode=1 irq=5")
        self.sig.events = [True]
        self.step()
        self.assertIn("auth", warm.sent)
        self.assertEqual(self.delay.released, 1)
        self.sig.events = [False]
        self.step()
        self.assertEqual(self.delay.taken, 1)
        self.assertEqual(self.resume.taken, 1)

    def test_sleep_with_no_session_still_releases(self) -> None:
        self.lock[0] = False
        self.sig.events = [True]
        self.step()
        self.assertEqual(self.delay.released, 1)
        self.sig.events = [False]
        self.step()
        self.assertEqual(self.resume.taken, 0)   # unlocked: no hold


class TouchWakeHelpersTest(unittest.TestCase):
    def test_sleep_signal(self) -> None:
        f = fp5_qtee_unlock.sleep_signal
        self.assertTrue(f("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)"))
        self.assertFalse(f("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)"))
        self.assertIsNone(f("org.freedesktop.login1.Session.Lock ()"))

    def test_inhibit_and_screen_args(self) -> None:
        a = fp5_qtee_unlock.inhibit_args("delay", None)
        self.assertEqual(a[:3], ["systemd-inhibit", "--what=sleep", "--mode=delay"])
        self.assertEqual(a[-2:], ["sleep", "infinity"])
        self.assertEqual(fp5_qtee_unlock.inhibit_args("block", 6)[-2:], ["sleep", "6"])
        s = fp5_qtee_unlock.screen_on_args()
        self.assertIn("org.gnome.ScreenSaver.SetActive", s)
        self.assertEqual(s[-1], "false")

    def test_inhibitor_take_release(self) -> None:
        spawned: list[list[str]] = []

        class P:
            def __init__(self) -> None:
                self.rc: int | None = None

            def poll(self) -> int | None:
                return self.rc

            def terminate(self) -> None:
                self.rc = -15

            def wait(self, timeout: float = 0) -> int:
                return self.rc or 0

        def spawn(argv: list[str]) -> P:
            spawned.append(argv)
            return P()

        inh = fp5_qtee_unlock.Inhibitor("delay", spawn=spawn)  # type: ignore[arg-type]
        self.assertTrue(inh.take())
        self.assertTrue(inh.take())
        self.assertEqual(len(spawned), 1)
        self.assertTrue(inh.held())
        inh.release()
        self.assertFalse(inh.held())
        inh.take()
        self.assertEqual(len(spawned), 2)

    def test_lock_signals_collects_sleep_events(self) -> None:
        sig = fp5_qtee_unlock.LockSignals(["sh", "-c", "echo 'x PrepareForSleep (true,)'; echo 'x PrepareForSleep (false,)'"])
        for _ in range(50):
            sig.poll()
            if len(sig.sleep_events) >= 2:
                break
            time.sleep(0.02)
        self.assertEqual(sig.take_sleep_events(), [True, False])
        sig.close()


class FastTapTest(WarmLoopTest.__base__):  # type: ignore[misc]
    """2026-10-26 fast-tap: keep warm, wake-miss buzz, too fast, wait."""

    HIT = WarmLoopTest.HIT
    FAIL = WarmLoopTest.FAIL
    step = WarmLoopTest.step
    unlocks = WarmLoopTest.unlocks
    _unlock_by_finger = WarmRelockTest._unlock_by_finger

    def setUp(self) -> None:
        WarmLoopTest.setUp(self)  # type: ignore[arg-type]
        self.sig = FakeSignals()
        self.loop.signals = self.sig  # type: ignore[assignment]
        self.loop.keep_warm = True
        self.loop.wake_buzz = True
        self.app = [False]
        self.loop.app_running = lambda: self.app[0]
        self.w.tell_toofast = lambda: self.events.append(("toofast",))  # type: ignore[method-assign]
        self.w.tell_wake_miss = lambda: self.events.append(("wake-miss",))  # type: ignore[method-assign]
        self.notes: list[str] = []
        self.w.note = lambda text: self.notes.append(text)  # type: ignore[method-assign]

    def test_unlock_keeps_session_and_next_lock_arms_at_once(self) -> None:
        warm = self._unlock_by_finger()
        self.assertFalse(warm.closed)
        self.assertIs(self.loop.warm, warm)
        self.lock[0] = True
        self.sig.pending = True
        self.step()
        self.assertEqual(len(self.started), 1)   # no reload
        self.assertEqual(warm.sent[-1], "auth")

    def test_unlocked_never_arms_kept_session(self) -> None:
        warm = self._unlock_by_finger()
        sent = list(warm.sent)
        warm.say("SERVE idle")
        for _ in range(10):
            self.step()
        self.assertEqual(warm.sent, sent)

    def test_finger_app_frees_the_sensor(self) -> None:
        warm = self._unlock_by_finger()
        self.app[0] = True
        self.step(1.1)
        self.step()
        self.assertTrue(warm.closed)
        self.assertIsNone(self.loop.warm)
        self.assertTrue(any("finger app running" in n for n in self.notes))

    def test_app_check_only_while_unlocked(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.app[0] = True
        self.step()
        self.step(1.1)
        self.assertFalse(warm.closed)

    def test_keep_warm_off_quits_on_unlock(self) -> None:
        self.loop.keep_warm = False
        warm = self._unlock_by_finger()
        self.assertTrue(warm.closed)

    def _wake_miss(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.panel[0] = False
        self.step()
        self.panel[0] = True
        self.step()
        warm.say("AUTH 1 arm mode=1 irq=428",
                 "AUTH 1 REAL DOWN itype=0x2", self.FAIL,
                 "SERVE result 2", "SERVE idle")
        self.step()

    def test_wake_press_miss_buzzes_without_strike(self) -> None:
        self._wake_miss()
        self.assertEqual(self.w.policy.failed, 0)
        self.assertIn(("wake-miss",), self.events)
        self.assertNotIn(("miss", "nomatch"), self.events)
        self.assertTrue(any("(buzzed)" in n for n in self.notes))

    def test_wake_press_buzz_can_be_turned_off(self) -> None:
        self.loop.wake_buzz = False
        self._wake_miss()
        self.assertNotIn(("wake-miss",), self.events)

    def test_toofast_ticks_and_is_not_a_strike(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        warm.say("AUTH 1 REAL DOWN itype=0x2",
                 "AUTH 1 toofast avgv=866,865,867")
        self.step()
        self.assertEqual(self.events.count(("toofast",)), 1)
        self.assertEqual(self.w.policy.failed, 0)
        self.assertNotIn(("miss", "partial"), self.events)

    def test_toofast_after_cancel_is_silent(self) -> None:
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        self.panel[0] = False
        self.step()                              # cancel
        warm.say("AUTH 1 toofast avgv=866,865,867")
        self.step()
        self.assertNotIn(("toofast",), self.events)

    def test_dark_toofast_is_rate_limited(self) -> None:
        self.loop.dark_arm = True
        self.panel[0] = False
        self.step()
        warm = self.started[0]
        warm.say("SERVE idle")
        self.step()
        for n in range(1, 4):
            warm.say(f"AUTH {n} toofast avgv=866,865,867",
                     "SERVE result 2", "SERVE idle")
            self.step(1.0)
        self.assertEqual(self.events.count(("toofast",)), 1)
        self.now[0] += fp5_qtee_unlock.DARK_TOOFAST_EVERY_S
        warm.say("AUTH 9 toofast avgv=866,865,867")
        self.step()
        self.assertEqual(self.events.count(("toofast",)), 2)


class FastTapHelpersTest(unittest.TestCase):
    def test_toofast_line(self) -> None:
        self.assertTrue(fp5_qtee_unlock.is_toofast("AUTH 3 toofast avgv=1,2,3\n"))
        self.assertFalse(fp5_qtee_unlock.is_toofast("AUTH 3 empty avgv=1,2,3"))
        self.assertIsNone(miss_kind("AUTH 3 toofast avgv=1,2,3"))

    def test_app_running(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "12").mkdir()
            (base / "12" / "cmdline").write_bytes(b"python3\0/x/fp5-qtee-app.py\0")
            (base / "self").mkdir()
            self.assertTrue(fp5_qtee_unlock.app_running(base))
            (base / "12" / "cmdline").write_bytes(b"python3\0other.py\0")
            self.assertFalse(fp5_qtee_unlock.app_running(base))
        self.assertFalse(fp5_qtee_unlock.app_running(Path("/nonexistent-fp5")))

    def test_serve_running_and_wait(self) -> None:
        busy = "  12 sudo -n /k/fp5-qtee-session serve /lib/firmware/qsee\n"
        self.assertTrue(fp5_qtee_unlock.serve_running(busy))
        self.assertFalse(fp5_qtee_unlock.serve_running(
            "  13 /k/fp5-qtee-session enroll /lib/firmware/qsee\n"))
        outs = [busy, busy, ""]
        now = [0.0]
        ok = fp5_qtee_unlock.wait_serve_gone(
            4.0, ps=lambda: outs.pop(0), clock=lambda: now[0],
            nap=lambda s: now.__setitem__(0, now[0] + s))
        self.assertTrue(ok)
        now[0] = 0.0
        ok = fp5_qtee_unlock.wait_serve_gone(
            1.0, ps=lambda: busy, clock=lambda: now[0],
            nap=lambda s: now.__setitem__(0, now[0] + s))
        self.assertFalse(ok)

    def test_take_line_prefers_pending(self) -> None:
        import collections
        warm = FakeWarm()
        warm.pending = collections.deque(["a\n"])  # type: ignore[attr-defined]
        warm.say("b")
        self.assertEqual(fp5_qtee_unlock.take_line(warm), "a\n")
        self.assertEqual(fp5_qtee_unlock.take_line(warm), "b\n")
        with self.assertRaises(queue.Empty):
            fp5_qtee_unlock.take_line(warm)

    def test_note_has_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(fp5_qtee_unlock, "LOG_DIR", Path(root)):
                UnlockWatcher().note("x")
            text = (Path(root) / "unlock-watcher.log").read_text()
        self.assertRegex(text, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} x\n$")


class WarmWaitTest(unittest.TestCase):
    def test_wait_line_wakes_on_output_and_keeps_it(self) -> None:
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 0.1; echo AUTH HIT; exec sleep 5"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        warm = WarmSession(proc, None)
        t0 = time.monotonic()
        self.assertTrue(warm.wait_line(3.0))
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(fp5_qtee_unlock.take_line(warm), "AUTH HIT\n")
        t0 = time.monotonic()
        self.assertFalse(warm.wait_line(0.1))
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)


if __name__ == "__main__":
    unittest.main()
