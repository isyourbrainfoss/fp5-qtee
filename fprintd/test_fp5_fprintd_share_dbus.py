"""D-Bus flow for shared verify: run under dbus-run-session.

A fake "watcher session" (testdata/share/fp5-qtee-session, comm
fp5-qtee-sessio) serves the share socket. fp5-fprintd must verify through
it, never create the want-sensor marker and never signal it.
"""
import json
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
USER = os.environ.get("USER", "kb")


class SharedFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.t = t
        (t / "store").mkdir()
        (t / "state").mkdir()
        (t / "state" / "fingers.json").write_text(json.dumps({"fingers": [
            {"fid": 1979259351, "user": USER, "finger": "right-thumb", "source": "test"},
            {"fid": 1558157930, "user": "someone-else", "finger": "right-index-finger", "source": "test"},
        ]}))
        cls.lockflag = t / "locked"
        cls.want = t / "want-sensor"
        name = f"fp5-share-dbus-{os.getpid()}"
        env = dict(os.environ, FP5_FPRINTD_TEST="1",
                   FP5_FPRINTD_SESSION=str(HERE / "testdata" / "fake-session"),
                   FP5_FPRINTD_STATE=str(t / "state"), FP5_FPRINTD_LOCK=str(t / "lock"),
                   FP5_FPRINTD_FAKE_LOCKED=str(cls.lockflag), FP5_FPRINTD_LOAD="/nonexistent",
                   FP5_FPRINTD_STORE=str(t / "store"), FP5_FPRINTD_WANT=str(cls.want),
                   FP5_FPRINTD_USER=USER, FP5_FPRINTD_SHARE_NAME=name,
                   FP5_FPRINTD_SHARE_UID=str(os.getuid()),
                   FAKE_SHARE_LOG=str(t / "share.log"), FAKE_SHARE_SEQ=str(t / "seq"))
        cls.env = env
        cls.server = subprocess.Popen([str(HERE / "testdata" / "share" / "fp5-qtee-session")], env=env)
        for _ in range(50):
            if (t / "share.log").exists():
                break
            time.sleep(0.1)
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
        alive = cls.server.poll() is None
        cls.server.terminate()
        cls.server.wait(5)
        print((cls.t / "daemon.log").read_text(), file=sys.stderr)
        cls.tmp.cleanup()
        assert alive, "fp5-fprintd must never end the watcher's session"

    def wait_signal(self, name, timeout=10):
        got = []
        loop = GLib.MainLoop()

        def cb(result, done):
            got.append((str(result), bool(done)))
            if done:
                loop.quit()
        m = self.dev.connect_to_signal(name, cb)
        GLib.timeout_add_seconds(timeout, loop.quit)
        return got, loop, m

    def verify(self, seq, between=None):
        (self.t / "seq").write_text(seq)
        got, loop, m = self.wait_signal("VerifyStatus")
        self.dev.Claim("")
        self.assertFalse(self.want.exists(), "no handover marker while sharing")
        self.dev.VerifyStart("any")
        if between:
            between()
        loop.run()
        m.remove()
        try:
            self.dev.VerifyStop()
        except dbus.exceptions.DBusException:
            pass
        self.dev.Release()
        self.assertFalse(self.want.exists())
        return got

    def share_log(self):
        return (self.t / "share.log").read_text().split("\n")

    def test_1_match_through_share(self):
        got = self.verify("hit")
        self.assertEqual(got[-1], ("verify-match", True))
        log = (self.t / "daemon.log").read_text()
        self.assertIn("verify through the unlock watcher's session", log)
        self.assertNotIn("order-10", log)
        self.assertNotIn("serve session started", log)  # no own session
        time.sleep(0.3)
        lines = self.share_log()
        self.assertIn("auth", lines)
        self.assertIn("quit", lines)

    def test_2_other_users_finger_is_no_match(self):
        got = self.verify("hit-other")
        self.assertEqual(got[-1], ("verify-no-match", True))

    def test_3_miss(self):
        got = self.verify("miss")
        self.assertEqual(got[-1], ("verify-no-match", True))

    def test_4_seat_locks_during_shared_verify(self):
        def lock():
            time.sleep(0.5)
            self.lockflag.write_text("1")
        try:
            got = self.verify("wait", between=lock)
        finally:
            self.lockflag.unlink()
        self.assertEqual(got[-1], ("verify-disconnected", True))
        time.sleep(0.5)
        self.assertEqual(self.share_log()[-2], "client gone")

    def test_5_locked_does_not_share(self):
        before = self.share_log().count("client")
        self.lockflag.write_text("1")
        try:
            self.dev.Claim("")
            with self.assertRaises(dbus.exceptions.DBusException) as e:
                self.dev.VerifyStart("any")
            self.assertEqual("net.reactivated.Fprint.Error.AlreadyInUse", e.exception.get_dbus_name())
        finally:
            self.dev.Release()
            self.lockflag.unlink()
        self.assertEqual(self.share_log().count("client"), before)


if __name__ == "__main__":
    unittest.main()
