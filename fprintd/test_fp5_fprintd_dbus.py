"""D-Bus flow test: run under dbus-run-session. Starts fp5_fprintd.py on
the session bus with a fake session binary and drives it like pam_fprintd
and GNOME Settings do."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

HERE = Path(__file__).resolve().parent
DBusGMainLoop(set_as_default=True)


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        (t / "store").mkdir()
        (t / "store" / "f.bin").write_bytes(b"x")
        cls.lockflag = t / "locked"
        env = dict(os.environ, FP5_FPRINTD_TEST="1",
                   FP5_FPRINTD_SESSION=str(HERE / "testdata" / "fake-session"),
                   FP5_FPRINTD_STATE=str(t / "state"), FP5_FPRINTD_LOCK=str(t / "lock"),
                   FP5_FPRINTD_FAKE_LOCKED=str(cls.lockflag), FP5_FPRINTD_LOAD="/nonexistent",
                   FP5_FPRINTD_STORE=str(t / "store"), FAKE_SEQ_FILE=str(t / "seq"), FP5_FPRINTD_USER=os.environ.get("USER", "kb"),
                   FP5_FPRINTD_LOCKOUT_FILE=str(t / "fp5-qtee-lockout"),
                   FP5_FPRINTD_HONOR_LOCKOUT="1")
        cls.lockout = t / "fp5-qtee-lockout"
        cls.env = env
        cls.daemon = subprocess.Popen([sys.executable, str(HERE / "fp5_fprintd.py")], env=env,
                                      stderr=open(t / "daemon.log", "w"))
        cls.bus = dbus.SessionBus()
        for _ in range(50):
            if cls.bus.name_has_owner("net.reactivated.Fprint"):
                break
            time.sleep(0.1)
        mgr = cls.bus.get_object("net.reactivated.Fprint", "/net/reactivated/Fprint/Manager")
        path = mgr.GetDefaultDevice(dbus_interface="net.reactivated.Fprint.Manager")
        cls.dev = dbus.Interface(cls.bus.get_object("net.reactivated.Fprint", path),
                                 "net.reactivated.Fprint.Device")

    @classmethod
    def tearDownClass(cls):
        cls.daemon.terminate()
        cls.daemon.wait(5)
        print((Path(cls.tmp.name) / "daemon.log").read_text(), file=sys.stderr)
        cls.tmp.cleanup()

    def wait_signal(self, name, until_done=True, timeout=10):
        got = []
        loop = GLib.MainLoop()
        def cb(result, done):
            got.append((str(result), bool(done)))
            if done or not until_done:
                loop.quit()
        m = self.dev.connect_to_signal(name, cb)
        GLib.timeout_add_seconds(timeout, loop.quit)
        return got, loop, m

    def verify(self, seq):
        (Path(self.tmp.name) / "seq").write_text(seq)
        got, loop, m = self.wait_signal("VerifyStatus")
        self.dev.Claim("")
        self.dev.VerifyStart("any")
        loop.run()
        m.remove()
        self.dev.VerifyStop()
        self.dev.Release()
        return got

    def test_0_errors_and_props(self):
        with self.assertRaises(dbus.exceptions.DBusException) as e:
            self.dev.VerifyStart("any")
        self.assertEqual("net.reactivated.Fprint.Error.ClaimDevice", e.exception.get_dbus_name())
        props = dbus.Interface(self.dev, "org.freedesktop.DBus.Properties")
        self.assertEqual(props.Get("net.reactivated.Fprint.Device", "scan-type"), "press")
        with self.assertRaises(dbus.exceptions.DBusException) as e:
            self.dev.ListEnrolledFingers("nobody-here")
        self.assertEqual("net.reactivated.Fprint.Error.NoEnrolledPrints", e.exception.get_dbus_name())

    def test_1_list(self):
        names = self.dev.ListEnrolledFingers("")
        self.assertEqual(sorted(names), ["right-index-finger", "right-thumb"])

    def test_2_verify_match(self):
        got = self.verify("hit")
        self.assertEqual(got[-1], ("verify-match", True))

    def test_3_retry_then_nomatch(self):
        # FAKE_SEQ is read by the fake session at its start.
        got = self.verify("empty,miss")
        self.assertIn(("verify-retry-scan", False), got)
        self.assertEqual(got[-1], ("verify-no-match", True))

    def test_4_locked_refuses(self):
        self.lockflag.write_text("1")
        try:
            self.dev.Claim("")
            with self.assertRaises(dbus.exceptions.DBusException) as e:
                self.dev.VerifyStart("any")
            self.assertEqual("net.reactivated.Fprint.Error.AlreadyInUse", e.exception.get_dbus_name())
        finally:
            self.dev.Release()
            self.lockflag.unlink()

    def test_5_delete_and_enroll(self):
        self.dev.Claim("")
        self.dev.DeleteEnrolledFinger("right-index-finger", timeout=60)
        self.assertEqual(list(self.dev.ListEnrolledFingers("")), ["right-thumb"])
        with self.assertRaises(dbus.exceptions.DBusException) as e:
            self.dev.DeleteEnrolledFinger("right-thumb")  # last finger is refused
        self.assertEqual("net.reactivated.Fprint.Error.PrintsNotDeleted", e.exception.get_dbus_name())
        got, loop, m = self.wait_signal("EnrollStatus")
        self.dev.EnrollStart("left-index-finger")
        loop.run()
        m.remove()
        self.dev.Release()
        self.assertEqual(got[-1], ("enroll-completed", True))
        self.assertIn(("enroll-retry-scan", False), got)
        self.assertEqual(sorted(self.dev.ListEnrolledFingers("")), ["left-index-finger", "right-thumb"])

    def test_6_relabel_and_details(self):
        man = dbus.Interface(self.dev, "org.fp5.Fprint.Manage")
        before = {str(n): int(fid) for n, fid, _ in man.ListDetailed("")}
        self.assertIn("left-index-finger", before)
        fid = before["left-index-finger"]
        self.assertEqual(str(man.RelabelFinger("left-index-finger", "left-thumb")), "renamed")
        self.assertEqual(sorted(self.dev.ListEnrolledFingers("")), ["left-thumb", "right-thumb"])
        after = {str(n): int(x) for n, x, _ in man.ListDetailed("")}
        self.assertEqual(after["left-thumb"], fid)   # same template, new name
        with self.assertRaises(dbus.exceptions.DBusException) as e:
            man.RelabelFinger("left-thumb", "pinky")
        self.assertEqual("net.reactivated.Fprint.Error.InvalidFingername", e.exception.get_dbus_name())
        with self.assertRaises(dbus.exceptions.DBusException) as e:
            man.RelabelFinger("right-index-finger", "left-index-finger")
        self.assertEqual("net.reactivated.Fprint.Error.NoEnrolledPrints", e.exception.get_dbus_name())
        backups = list((Path(self.tmp.name) / "state" / "backup").glob("fingers-*.json"))
        self.assertTrue(backups)               # fingers.json copied before the rename
        self.assertEqual(str(man.RelabelFinger("left-thumb", "left-index-finger")), "renamed")


    def test_7_lockout(self):
        man = "org.fp5.Fprint.Manage"
        props = dbus.Interface(self.dev, "org.freedesktop.DBus.Properties")
        self.assertFalse(props.Get(man, "locked-out"))
        self.assertEqual(props.Get(man, "lockout-reason"), "")
        got = []
        loop = GLib.MainLoop()
        def cb(locked, remaining, reason):
            got.append((bool(locked), int(remaining), str(reason)))
            loop.quit()
        m = self.dev.connect_to_signal("LockoutChanged", cb, dbus_interface=man)
        changed = []
        pm = props.connect_to_signal("PropertiesChanged",
                                     lambda i, c, inv: changed.append((str(i), dict(c))))
        until = time.clock_gettime(time.CLOCK_BOOTTIME) + 25
        self.lockout.write_text(f"5 {until:.3f}\n")
        GLib.timeout_add_seconds(8, loop.quit)
        loop.run()
        self.assertTrue(got and got[-1][0] and got[-1][2] == "lockout", got)
        self.assertTrue(0 < got[-1][1] <= 26)
        self.assertTrue(any(i == man and c.get("locked-out") for i, c in changed), changed)
        self.assertTrue(props.Get(man, "locked-out"))
        self.assertEqual(int(props.Get(man, "failed-attempts")), 5)
        # stock interface unchanged
        self.assertNotIn("locked-out", props.GetAll("net.reactivated.Fprint.Device"))
        # honor mode: verify ends at once with verify-disconnected
        got_v = self.verify("hit")
        self.assertEqual(got_v[-1], ("verify-disconnected", True))
        # PIN used: the watcher rewrites the file with 0 strikes
        loop2 = GLib.MainLoop()
        got.clear()
        def cb2(locked, remaining, reason):
            got.append((bool(locked), int(remaining), str(reason)))
            loop2.quit()
        m.remove()
        m = self.dev.connect_to_signal("LockoutChanged", cb2, dbus_interface=man)
        self.lockout.write_text("0 0.000\n")
        GLib.timeout_add_seconds(8, loop2.quit)
        loop2.run()
        m.remove()
        pm.remove()
        self.assertEqual(got[-1], (False, 0, ""))
        self.assertEqual(self.verify("hit")[-1], ("verify-match", True))


if __name__ == "__main__":
    unittest.main()
