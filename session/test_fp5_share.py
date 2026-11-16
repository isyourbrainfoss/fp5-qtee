#!/usr/bin/env python3
"""Drive test_fp5_share (the serve loop with a fake auth) like the unlock
watcher (stdin/stdout socket) and fp5-fprintd (the share socket) do.

Run: make test_fp5_share && python3 test_fp5_share.py
"""
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "test_fp5_share"


class Lines:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.seen: list[str] = []

    def wait(self, prefix: str, timeout: float = 5.0) -> str:
        end = time.monotonic() + timeout
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                text = line.decode()
                self.seen.append(text)
                if text.startswith(prefix):
                    return text
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {prefix!r}; saw {self.seen}")
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise AssertionError(f"EOF before {prefix!r}; saw {self.seen}")
            self.buf += data

    def quiet(self, secs: float = 0.4) -> list[str]:
        """Lines that arrive within secs (should be none)."""
        self.sock.settimeout(secs)
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            data = b""
        self.buf += data
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            out.append(line.decode())
        return out


class Share(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.finger = Path(self.tmp.name) / "finger"
        self.name = f"fp5-qtee-share-test-{os.getpid()}-{id(self)}"
        self.w, theirs = socket.socketpair()
        env = dict(os.environ, FP5_TEST_FINGER=str(self.finger),
                   FP5_TEST_SHARE_NAME=self.name)
        self.proc = subprocess.Popen([str(BIN)], stdin=theirs, stdout=theirs,
                                     stderr=subprocess.PIPE, env=env)
        theirs.close()
        self.wl = Lines(self.w)
        self.wl.wait("SHARE listen")
        self.wl.wait("SERVE ready")
        self.wl.wait("SERVE idle")

    def tearDown(self) -> None:
        try:
            self.w.close()
        finally:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            if self.proc.stderr:
                self.proc.stderr.close()
            self.tmp.cleanup()

    def client(self) -> tuple[socket.socket, Lines]:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect("\0" + self.name)
        lines = Lines(c)
        lines.wait("SERVE ready")
        lines.wait("SERVE idle")
        return c, lines

    def touch(self) -> None:
        self.finger.write_text("x")

    def wsend(self, cmd: str) -> None:
        self.w.sendall((cmd + "\n").encode())

    def test_shared_hit_goes_to_client_only(self) -> None:
        c, cl = self.client()
        c.sendall(b"auth\n")
        cl.wait("AUTH 100001 arm")
        self.touch()
        hit = cl.wait("AUTH HIT 100001")
        self.assertIn("fid=1979259351", hit)
        self.assertEqual(cl.wait("SERVE result"), "SERVE result 0")
        cl.wait("SERVE idle")
        self.assertEqual(self.wl.quiet(), [], "watcher must see nothing")
        # Watcher numbering is untouched: its first auth is 1.
        self.wsend("auth")
        self.wl.wait("AUTH 1 arm")
        self.touch()
        self.wl.wait("AUTH HIT 1 ")
        self.wl.wait("SERVE result 0")
        self.wl.wait("SERVE idle")
        self.assertEqual(cl.quiet(), [], "client sees nothing of the watcher")
        c.close()

    def test_watcher_auth_preempts_shared(self) -> None:
        c, cl = self.client()
        c.sendall(b"auth\n")
        cl.wait("AUTH 100001 arm")
        mark = len(self.wl.seen)
        self.wsend("auth")
        cl.wait("AUTH 100001 cancelled")
        self.assertEqual(cl.wait("SERVE result"), "SERVE result 3")
        cl.wait("SERVE idle")
        # The watcher's auth runs at once, without a stray SERVE idle first.
        first = self.wl.wait("AUTH")
        self.assertTrue(first.startswith("AUTH 1 arm"), self.wl.seen)
        self.assertNotIn("SERVE idle", self.wl.seen[mark:])
        self.touch()
        self.wl.wait("AUTH HIT 1 ")
        self.wl.wait("SERVE result 0")
        self.wl.wait("SERVE idle")
        c.close()

    def test_watcher_auth_then_cancel_during_shared(self) -> None:
        c, cl = self.client()
        c.sendall(b"auth\n")
        cl.wait("AUTH 100001 arm")
        self.wsend("auth")
        self.wsend("cancel")
        cl.wait("SERVE result 3")
        self.wl.wait("AUTH 1 cancelled")
        self.wl.wait("SERVE result 3")
        self.wl.wait("SERVE idle")
        c.close()

    def test_client_cancel_and_rearm(self) -> None:
        c, cl = self.client()
        c.sendall(b"auth\n")
        cl.wait("AUTH 100001 arm")
        c.sendall(b"cancel\n")
        cl.wait("AUTH 100001 cancelled")
        cl.wait("SERVE result 3")
        cl.wait("SERVE idle")
        c.sendall(b"auth\n")
        cl.wait("AUTH 100002 arm")
        self.touch()
        cl.wait("AUTH HIT 100002")
        self.assertEqual(self.wl.quiet(), [])
        c.close()

    def test_client_gone_mid_attempt(self) -> None:
        c, cl = self.client()
        c.sendall(b"auth\n")
        cl.wait("AUTH 100001 arm")
        c.close()
        time.sleep(0.3)
        self.assertIsNone(self.proc.poll(), "session must survive")
        self.wsend("auth")
        self.wl.wait("AUTH 1 arm")
        self.touch()
        self.wl.wait("AUTH HIT 1 ")
        # A new client can come after the old one left.
        c2, cl2 = self.client()
        c2.sendall(b"auth\n")
        cl2.wait("AUTH 100002 arm")
        c2.close()

    def test_second_client_refused(self) -> None:
        c, cl = self.client()
        c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c2.connect("\0" + self.name)
        Lines(c2).wait("SERVE reject busy")
        c2.close()
        c.close()

    def test_client_auth_waits_for_watcher_attempt(self) -> None:
        c, cl = self.client()
        self.wsend("auth")
        self.wl.wait("AUTH 1 arm")
        c.sendall(b"auth\n")
        self.assertEqual(cl.quiet(), [], "queued, not run, while the watcher's attempt runs")
        self.touch()
        self.wl.wait("AUTH HIT 1 ")
        self.wl.wait("SERVE idle")
        cl.wait("AUTH 100001 arm")
        self.touch()
        cl.wait("AUTH HIT 100001")
        c.close()

    def test_watcher_eof_ends_session_during_shared(self) -> None:
        c, cl = self.client()
        c.sendall(b"auth\n")
        cl.wait("AUTH 100001 arm")
        self.w.shutdown(socket.SHUT_WR)
        self.assertEqual(self.proc.wait(timeout=5), 0)
        c.close()

    def test_client_unknown_line(self) -> None:
        c, cl = self.client()
        c.sendall(b"bench\n")
        cl.wait("SERVE reject unknown")
        c.close()


@unittest.skipUnless(os.geteuid() != 0 and subprocess.run(["sudo", "-n", "true"],
                     capture_output=True).returncode == 0, "needs passwordless sudo")
class OtherUidRefused(unittest.TestCase):
    """The session (root here, as on the phone) serves only its own uid."""

    def test_non_root_client_refused(self) -> None:
        name = f"fp5-qtee-share-uid-{os.getpid()}"
        w, theirs = socket.socketpair()
        p = subprocess.Popen(["sudo", "-n", "env", f"FP5_TEST_SHARE_NAME={name}", str(BIN)],
                             stdin=theirs, stdout=theirs, stderr=subprocess.PIPE)
        theirs.close()
        wl = Lines(w)
        wl.wait("SERVE idle")
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect("\0" + name)
        c.settimeout(3)
        self.assertEqual(c.recv(100), b"", "closed without SERVE ready")
        c.close()
        w.close()
        p.wait(timeout=5)
        err = p.stderr.read().decode()
        p.stderr.close()
        self.assertIn("share client refused (uid", err)


class NoShareOnPipe(unittest.TestCase):
    def test_pipe_stdin_does_not_listen(self) -> None:
        name = f"fp5-qtee-share-pipe-{os.getpid()}"
        p = subprocess.Popen([str(BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             env=dict(os.environ, FP5_TEST_SHARE_NAME=name))
        line = p.stdout.readline()
        while line.startswith("IRQWAIT"):
            line = p.stdout.readline()
        self.assertTrue(line.startswith("SHARE off (stdin is not a socket)"), line)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self.assertRaises(OSError):
            c.connect("\0" + name)
        c.close()
        p.stdin.close()
        p.wait(timeout=5)
        p.stdout.close()

    def test_env_off(self) -> None:
        a, b = socket.socketpair()
        p = subprocess.Popen([str(BIN)], stdin=b, stdout=b, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, FP5_QTEE_SHARE="0"))
        b.close()
        self.assertTrue(Lines(a).wait("SHARE").startswith("SHARE off (FP5_QTEE_SHARE=0)"))
        a.close()
        p.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
