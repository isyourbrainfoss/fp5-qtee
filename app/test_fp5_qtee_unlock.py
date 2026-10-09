#!/usr/bin/env python3
"""Decisions for the Phosh unlock watcher. Run: python3 test_fp5_qtee_unlock.py"""

from __future__ import annotations

import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fp5_qtee_unlock
from fp5_qtee_unlock import (
    Throttle,
    UnlockWatcher,
    armed,
    choose_phosh,
    feedback_args,
    hit_fid,
    is_locked,
    miss_kind,
    pump_lines,
    screen_is_on,
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

    def test_feedback_args(self) -> None:
        self.assertEqual(
            feedback_args("bell-terminal"),
            ["fbcli", "-A", "org.fp5.qtee", "-E", "bell-terminal"],
        )
        self.assertIsNone(feedback_args(""))


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
        self.w.feedback = lambda event: None  # type: ignore[method-assign]

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


if __name__ == "__main__":
    unittest.main()
