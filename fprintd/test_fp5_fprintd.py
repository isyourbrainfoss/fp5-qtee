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


if __name__ == "__main__":
    unittest.main()
