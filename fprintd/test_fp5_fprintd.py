import json
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


class SyncHelpersTest(unittest.TestCase):
    def test_sync_owner(self):
        self.assertEqual(f.sync_owner("fp5", ["fp5"], configured=None), "fp5")
        self.assertEqual(f.sync_owner(None, ["fp5"], configured=None), "fp5")
        self.assertEqual(f.sync_owner("kb", ["fp5", "kb"], configured=None), "kb")
        self.assertEqual(f.sync_owner("x", [], configured=None), f.DEFAULT_USER)
        self.assertEqual(f.sync_owner("fp5", ["fp5"], configured="user"), "user")

    def test_want_marker(self):
        with tempfile.TemporaryDirectory() as d:
            m = Path(d) / "sub" / "want-sensor"
            f.want_sensor(True, m)
            self.assertTrue(m.is_file())
            f.want_sensor(False, m)
            self.assertFalse(m.exists())
            f.want_sensor(False, m)  # already gone: no error

    def test_wait_sessions_gone(self):
        seq = [[5], [5], []]
        t = [0.0]
        def clock():
            return t[0]
        def sleep(s):
            t[0] += s
        self.assertTrue(f.wait_sessions_gone(5, pids=lambda: seq.pop(0), clock=clock, sleep=sleep))
        t[0] = 0.0
        self.assertFalse(f.wait_sessions_gone(1, pids=lambda: [5], clock=clock, sleep=sleep))

    def test_free_order10(self):
        text = ("Node 0, zone      DMA    84     67     52     48     38     75     67     47     16      0      0 \n"
                "Node 0, zone   Normal    252    808    371    169     33    198     22     27      0      0      0 \n")
        self.assertEqual(f.free_order10(text), 0)
        text2 = text.replace("16      0      0 \n", "16      4      5 \n", 1)
        self.assertEqual(f.free_order10(text2), 5)
        self.assertEqual(f.free_order10(""), 0)

    def test_free_order10_zones(self):
        text = ("Node 0, zone      DMA    1 1 1 1 1 1 1 1 1 1 4 \n"
                "Node 0, zone   Normal    1 1 1 1 1 1 1 1 1 1 1 \n")
        self.assertEqual(f.free_order10(text), 5)
        self.assertEqual(f.free_order10(text, zones=("Normal",)), 1)

    def test_memory_ok_for_yield(self):
        seq = [1, 3]
        compacted = []
        self.assertTrue(f.memory_ok_for_yield(free=lambda: seq.pop(0),
                                              compact=lambda: compacted.append(1), need=2))
        self.assertEqual(compacted, [1])
        self.assertFalse(f.memory_ok_for_yield(free=lambda: 0, compact=lambda: None, need=2))
        self.assertFalse(f.memory_ok_for_yield(free=lambda: -1, compact=lambda: None, need=2))
        self.assertTrue(f.memory_ok_for_yield(free=lambda: 5, compact=lambda: self.fail("compacted"), need=2))

    def test_map_sync_to_owner(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fingers.json"
            path.write_text('{"fingers": []}')
            m = f.FingerMap(path)
            self.assertEqual(m.names("fp5"), [])
            self.assertTrue(m.sync([1088502210], "fp5"))
            self.assertEqual(m.names("fp5"), ["right-index-finger"])
            self.assertEqual(json.loads(path.read_text())["fingers"][0]["user"], "fp5")


if __name__ == "__main__":
    unittest.main()
