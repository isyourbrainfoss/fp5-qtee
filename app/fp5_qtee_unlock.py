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
# A locked->unlocked change this soon after our own unlock-session is ours,
# not the PIN. Phosh clears LockedHint a little after logind unlocks.
OUR_UNLOCK_GRACE_S = 10.0
REQUIRE_PIN = os.environ.get("FP5_QTEE_REQUIRE_PIN_AFTER_BOOT", "1") != "0"
POLICY_TEXT = {
    "pin-after-boot": "Unlock with your PIN once to turn on fingerprint unlock.",
    "pin-72h": "Fingerprint unlock needs your PIN every 72 hours.",
    "lockout": "Too many attempts. Try again in 30 seconds, or use your PIN.",
    "lockout-permanent": "Too many attempts. Unlock with your PIN.",
}
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


def lockout_state_for(marker: Path | None) -> Path | None:
    """The lockout state lives next to the PIN marker, on the same tmpfs."""
    if marker is None:
        return None
    return marker.with_name("fp5-qtee-lockout")


def _write_atomic(path: Path, text: str) -> None:
    """Write a 0600 file so a crash mid-write never leaves half of it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class UnlockPolicy:
    """When a finger may unlock. Pure state, so it can be tested.

    - The PIN must have been used once this boot, and within the last
      72 h. It is remembered in a file in XDG_RUNTIME_DIR, which is a
      tmpfs, so a reboot forgets it. The file holds CLOCK_BOOTTIME, so a
      wall-clock jump does not change the age.
    - Only a confirmed locked -> unlocked change between two LockedHint
      samples of the same session counts as the PIN. An unknown sample
      (loginctl timed out, failed, or printed nothing) is never an unlock
      and breaks the chain. The first sample is never evidence of the PIN.
      A change within OUR_UNLOCK_GRACE_S of our own unlock is ours.
    - 5 rejected fingers in a row: 30 s lockout. 20: until the PIN.
      The count and the lockout end are kept in a state file next to the
      PIN marker, so a watcher crash or restart does not clear them.
      A state file that cannot be read, or a miss that cannot be saved,
      locks fingerprint unlock until the PIN (fail closed).
    - Only a scored non-match counts. A partial press does not.
    """

    def __init__(self, require_pin: bool = REQUIRE_PIN,
                 marker: Path | None = None,
                 clock: Callable[[], float] = boottime,
                 state: Path | None = None) -> None:
        self.require_pin = require_pin
        self.marker = marker
        self.state = state
        self.clock = clock
        self.failed = 0
        self.locked_until = 0.0
        self.prev_locked: bool | None = None
        self._pin_at: float | None = None
        self._our_unlock_at: float | None = None
        # True while the newest strike count is not on disk.
        self._unsaved = False
        self._load_state()

    # -- persistence -------------------------------------------------

    def _load_state(self) -> None:
        if self.state is None:
            return
        try:
            text = self.state.read_text(encoding="utf-8")
        except FileNotFoundError:
            # No strikes saved. A PIN this boot writes the file, so a
            # PIN marker without it means it was removed: fail closed.
            if self.marker is not None and self.marker.exists():
                self.failed = LOCKOUT_PERMANENT
            return
        except OSError:
            self.failed = LOCKOUT_PERMANENT
            return
        parts = text.split()
        try:
            failed = int(parts[0])
            until = float(parts[1])
        except (IndexError, ValueError):
            self.failed = LOCKOUT_PERMANENT
            return
        if failed < 0:
            failed = LOCKOUT_PERMANENT
        self.failed = failed
        # Never wait longer than one timed lockout from now.
        self.locked_until = min(until, self.clock() + LOCKOUT_S)

    def _save_state(self) -> None:
        if self.state is None:
            self._unsaved = False
            return
        try:
            _write_atomic(self.state, f"{self.failed} {self.locked_until:.3f}\n")
        except OSError:
            self._unsaved = True
            return
        self._unsaved = False

    # -- PIN ---------------------------------------------------------

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
        self._save_state()
        if self.marker is not None:
            try:
                _write_atomic(self.marker, f"{now:.3f}\n")
            except OSError:
                pass

    def forget_session(self) -> None:
        """A different (or unknown) session: no transition carries over."""
        self.prev_locked = None

    def observe_lock(self, is_locked: bool | None) -> None:
        """Feed one LockedHint sample: True, False, or None for unknown."""
        was = self.prev_locked
        self.prev_locked = is_locked
        if is_locked is None or is_locked:
            return
        # Unlocked now. Only a confirmed locked sample right before it
        # makes this an unlock we saw happen.
        if was is not True:
            return
        ours_at = self._our_unlock_at
        self._our_unlock_at = None
        if ours_at is not None and self.clock() - ours_at <= OUR_UNLOCK_GRACE_S:
            return
        self._pin_seen()

    # -- fingers -----------------------------------------------------

    def on_hit(self) -> None:
        """Our unlock. It must not count as the PIN."""
        self.failed = 0
        self.locked_until = 0.0
        self._our_unlock_at = self.clock()
        self._save_state()

    def on_miss(self) -> None:
        self.failed += 1
        if self.failed % LOCKOUT_EVERY == 0:
            self.locked_until = self.clock() + LOCKOUT_S
        self._save_state()

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
        if self._unsaved and self.failed:
            # A restart would forget this strike. Do not risk it.
            return "lockout-permanent"
        if now < self.locked_until:
            return "lockout"
        return None


def notify_args(text: str, timeout_ms: int, transient: bool = False) -> list[str]:
    """notify-send command that puts the message in the summary.

    The Phosh lock screen shows only the summary of a notification, so the
    sentence itself must be the summary. No body is passed.
    """
    args = ["notify-send", "-a", "Fingerprint", "-t", str(timeout_ms)]
    if transient:
        args += ["-h", "boolean:transient:true"]
    return args + [text]


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
    return parse_locked(show_text) is True


def parse_locked(show_text: str) -> bool | None:
    """LockedHint from loginctl output: True, False, or None if absent."""
    for line in show_text.splitlines():
        line = line.strip()
        if line == "LockedHint=yes":
            return True
        if line == "LockedHint=no":
            return False
    return None


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


def locked(sid: str) -> bool | None:
    """LockedHint of sid. None when unknown: loginctl timed out, failed
    (for example a stale session id), or printed no LockedHint. Callers
    must treat None as neither locked nor unlocked."""
    try:
        proc = subprocess.run(
            ["loginctl", "show-session", sid, "-p", "LockedHint"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return parse_locked(proc.stdout)


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
        marker = default_pin_marker()
        self.policy = UnlockPolicy(marker=marker, state=lockout_state_for(marker))
        self._policy_sid: str | None = None
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
                notify_args(text, 6000),
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
        self._background(notify_args(MISS_TEXT[kind], 2500, transient=True))

    def session_id(self) -> str | None:
        sid = self._sid.get()
        if sid is None:
            # Phosh may just have registered. Ask again next time.
            self._sid.forget()
        if sid != self._policy_sid:
            # LockedHint changes of another session say nothing about
            # this one.
            self._policy_sid = sid  # type: ignore[assignment]
            self.policy.forget_session()
        return sid  # type: ignore[return-value]

    def lock_state(self, sid: str) -> bool | None:
        """locked(sid), feeding the policy. Unknown re-reads the session id."""
        state = locked(sid)
        self.policy.observe_lock(state)
        if state is None:
            self._sid.forget()
        return state

    def still_wanted(self, sid: str, lock_check: Throttle) -> bool:
        """Checked while a session waits for a finger.

        The panel is read every time. LockedHint is asked at most once
        per LOCK_POLL_S, because each ask is a loginctl process.
        """
        if OLD_DRIVER.is_dir():
            return False
        if not panel_on():
            return False
        # Unknown (None) is not locked: stop rather than guess.
        return lock_check.get() is True

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
                if kind is not None:
                    # A scored non-match is a strike even if the panel
                    # went off meanwhile; only the feedback is skipped.
                    if kind == "nomatch":
                        self.policy.on_miss()
                    if wanted():
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
            state = self.lock_state(sid)
            if state is None:
                # loginctl hung, failed, or the id is stale. Neither an
                # unlock nor a reason to listen.
                continue
            if not state:
                self._told_this_lock = False
                self._told_reason = None
                continue
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


def main() -> None:
    UnlockWatcher().run()


if __name__ == "__main__":
    main()
