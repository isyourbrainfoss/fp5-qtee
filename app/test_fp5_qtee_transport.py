import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import fp5_qtee_transport as t


class SocketSessionTest(unittest.TestCase):
    def test_mode_line_then_line_protocol(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sock"
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(path))
            srv.listen(1)
            got = []

            def serve():
                c, _ = srv.accept()
                f = c.makefile("rwb", buffering=0)
                got.append(f.readline())
                f.write(b"SERVE idle\n")
                got.append(f.readline())
                f.write(b"AUTH HIT 1\n")
                c.shutdown(socket.SHUT_RDWR)
                c.close()

            th = threading.Thread(target=serve)
            th.start()
            s = t.SocketSession("serve", path=path)
            self.assertIsNone(s.pid)
            self.assertEqual(s.stdout.readline(), "SERVE idle\n")
            s.stdin.write("auth\n")
            self.assertEqual(s.stdout.readline(), "AUTH HIT 1\n")
            th.join(2)
            self.assertEqual(s.wait(timeout=2), 0)
            self.assertEqual(got, [b"MODE serve\n", b"auth\n"])
            s.kill()
            srv.close()

    def test_transport_choice(self):
        old = os.environ.get("FP5_QTEE_TRANSPORT")
        try:
            os.environ["FP5_QTEE_TRANSPORT"] = "sudo"
            self.assertEqual(t.transport(), "sudo")
            os.environ["FP5_QTEE_TRANSPORT"] = "socket"
            self.assertEqual(t.transport(), "socket")
        finally:
            if old is None:
                os.environ.pop("FP5_QTEE_TRANSPORT", None)
            else:
                os.environ["FP5_QTEE_TRANSPORT"] = old


if __name__ == "__main__":
    unittest.main()
