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
# Android's lockout (AOSP FingerprintService): 5 rejected fingers lock the
# sensor for 30 s, 20 lock it until the PIN is used.
LOCKOUT_EVERY = 5
LOCKOUT_S = 30.0
LOCKOUT_PERMANENT = 20
# Android also wants the PIN after a restart and at least every 72 h.
PIN_MAX_AGE_S = 72 * 3600.0
REQUIRE_PIN = os.environ.get("FP5_QTEE_REQUIRE_PIN_AFTER_BOOT", "1") != "0"
POLICY_TEXT = {
    "pin-after-boot": "Unlock with your PIN once to turn on fingerprint unlock.",
    "pin-72h": "Fingerprint unlock needs your PIN every 72 hours.",
    "lockout": "Too many attempts. Try again in 30 seconds, or use your PIN.",
    "lockout-permanent": "Too many attempts. Unlock with your PIN.",
}
# Warm mode keeps one "serve" session loaded while the phone is locked,
# so a wake only has to arm the sensor. Opt in until it is proven on more
# phones: FP5_QTEE_WARM=1.
WARM = os.environ.get("FP5_QTEE_WARM", "0") == "1"
# After the panel goes off, ask LockedHint once a second this many times,
# so the session can load while the screen is dark. Then stay quiet.
OFF_LOCK_CHECKS = 10
WARM_RETRY_S = 5.0
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


def boottime() -> float:
    """Seconds since boot, counting suspend. Resets on reboot."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def default_pin_marker() -> Path | None:
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not os.path.isdir(run):
        return None
    return Path(run) / "fp5-qtee-pin-ok"


class UnlockPolicy:
    """When a finger may unlock. Pure state, so it can be tested.

    - The PIN (any unlock that was not ours) must have been used once
      this boot, and within the last 72 h. It is remembered in a file in
      XDG_RUNTIME_DIR, which is a tmpfs, so a reboot forgets it. The file
      holds CLOCK_BOOTTIME, so a wall-clock jump does not change the age.
    - 5 rejected fingers in a row: 30 s lockout. 20: until the PIN.
    - Only a scored non-match counts. A partial press does not.
    """

    def __init__(self, require_pin: bool = REQUIRE_PIN,
                 marker: Path | None = None,
                 clock: Callable[[], float] = boottime) -> None:
        self.require_pin = require_pin
        self.marker = marker
        self.clock = clock
        self.failed = 0
        self.locked_until = 0.0
        self.prev_locked: bool | None = None
        self._pin_at: float | None = None

    def pin_at(self) -> float | None:
        if self.marker is not None:
            try:
                value = float(self.marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                value = None
            # A value from the future is from an earlier boot.
            if value is not None and value <= self.clock():
                return value
            return None
        return self._pin_at

    def _pin_seen(self) -> None:
        now = self.clock()
        self._pin_at = now
        self.failed = 0
        self.locked_until = 0.0
        if self.marker is not None:
            try:
                self.marker.write_text(f"{now:.3f}\n", encoding="utf-8")
            except OSError:
                pass

    def observe_lock(self, is_locked: bool) -> None:
        """Feed LockedHint. An unlock that was not ours means the PIN."""
        was = self.prev_locked
        self.prev_locked = is_locked
        if is_locked:
            return
        if was is False:
            return
        # Unlocked now, and locked before (or first look). on_hit already
        # marked our own unlock as unlocked, so this one was the PIN.
        self._pin_seen()

    def on_hit(self) -> None:
        """Our unlock. It must not count as the PIN."""
        self.failed = 0
        self.locked_until = 0.0
        self.prev_locked = False

    def on_miss(self) -> None:
        self.failed += 1
        if self.failed % LOCKOUT_EVERY == 0:
            self.locked_until = self.clock() + LOCKOUT_S

    def why_not(self) -> str | None:
        """None when a finger may unlock now, else the reason."""
        now = self.clock()
        if self.require_pin:
            at = self.pin_at()
            if at is None:
                return "pin-after-boot"
            if now - at > PIN_MAX_AGE_S:
                return "pin-72h"
        if self.failed >= LOCKOUT_PERMANENT:
            return "lockout-permanent"
        if now < self.locked_until:
            return "lockout"
        return None


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
        self.policy = UnlockPolicy(marker=default_pin_marker())
        self._told_reason: str | None = None

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
                    if self.policy.why_not() is not None:
                        outcome = "stop"
                        break
                    outcome = "hit"
                    self.policy.on_hit()
                    self.feedback(HIT_EVENT)
                    self.unlock(sid, fid)
                    break
                kind = miss_kind(line)
                if kind is not None and wanted():
                    if kind == "nomatch":
                        self.policy.on_miss()
                    self.tell_miss(kind)
                    if self.policy.why_not() is not None:
                        outcome = "stop"
                        break
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

    def start_warm(self) -> WarmSession | None:
        """Load qcomtee if needed and start a serve session."""
        if OLD_DRIVER.is_dir():
            return None
        binary = session_bin()
        if binary is None or other_session_running(None):
            return None
        if self.prepare() is not None:
            return None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.note("warm session start")
        return WarmSession.spawn(binary, LOG_DIR / f"finger-serve-{stamp}.txt")

    def run_warm(self) -> None:
        self.note("watcher start (warm)")
        loop = WarmLoop(self)
        try:
            while True:
                loop.step()
                time.sleep(PANEL_POLL_S)
        finally:
            loop.stop()

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
                self.policy.observe_lock(False)
                self._told_this_lock = False
                self._told_reason = None
                continue
            self.policy.observe_lock(True)
            reason = self.policy.why_not()
            if reason is not None:
                # The sensor is not armed at all, so nothing is spent.
                if reason != self._told_reason:
                    self._told_reason = reason
                    self.note(f"not listening: {reason}")
                    self.notify(POLICY_TEXT[reason])
                continue
            self._told_reason = None
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


class WarmSession:
    """One fp5-qtee-session in serve mode. Commands in, lines out."""

    def __init__(self, proc: subprocess.Popen[str], log_path: Path | None) -> None:
        self.proc = proc
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.idle = False
        self.cancel_sent = False
        self._log = None
        if log_path is not None:
            try:
                self._log = log_path.open("w", encoding="utf-8")
            except OSError:
                self._log = None
        assert proc.stdout is not None
        threading.Thread(
            target=pump_lines, args=(proc.stdout, self.lines), daemon=True
        ).start()

    @classmethod
    def spawn(cls, binary: Path, log_path: Path | None) -> "WarmSession":
        proc = subprocess.Popen(
            ["sudo", "-n", str(binary), "serve", FIRMWARE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        return cls(proc, log_path)

    def send(self, cmd: str) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError, AssertionError):
            pass

    def log(self, line: str) -> None:
        if self._log is not None:
            try:
                self._log.write(line)
                self._log.flush()
            except OSError:
                pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        """quit, then EOF, then signals."""
        self.send("quit")
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self._log is not None:
            self._log.close()
            self._log = None


class WarmLoop:
    """Drives a WarmSession from the panel and lock state.

    - Lock seen (also right after the panel goes off): start the session.
      It loads the trustlet and then blocks on stdin, so a dark locked
      phone spends nothing on it.
    - Panel on and locked: send auth. After each result the session says
      "SERVE idle" and is armed again, so presses can follow each other.
    - Panel off: send cancel. Unlocked: quit the session, which frees the
      sensor for the Finger app.
    A hit unlocks only if the panel is on, logind still says locked, and
    the policy allows it.
    """

    def __init__(self, watcher: "UnlockWatcher",
                 panel: Callable[[], bool] = panel_on,
                 is_locked: Callable[[str], bool] = locked,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.w = watcher
        self.panel = panel
        self.is_locked = is_locked
        self.clock = clock
        self.warm: WarmSession | None = None
        self.armed = False
        self.believed_locked = False
        self.prev_panel: bool | None = None
        self.off_checks = 0
        self.next_lock_ask = 0.0
        self.retry_at = 0.0
        self.sid: str | None = None

    def start(self) -> None:
        if self.warm is not None or self.clock() < self.retry_at:
            return
        warm = self.w.start_warm()
        if warm is None:
            self.retry_at = self.clock() + WARM_RETRY_S
            return
        self.warm = warm
        self.armed = False

    def stop(self) -> None:
        if self.warm is not None:
            self.w.note("warm session quit")
            self.warm.close()
        self.warm = None
        self.armed = False

    def drain(self, panel: bool) -> None:
        warm = self.warm
        if warm is None:
            return
        dead = False
        while True:
            try:
                line = warm.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                dead = True
                break
            warm.log(line)
            if line.startswith("SERVE idle"):
                warm.idle = True
                warm.cancel_sent = False
                self.armed = False
                continue
            fid = hit_fid(line)
            if fid is not None:
                self.on_hit(fid, panel)
                continue
            kind = miss_kind(line)
            if kind is not None and panel and self.believed_locked:
                if kind == "nomatch":
                    self.w.policy.on_miss()
                self.w.tell_miss(kind)
        if dead or not warm.alive():
            self.w.note("warm session ended")
            self.stop()
            self.retry_at = self.clock() + WARM_RETRY_S

    def on_hit(self, fid: int, panel: bool) -> None:
        sid = self.sid
        if not panel or not self.believed_locked or sid is None:
            return
        if self.w.policy.why_not() is not None:
            return
        if not self.is_locked(sid):
            return
        self.w.policy.on_hit()
        self.w.feedback(HIT_EVENT)
        self.w.unlock(sid, fid)
        self.believed_locked = False
        self.w._told_this_lock = False

    def ask_lock(self) -> bool:
        self.sid = self.w.session_id()
        now_locked = self.sid is not None and self.is_locked(self.sid)
        self.w.policy.observe_lock(now_locked)
        return now_locked

    def step(self) -> None:
        now = self.clock()
        panel = self.panel()
        self.drain(panel)
        if not panel:
            if self.armed and self.warm is not None and not self.warm.cancel_sent:
                self.warm.send("cancel")
                self.warm.cancel_sent = True
            if self.prev_panel:
                self.off_checks = OFF_LOCK_CHECKS
                self.next_lock_ask = now
            self.prev_panel = False
            if self.off_checks and self.warm is None and now >= self.next_lock_ask:
                self.off_checks -= 1
                self.next_lock_ask = now + 1.0
                if self.ask_lock():
                    self.believed_locked = True
                    self.off_checks = 0
                    self.start()
            return
        if not self.prev_panel:
            # Waking. With a warm session the lock was seen before the
            # panel went off; arm now and confirm within LOCK_POLL_S.
            self.next_lock_ask = now if self.warm is None else now + LOCK_POLL_S
        self.prev_panel = True
        if now >= self.next_lock_ask:
            self.next_lock_ask = now + LOCK_POLL_S
            self.believed_locked = self.ask_lock()
            if not self.believed_locked:
                self.stop()
                self.w._told_this_lock = False
                self.w._told_reason = None
                return
        if not self.believed_locked:
            return
        reason = self.w.policy.why_not()
        if reason is not None:
            if self.armed and self.warm is not None and not self.warm.cancel_sent:
                self.warm.send("cancel")
                self.warm.cancel_sent = True
            if reason != self.w._told_reason:
                self.w._told_reason = reason
                self.w.note(f"not listening: {reason}")
                self.w.notify(POLICY_TEXT[reason])
            return
        self.w._told_reason = None
        self.start()
        warm = self.warm
        if warm is not None and warm.idle and not self.armed:
            warm.send("auth")
            warm.idle = False
            self.armed = True
            if not self.w._told_this_lock:
                self.w._told_this_lock = True
                self.w.notify("Hold the power button to unlock. PIN still works.")


def main() -> None:
    watcher = UnlockWatcher()
    if WARM:
        watcher.run_warm()
    else:
        watcher.run()


if __name__ == "__main__":
    main()
