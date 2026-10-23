"""UI flow test for fp5_fingerprints.py against the fake-session daemon.

    gtk4-broadwayd :5 &            # any GTK display works
    BROADWAY_DISPLAY=:5 GDK_BACKEND=broadway dbus-run-session -- python3 test_ui.py
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

HERE = Path(__file__).resolve().parent
os.environ["FP5_FINGERPRINTS_BUS"] = "session"
sys.path.insert(0, str(HERE))
import fp5_fingerprints as ui  # noqa: E402


def pump(cond, timeout=10.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.02)
    return False


def rows(win):
    return [r.get_title() for r in win._rows]


class UiFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        (t / "store").mkdir()
        (t / "store" / "f.bin").write_bytes(b"x")
        env = dict(os.environ, FP5_FPRINTD_TEST="1",
                   FP5_FPRINTD_SESSION=str(HERE.parent / "testdata" / "fake-session"),
                   FP5_FPRINTD_STATE=str(t / "state"), FP5_FPRINTD_LOCK=str(t / "lock"),
                   FP5_FPRINTD_FAKE_LOCKED=str(t / "locked"), FP5_FPRINTD_LOAD="/nonexistent",
                   FP5_FPRINTD_STORE=str(t / "store"), FAKE_SEQ_FILE=str(t / "seq"),
                   FP5_FPRINTD_USER=os.environ.get("USER", "kb"))
        cls.lockflag = t / "locked"
        cls.seq = t / "seq"
        cls.daemon = subprocess.Popen([sys.executable, str(HERE.parent / "fp5_fprintd.py")], env=env,
                                      stderr=open(t / "daemon.log", "w"))
        import dbus
        bus = dbus.SessionBus()
        for _ in range(50):
            if bus.name_has_owner("net.reactivated.Fprint"):
                break
            time.sleep(0.1)
        Adw.init()
        cls.app = Adw.Application(application_id="org.fp5.FingerprintsTest")
        cls.app.register(None)
        cls.win = ui.Window(cls.app, ui.Client())
        cls.win.present()

    @classmethod
    def tearDownClass(cls):
        cls.daemon.terminate()
        cls.daemon.wait(5)
        cls.tmp.cleanup()

    def test_0_lists_fingers(self):
        self.assertTrue(pump(lambda: len(self.win._rows) == 2))
        self.assertEqual(sorted(rows(self.win)), ["Right index finger", "Right thumb"])
        self.assertIn("2 fingers enrolled", self.win.status.get_description())
        self.assertTrue(self.win.add_row.get_sensitive())

    def test_1_rename_and_swap(self):
        self.win._do_rename("right-thumb", "left-thumb")
        self.assertTrue(pump(lambda: "Left thumb" in rows(self.win)))
        self.win._do_rename("left-thumb", "right-index-finger")   # swap
        self.assertTrue(pump(lambda: sorted(rows(self.win)) == ["Left thumb", "Right index finger"]))
        self.win._do_rename("left-thumb", "right-thumb")           # back to the original names
        self.assertTrue(pump(lambda: sorted(rows(self.win)) == ["Right index finger", "Right thumb"]))
        self.win._do_rename("right-thumb", "right-thumb")          # same: no-op, no crash
        self.assertTrue(pump(lambda: len(self.win._rows) == 2))

    def test_2_test_finger(self):
        self.seq.write_text("hit")
        self.win._start_test()
        self.assertTrue(pump(lambda: self.win.op_label is not None and self.win.op == "verify"))
        self.assertTrue(pump(lambda: "Match" in self.win.op_label.get_label()))
        self.assertTrue(pump(lambda: self.win.op is None))
        self.assertEqual(self.win.op_button.get_label(), "Done")
        self.win._op_button()
        self.assertTrue(pump(lambda: len(self.win._rows) == 2))

    def test_3_locked_is_explained(self):
        # Locked phone: the watcher owns the sensor, VerifyStart is refused.
        # The app must stay on the main page (and show a toast), not hang.
        self.lockflag.write_text("1")
        try:
            self.win._start_test()
            pump(lambda: False, 1.5)
        finally:
            self.lockflag.unlink()
        self.assertEqual(self.win.nav.get_visible_page().get_tag(), "main")
        self.assertIsNone(self.win.op if self.win.op != "verify" else None)
        self.win.op = None

    def test_4_enroll_add_and_delete(self):
        self.win._do_delete("right-index-finger")
        self.assertTrue(pump(lambda: rows(self.win) == ["Right thumb"], 20))
        self.win._start_enroll("left-index-finger")
        self.assertTrue(pump(lambda: self.win.op == "enroll" and self.win.op_bar is not None))
        self.assertTrue(pump(lambda: self.win.op is None, 20))
        self.assertIn("Done", self.win.op_label.get_label())
        self.assertGreater(self.win.op_bar.get_fraction(), 0)
        self.win._op_button()
        self.assertTrue(pump(lambda: sorted(rows(self.win)) == ["Left index finger", "Right thumb"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
