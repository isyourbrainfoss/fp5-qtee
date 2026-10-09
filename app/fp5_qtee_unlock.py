#!/usr/bin/env python3
"""While the Phosh lock screen is up and the panel is on, match once.

A real AUTH HIT asks logind to unlock that Phosh session. A miss does not.
The screen being off does not listen, so a pocket press cannot unlock.
That is checked while the session waits for a finger too, not only when
the session prints a line. Does not load qsee_fingerpr and does not use
the Phosh PAM stack.
"""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

SESSION_CANDIDATES = (
    Path("/home/user/fp5-qtee-keep/session/fp5-qtee-session"),
    Path("/tmp/fp5-qtee/fp5-qtee-session"),
)
LOAD_SCRIPT = Path("/home/user/fp5-qtee-keep/load-qcomtee.sh")
FIRMWARE = "/lib/firmware/qsee"
LOG_DIR = Path("/home/user/fp5-qtee-keep/logs")
OLD_DRIVER = Path("/sys/module/qsee_fingerpr")
DSI_DPMS = Path("/sys/class/drm")
BACKLIGHT = Path("/sys/class/backlight/ae94000.dsi.0/bl_power")
POISON = {0, 0xAAAAAAAA, 0xA5A5A5A5}
# Panel state is a sysfs read, so it is cheap to look at often. LockedHint
# needs a loginctl process, so it is asked less often.
PANEL_POLL_S = 0.2
LOCK_POLL_S = 1.0
SESSION_ID_TTL_S = 30.0
_HIT = re.compile(r"^AUTH HIT \d+ .* fid=(\d+) ")
_ARM = re.compile(r"^AUTH \d+ arm ")
# auth_once prints these after a real finger-down (itype 0x2):
#   AUTH FAIL <n> itype=0x2 ...   the image was scored and did not match
#   AUTH <n> empty avgv=...       the finger was too light or partial
# An AUTH FAIL with itype 0x0 is the arm command failing, not a press.
_NOMATCH = re.compile(r"^AUTH FAIL \d+ itype=0x2 ")
_PARTIAL = re.compile(r"^AUTH \d+ empty ")
# feedbackd event for a press that did not unlock. The standard event
# names have no "authentication failed" yet. bell-terminal is a single
# 100 ms rumble in the default theme's quiet profile, with no sound in
# full, and nothing in silent. Override with FP5_QTEE_MISS_EVENT, or set
# it empty to turn the haptic off.
MISS_EVENT = os.environ.get("FP5_QTEE_MISS_EVENT", "bell-terminal")
HIT_EVENT = os.environ.get("FP5_QTEE_HIT_EVENT", "")
FEEDBACK_APP_ID = "org.fp5.qtee"
MISS_TEXT = {
    "nomatch": "Not recognized. Try again, or use your PIN.",
    "partial": "Finger not read. Cover the whole sensor and hold still.",
}


def hit_fid(line: str) -> int | None:
    match = _HIT.match(line.strip())
    if match is None:
        return None
    fid = int(match.group(1))
    if fid in POISON:
        return None
    return fid


def armed(line: str) -> bool:
    return _ARM.match(line.strip()) is not None


def miss_kind(line: str) -> str | None:
    """nomatch or partial for a real press that did not unlock, else None."""
    text = line.strip()
    if _NOMATCH.match(text):
        return "nomatch"
    if _PARTIAL.match(text):
        return "partial"
    return None


def feedback_args(event: str) -> list[str] | None:
    """fbcli command for a feedbackd event, or None when turned off."""
    if not event:
        return None
    return ["fbcli", "-A", FEEDBACK_APP_ID, "-E", event]


def pump_lines(stream: Iterable[str], out: "queue.Queue[str | None]") -> None:
    """Copy each line into the queue, then None at end of stream."""
    try:
        for line in stream:
            out.put(line)
    except (OSError, ValueError):
        pass
    out.put(None)


class Throttle:
    """Remembers a value and asks again only after ttl seconds."""

    def __init__(self, ttl: float, fetch: Callable[[], object],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._fetch = fetch
        self._clock = clock
        self._at: float | None = None
        self._value: object = None

    def get(self) -> object:
        now = self._clock()
        if self._at is None or now - self._at >= self._ttl:
            self._value = self._fetch()
            self._at = now
        return self._value

    def forget(self) -> None:
        self._at = None


def screen_is_on(dpms: str | None, bl_power: str | None) -> bool:
    """Panel power. dpms Off means do not listen. Missing both means off."""
    if dpms is not None:
        return dpms.strip().lower() == "on"
    if bl_power is not None:
        return bl_power.strip() == "0"
    return False


def choose_phosh(props: dict[str, dict[str, str]]) -> str | None:
    """The seat session Phosh registered, preferring the active one."""
    fallback = None
    for sid, row in props.items():
        if row.get("Desktop") != "phosh":
            continue
        if row.get("Type") not in (None, "", "wayland"):
            continue
        if row.get("Active") == "yes":
            return sid
        fallback = sid
    return fallback


def is_locked(show_text: str) -> bool:
    return "LockedHint=yes" in show_text


def session_bin() -> Path | None:
    for path in SESSION_CANDIDATES:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _run(args: list[str], timeout: float = 5) -> str:
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.stdout


def phosh_session_id() -> str | None:
    listed = _run(["loginctl", "list-sessions", "--no-legend"])
    props: dict[str, dict[str, str]] = {}
    for line in listed.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        show = _run(
            [
                "loginctl",
                "show-session",
                sid,
                "-p",
                "Desktop",
                "-p",
                "Type",
                "-p",
                "Active",
            ]
        )
        row: dict[str, str] = {}
        for item in show.splitlines():
            if "=" in item:
                key, value = item.split("=", 1)
                row[key] = value
        props[sid] = row
    return choose_phosh(props)


def read_dpms() -> str | None:
    paths = sorted(DSI_DPMS.glob("card*-DSI-*/dpms"))
    if not paths:
        return None
    try:
        return paths[0].read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_bl_power() -> str | None:
    try:
        return BACKLIGHT.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def panel_on() -> bool:
    return screen_is_on(read_dpms(), read_bl_power())


def locked(sid: str) -> bool:
    try:
        text = _run(["loginctl", "show-session", sid, "-p", "LockedHint"])
    except subprocess.TimeoutExpired:
        return False
    return is_locked(text)


def other_session_running(our_pid: int | None) -> bool:
    try:
        out = _run(["ps", "-eo", "pid,args"], timeout=3)
    except subprocess.TimeoutExpired:
        return False
    for line in out.splitlines():
        if "fp5-qtee-session" not in line:
            continue
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if our_pid is not None and pid == our_pid:
            continue
        return True
    return False


class UnlockWatcher:
    def __init__(self) -> None:
        self._log = None
        self._sid = Throttle(SESSION_ID_TTL_S, phosh_session_id)
        self._told_this_lock = False

    def note(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {text}\n"
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR / "unlock-watcher.log").open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def notify(self, text: str) -> None:
        try:
            subprocess.run(
                ["notify-send", "-t", "6000", "Fingerprint", text],
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _background(self, args: list[str]) -> None:
        def go() -> None:
            try:
                subprocess.run(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        threading.Thread(target=go, daemon=True).start()

    def feedback(self, event: str) -> None:
        """Haptic through feedbackd, so the user's profile applies."""
        args = feedback_args(event)
        if args is not None:
            self._background(args)

    def tell_miss(self, kind: str) -> None:
        """A finger was read and did not unlock. Say so right away."""
        self.note(f"miss {kind}")
        self.feedback(MISS_EVENT)
        # Transient, so misses do not pile up in the notification list.
        self._background(
            ["notify-send", "-h", "boolean:transient:true", "-t", "2500",
             "-a", "Fingerprint", "Fingerprint", MISS_TEXT[kind]]
        )

    def session_id(self) -> str | None:
        sid = self._sid.get()
        if sid is None:
            # Phosh may just have registered. Ask again next time.
            self._sid.forget()
        return sid  # type: ignore[return-value]

    def still_wanted(self, sid: str, lock_check: Throttle) -> bool:
        """Checked while a session waits for a finger.

        The panel is read every time. LockedHint is asked at most once
        per LOCK_POLL_S, because each ask is a loginctl process.
        """
        if OLD_DRIVER.is_dir():
            return False
        if not panel_on():
            return False
        return bool(lock_check.get())

    def unlock(self, sid: str, fid: int) -> None:
        self.note(f"unlock session {sid} fid {fid}")
        subprocess.run(
            ["loginctl", "unlock-session", sid],
            check=False,
            timeout=5,
        )

    def spawn(self, binary: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                "sudo",
                "-n",
                "timeout",
                "-k",
                "10",
                "120",
                str(binary),
                "unlock",
                FIRMWARE,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    def prepare(self) -> str | None:
        """Load qcomtee if needed. Returns an outcome to stop with, or None."""
        if LOAD_SCRIPT.is_file():
            loaded = subprocess.run(
                ["sudo", "-n", str(LOAD_SCRIPT)],
                check=False,
                timeout=40,
            )
            if loaded.returncode != 0:
                self.note(f"load-qcomtee exit {loaded.returncode}")
                return "error"
        if OLD_DRIVER.is_dir():
            self.note("qsee_fingerpr loaded; not listening")
            return "stop"
        return None

    def listen_once(self, sid: str) -> str:
        """Run one unlock attempt. Returns hit, miss, stop, or error."""
        binary = session_bin()
        if binary is None:
            return "error"
        early = self.prepare()
        if early is not None:
            return early
        lock_check = Throttle(LOCK_POLL_S, lambda: locked(sid))
        return self.follow(
            sid,
            self.spawn(binary),
            lambda: self.still_wanted(sid, lock_check),
        )

    def follow(self, sid: str, proc: subprocess.Popen[str],
               wanted: Callable[[], bool], poll_s: float = PANEL_POLL_S) -> str:
        """Read the session's lines until a hit, its exit, or not wanted.

        The session prints nothing while it waits for a finger, so the
        wait is on a queue with a timeout. Each timeout re-checks the
        panel and the lock. Turning the panel off or unlocking with the
        PIN stops the session at once instead of after its 90 s budget.
        """
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = LOG_DIR / f"finger-unlock-{stamp}.txt"
        lines: queue.Queue[str | None] = queue.Queue()
        assert proc.stdout is not None
        reader = threading.Thread(
            target=pump_lines, args=(proc.stdout, lines), daemon=True
        )
        reader.start()
        outcome = "miss"
        with path.open("w", encoding="utf-8") as log:
            while True:
                try:
                    line = lines.get(timeout=poll_s)
                except queue.Empty:
                    if not wanted():
                        outcome = "stop"
                        break
                    continue
                if line is None:
                    break
                log.write(line)
                log.flush()
                if not self._told_this_lock and armed(line):
                    self._told_this_lock = True
                    self.notify("Hold the power button to unlock. PIN still works.")
                fid = hit_fid(line)
                if fid is not None:
                    # A press that landed just as the panel went off
                    # must not unlock.
                    if not wanted():
                        outcome = "stop"
                        break
                    outcome = "hit"
                    self.feedback(HIT_EVENT)
                    self.unlock(sid, fid)
                    break
                kind = miss_kind(line)
                if kind is not None and wanted():
                    self.tell_miss(kind)
                if not wanted():
                    outcome = "stop"
                    break
        self._stop(proc)
        reader.join(timeout=2)
        return outcome

    def _stop(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait(timeout=5)

    def run(self) -> None:
        """Idle cheaply until the panel is on, then ask logind.

        The panel is a sysfs read every PANEL_POLL_S, so a wake is seen
        within about 0.2 s. loginctl runs only while the panel is on.
        """
        self.note("watcher start")
        last_lock_ask = 0.0
        while True:
            if not panel_on():
                # Ask logind as soon as the panel comes back on.
                last_lock_ask = 0.0
                time.sleep(PANEL_POLL_S)
                continue
            now = time.monotonic()
            if now - last_lock_ask < LOCK_POLL_S and last_lock_ask:
                time.sleep(PANEL_POLL_S)
                continue
            last_lock_ask = now
            if OLD_DRIVER.is_dir() or session_bin() is None:
                continue
            sid = self.session_id()
            if sid is None:
                continue
            if not locked(sid):
                self._told_this_lock = False
                continue
            if other_session_running(None):
                time.sleep(LOCK_POLL_S)
                continue
            self.note(f"listen session {sid}")
            began = time.monotonic()
            outcome = self.listen_once(sid)
            self.note(f"listen done {outcome}")
            last_lock_ask = 0.0
            if outcome == "hit":
                self._told_this_lock = False
                time.sleep(2.0)
            elif outcome == "error":
                time.sleep(5.0)
            elif outcome == "miss" and time.monotonic() - began < 3.0:
                # A session that exits at once is failing, not missing.
                # Do not spin on it.
                time.sleep(1.0)


def main() -> None:
    UnlockWatcher().run()


if __name__ == "__main__":
    main()
