"""How the user-side watcher/app reaches fp5-qtee-session.

Two ways, chosen by what the system offers (no distro checks):

- "socket" (BengalOS, no sudo): connect to the socket-activated system
  service at FP5_QTEE_SOCKET (default /run/fp5-qtee/session.sock). The
  first line says which mode to run ("MODE serve"); the system side checks
  polkit for this caller and then runs the session with the socket as its
  stdin/stdout. After that it is the same line protocol as before.
- "sudo" (Nura, the old way): sudo -n the session binary directly.

FP5_QTEE_TRANSPORT=socket|sudo forces one. Default: socket when the socket
exists, else sudo.
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import time
from pathlib import Path

SOCKET_PATH = Path(os.environ.get("FP5_QTEE_SOCKET", "/run/fp5-qtee/session.sock"))


def transport() -> str:
    forced = os.environ.get("FP5_QTEE_TRANSPORT", "")
    if forced in ("socket", "sudo"):
        return forced
    return "socket" if SOCKET_PATH.exists() else "sudo"


class SocketSession:
    """The subset of subprocess.Popen the watcher uses, over a socket.

    pid is None: there is no local process group to signal. stop() ends
    the remote session by sending quit and closing the connection, which
    the session sees as EOF on stdin (it exits on EOF).
    """

    is_socket = True

    def __init__(self, mode: str, args: list[str] | None = None,
                 path: Path = SOCKET_PATH, timeout: float = 5.0) -> None:
        self.pid = None
        self.returncode: int | None = None
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(path))
        self._sock.settimeout(None)
        line = " ".join(["MODE", mode, *(args or [])]) + "\n"
        self._sock.sendall(line.encode())
        raw_in = self._sock.makefile("wb", buffering=0)
        raw_out = self._sock.makefile("rb", buffering=0)
        self.stdin = io.TextIOWrapper(io.BufferedWriter(raw_in), encoding="utf-8",
                                      line_buffering=True)
        self.stdout = io.TextIOWrapper(io.BufferedReader(raw_out), encoding="utf-8")
        self._eof = False

    def _mark_eof(self) -> None:
        self._eof = True
        if self.returncode is None:
            self.returncode = 0

    def poll(self) -> int | None:
        if self._eof:
            return self.returncode
        try:
            data = self._sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return None
        except OSError:
            self._mark_eof()
            return self.returncode
        if data == b"":
            self._mark_eof()
            return self.returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        end = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if end is not None and time.monotonic() > end:
                raise subprocess.TimeoutExpired("fp5-qtee socket", timeout or 0)
            time.sleep(0.05)
        return self.returncode or 0

    def terminate(self) -> None:
        try:
            self.stdin.write("quit\n")
            self.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def kill(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._mark_eof()


def spawn(binary: Path, mode: str, firmware: str, *, stdin: bool,
          hold_s: int | None = None, args: list[str] | None = None):
    """Start one session. Returns a Popen or a SocketSession."""
    if transport() == "socket":
        return SocketSession(mode, args)
    cmd = ["sudo", "-n"]
    if hold_s is not None:
        cmd += ["timeout", "-k", "10", str(hold_s)]
    cmd += [str(binary), mode, firmware, *(args or [])]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def signal_group(proc, sig: int) -> None:
    """os.killpg for a local session; a socket session is asked to quit."""
    import signal as _signal

    if getattr(proc, "is_socket", False):
        if sig == _signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
        return
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        if sig == _signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
