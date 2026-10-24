import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import fp5_fprintd as f


class EventTest(unittest.TestCase):
    def test_verify_lines(self):
        self.assertEqual(f.verify_event("AUTH HIT 3 itype=0x2 avgv=900 fid=1979259351 rc=0\n"),
                         ("match", 1979259351))
        self.assertEqual(f.verify_event("AUTH FAIL 2 itype=0x2 esd=0 avgv=800 fid=0 rc=-2"),
                         ("no-match", None))
        # itype 0x0 is the arm command failing, not a press
        self.assertIsNone(f.verify_event("AUTH FAIL 2 itype=0x0 esd=0 avgv=0 rc=-5 fid=0"))
        self.assertEqual(f.verify_event("AUTH 4 empty avgv=1,2,3"), ("retry", None))
        self.assertEqual(f.verify_event("AUTH 1 arm mode=1 irq=5"), ("armed", None))
        self.assertEqual(f.verify_event("SERVE result 3"), ("result", 3))
        self.assertIsNone(f.verify_event("SERVE idle"))

    def test_enroll_lines(self):
        self.assertEqual(f.enroll_event("ENROLL fid=123 rc=0 text rsp=0 req=0"), ("fid", 123))
        self.assertEqual(f.enroll_event("rem=7 sample=3"), ("rem", 7))
        self.assertEqual(f.enroll_event("PRESS REJECT avgv=1,2,3"), ("retry", None))
        self.assertEqual(f.enroll_event("enroll done rem=0 wrote=1 save_ms=80"), ("done", 1))
        self.assertEqual(f.enroll_event("enroll done rem=3 wrote=0 save_ms=0"), ("done", 0))
        self.assertEqual(f.enroll_event("ENROLL rejected rc=-3"), ("rejected", None))

    def test_list(self):
        out = "x\nFINGER index=0 gid=0 fid=1558157930 slot=0\nFINGER index=1 gid=0 fid=1979259351 slot=1\n"
        self.assertEqual(f.listed_fids(out), [1558157930, 1979259351])


class MapTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "fingers.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_seed_and_lookup(self):
        m = f.FingerMap(self.path)
        self.assertEqual(sorted(m.names("user")), ["right-index-finger", "right-thumb"])
        self.assertEqual(m.fid("user", "right-thumb"), 1979259351)
        self.assertEqual(m.owner(1558157930), "user")
        self.assertEqual(m.names("other"), [])
        self.assertTrue(self.path.is_file())

    def test_sync_adds_and_drops(self):
        m = f.FingerMap(self.path)
        self.assertTrue(m.sync([1979259351, 42]))
        self.assertIsNone(m.fid("user", "right-index-finger") if m.owner(1558157930) else None)
        self.assertEqual(m.owner(42), "user")
        self.assertEqual(m.fid("user", "right-index-finger"), 42)
        self.assertFalse(m.sync([1979259351, 42]))
        again = f.FingerMap(self.path)
        self.assertEqual(sorted(r["fid"] for r in again.rows), [42, 1979259351])

    def test_add_replaces_name(self):
        m = f.FingerMap(self.path)
        m.add(7, "user", "right-thumb", "enroll")
        self.assertEqual(m.fid("user", "right-thumb"), 7)
        self.assertIsNone(m.owner(1979259351))

    def test_bad_json_reseeds(self):
        self.path.write_text("{nope")
        m = f.FingerMap(self.path)
        self.assertEqual(len(m.rows), 2)
        json.loads(self.path.read_text())

    def test_relabel(self):
        m = f.FingerMap(self.path)
        self.assertEqual(m.relabel("user", "right-thumb", "left-thumb"), "renamed")
        self.assertEqual(m.fid("user", "left-thumb"), 1979259351)
        self.assertIsNone(m.fid("user", "right-thumb"))
        self.assertEqual(m.relabel("user", "left-thumb", "right-index-finger"), "swapped")
        self.assertEqual(m.fid("user", "right-index-finger"), 1979259351)
        self.assertEqual(m.fid("user", "left-thumb"), 1558157930)
        self.assertEqual(m.relabel("user", "left-thumb", "left-thumb"), "same")
        self.assertEqual(f.FingerMap(self.path).fid("user", "left-thumb"), 1558157930)  # persisted
        with self.assertRaises(KeyError):
            m.relabel("user", "right-thumb", "left-thumb")
        with self.assertRaises(ValueError):
            m.relabel("user", "left-thumb", "pinky")
        with self.assertRaises(KeyError):
            m.relabel("other", "left-thumb", "right-thumb")

    def test_details(self):
        m = f.FingerMap(self.path)
        self.assertEqual({d[0] for d in m.details("user")}, {"right-index-finger", "right-thumb"})
        self.assertEqual(m.details("other"), [])


class LockoutTest(unittest.TestCase):
    """lockout_state mirrors the watcher's UnlockPolicy (app/)."""

    def test_states(self):
        now = 1000.0
        st = f.lockout_state(None, now, False)
        self.assertFalse(st["locked-out"])
        self.assertEqual(st["lockout-reason"], "")
        st = f.lockout_state("3 0.000\n", now, True)
        self.assertEqual((st["locked-out"], st["failed-attempts"]), (False, 3))
        st = f.lockout_state("5 1012.200\n", now, True)
        self.assertEqual((st["locked-out"], st["lockout-permanent"], st["lockout-remaining"],
                          st["lockout-reason"]), (True, False, 13, "lockout"))
        # never more than one timed lockout ahead
        self.assertEqual(f.lockout_state("5 99999\n", now, True)["lockout-remaining"], 30)
        st = f.lockout_state("20 0\n", now, True)
        self.assertEqual((st["locked-out"], st["lockout-permanent"], st["lockout-reason"]),
                         (True, True, "lockout-permanent"))
        # fail closed like the watcher
        for text, marker, bad in (("garbage", False, False), ("-1 0", False, False),
                                  (None, True, False), (None, False, True)):
            st = f.lockout_state(text, now, marker, bad)
            self.assertTrue(st["lockout-permanent"], (text, marker, bad))

    def test_read_files(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "fp5-qtee-lockout"
            os.environ["FP5_FPRINTD_LOCKOUT_FILE"] = str(state)
            try:
                self.assertFalse(f.read_lockout("x")["locked-out"])
                state.write_text(f"10 {f.boottime() + 20:.3f}\n")
                st = f.read_lockout("x")
                self.assertTrue(st["locked-out"])
                self.assertTrue(18 <= st["lockout-remaining"] <= 21)
                state.unlink()
                (Path(d) / "fp5-qtee-pin-ok").write_text("1\n")
                self.assertTrue(f.read_lockout("x")["lockout-permanent"])  # marker, no state
            finally:
                del os.environ["FP5_FPRINTD_LOCKOUT_FILE"]

    def test_matches_watcher_policy(self):
        app = Path(__file__).resolve().parent.parent / "app"
        if not (app / "fp5_qtee_unlock.py").is_file():
            self.skipTest("app/ not in this tree")
        sys.path.insert(0, str(app))
        try:
            import fp5_qtee_unlock as w
        finally:
            sys.path.remove(str(app))
        self.assertEqual(w.LOCKOUT_S, f.LOCKOUT_S)
        self.assertEqual(w.LOCKOUT_PERMANENT, f.LOCKOUT_PERMANENT)
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "fp5-qtee-lockout"
            marker = Path(d) / "fp5-qtee-pin-ok"
            clock = [500.0]
            pol = w.UnlockPolicy(require_pin=False, marker=marker, clock=lambda: clock[0], state=state)
            for i in range(25):
                pol.on_miss()
                for dt in (0.0, 10.0, 29.0, 31.0):
                    clock[0] += dt
                    fresh = w.UnlockPolicy(require_pin=False, marker=marker,
                                           clock=lambda: clock[0], state=state)
                    why = fresh.why_not()
                    st = f.lockout_state(state.read_text(), clock[0], marker.exists())
                    self.assertEqual(why if why in ("lockout", "lockout-permanent") else "",
                                     st["lockout-reason"], (i, dt))
                    self.assertEqual(st["locked-out"], why is not None)


if __name__ == "__main__":
    unittest.main()
