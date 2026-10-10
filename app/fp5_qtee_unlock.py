#!/usr/bin/env python3
"""While the Phosh seat is locked, match once.

The session stays armed with the panel off, so the wake press is a new
edge on a sensor calibrated with no finger. A finger already down at
arm is captured by that session. A real AUTH HIT asks logind to unlock
the Phosh session once the panel is on. A hit that leaves the panel off
does not unlock. A miss while the panel stays off is not a strike and
does not wake the screen. Only a hit can unlock. Does not load
qsee_fingerpr and does not use the Phosh PAM stack.
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

from fp5_qtee_haptic import Haptic

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
# The power key is the sensor, so the finger that is already down when the
# first listen after panel-on arms is not a try. The session does not say
# "already down"; it logs AUTH arm and then REAL DOWN. That down is within
# about 300 ms when the IRQ is already pending, and the work-mode command
# sits between the arm line and the wait, so the default grace is 1 s from
# the arm line (not from panel-on). Only that first no-match is ignored.
# A later press counts, even a quick one. A hit still unlocks.
WAKE_GRACE_S = float(os.environ.get("FP5_QTEE_WAKE_GRACE_MS", "1000")) / 1000.0
# A wake hit can be printed before sysfs says the panel is on. Wait this
# long, then unlock only if it came on. A miss that never turns the panel
# on is not a strike.
PANEL_WAKE_WAIT_S = 1.0
# One unlock process may stay armed this long. follow() stops it when the
# seat unlocks. A shorter budget resets and calibrates again while a wake
# finger can already be down.
UNLOCK_HOLD_S = 1800
_HIT = re.compile(r"^AUTH HIT \d+ .* fid=(\d+) ")
_ARM = re.compile(r"^AUTH \d+ arm ")
# auth_once prints these after a real finger-down (itype 0x2):
#   AUTH FAIL <n> itype=0x2 ...   the image was scored and did not match
#   AUTH <n> empty avgv=...       the finger was too light or partial
# An AUTH FAIL with itype 0x0 is the arm command failing, not a press.
_NOMATCH = re.compile(r"^AUTH FAIL \d+ itype=0x2 ")
_PARTIAL = re.compile(r"^AUTH \d+ empty ")
_DOWN = re.compile(r"^AUTH \d+ REAL DOWN ")
# Named fbcli events. The fingerprint waveforms live in fp5_qtee_haptic
# (one 20 ms click, or two 30 ms pulses). These stay so a drop-in can
# still name an event; an empty name plays nothing through fbcli.
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
# Warm mode keeps one "serve" session loaded while the phone is locked,
# so a wake only has to arm the sensor. Opt in until it is proven on more
# phones: FP5_QTEE_WARM=1.
WARM = os.environ.get("FP5_QTEE_WARM", "0") == "1"
# Screen-off arming. Default off: the focaltech IRQ (gpio34) is not a
# wakeup source, so a dark press cannot resume s2idle. The kernel change
# that would make it one is enable_irq_wake() on that IRQ. This tree does
# not patch the kernel. With the flag on, a hit may ask the panel to come
# on; a miss never does, and only a hit can unlock.
DARK_ARM = os.environ.get("FP5_QTEE_DARK_ARM", "0") == "1"
# After the panel goes off, ask LockedHint once a second this many times,
# so the session can load while the screen is dark. Then stay quiet.
OFF_LOCK_CHECKS = 10
WARM_RETRY_S = 5.0
# With logind's LockedHint signal, an unlocked phone with the panel on is
# asked only this often (a safety net); the signal makes a lock seen at
# once. Without the signal, a dark phone is asked this often until the
# session is loaded, so a late lock still preloads before the next wake.
UNLOCKED_LOCK_POLL_S = 15.0
DARK_LOCK_POLL_S = 10.0
_LOCK_SIGNAL = re.compile(r"LockedHint|\.Session\.(Lock|Unlock) ")
MISS_TEXT = {
    "nomatch": "Not recognized. Try again, or use your PIN.",
    "partial": "Finger not read. Cover the whole sensor and hold still.",
}


def should_arm(panel: bool, locked: bool, dark_arm: bool) -> bool:
    """Send auth while the panel is on, or in the dark when opted in."""
    if not locked:
        return False
    return panel or dark_arm


def should_wake_panel(hit: bool, panel_on_now: bool, dark_arm: bool) -> bool:
    """A miss never wakes. A hit wakes only for an opted-in dark arm."""
    return bool(hit and dark_arm and not panel_on_now)


def request_panel_on(backlight: Path = BACKLIGHT, drm: Path = DSI_DPMS) -> None:
    """Best-effort panel on. This does not wake a suspended CPU.

    bl_power 0 is FB_BLANK_UNBLANK. dpms "on" is the DRM power mode. Until
    enable_irq_wake() is set on the focaltech IRQ (gpio34), a press while
    the CPU is in s2idle never reaches this.
    """
    try:
        backlight.write_text("0")
    except OSError:
        pass
    if not drm.is_dir():
        return
    for path in sorted(drm.glob("card*-DSI-*/dpms")):
        try:
            path.write_text("on\n")
        except OSError:
            pass


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


def is_down(line: str) -> bool:
    return _DOWN.match(line.strip()) is not None


def wake_press(down_at: float, arm_at: float | None,
               grace_s: float = WAKE_GRACE_S) -> bool:
    """True when finger-down is within grace_s of this listen's arm.

    arm_at is the watcher clock when the AUTH arm line was read. None
    means this press is not the wake press: no arm yet, or not the first
    listen after the panel came on.
    """
    if arm_at is None or grace_s <= 0:
        return False
    return down_at <= arm_at + grace_s


def consume_wake(claimed: bool, at: float, arm_at: float | None,
                 grace_s: float = WAKE_GRACE_S) -> tuple[bool, bool]:
    """(ignore this no-match, claimed after).

    Only the first no-match of the first listen after panel-on, and only
    when that finger-down is within grace_s of that listen's arm. The
    caller passes arm_at for that listen and None after it. A later
    press is a real try, even when it is quick.
    """
    if claimed or not wake_press(at, arm_at, grace_s):
        return False, claimed
    return True, True


def await_panel(panel: Callable[[], bool], wait_s: float,
                wanted: Callable[[], bool],
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> bool:
    """True when the panel is on, or comes on within wait_s.

    Stops early when wanted() is false. wait_s <= 0 checks once.
    """
    if panel():
        return True
    if wait_s <= 0:
        return False
    deadline = clock() + wait_s
    while True:
        if not wanted():
            return False
        now = clock()
        if now >= deadline:
            return panel()
        sleep(min(0.05, deadline - now))
        if panel():
            return True


def lock_signal(line: str) -> bool:
    """A gdbus monitor line saying a session's lock state changed."""
    return _LOCK_SIGNAL.search(line) is not None


class LockSignals:
    """logind lock changes, from one idle `gdbus monitor` process.

    Only a hint to ask LockedHint now; the lock state itself is always read
    with loginctl. If gdbus is missing or exits, alive is False and the
    loop falls back to polling.
    """

    def __init__(self, argv: list[str] | None = None) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.alive = False
        self.proc: subprocess.Popen[str] | None = None
        try:
            self.proc = subprocess.Popen(
                argv or ["gdbus", "monitor", "--system",
                         "--dest", "org.freedesktop.login1"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError:
            return
        self.alive = True
        assert self.proc.stdout is not None
        threading.Thread(
            target=pump_lines, args=(self.proc.stdout, self.lines), daemon=True
        ).start()

    def poll(self) -> bool:
        """True if a lock change was signalled since the last poll."""
        seen = False
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                self.alive = False
                break
            if lock_signal(line):
                seen = True
        return seen

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        if self.proc is not None:
            if self.proc.returncode is None:
                self.proc.wait(timeout=2)
            if self.proc.stdout is not None:
                try:
                    self.proc.stdout.close()
                except (OSError, ValueError):
                    pass
        self.alive = False


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
        marker = default_pin_marker()
        self.policy = UnlockPolicy(marker=marker, state=lockout_state_for(marker))
        self.haptic = Haptic()
        # main() copies DARK_ARM onto this. Tests leave it off.
        self.dark_arm = False
        self._policy_sid: str | None = None
        self._told_reason: str | None = None
        # The first listen after off -> on may ignore one no-match near
        # its arm line. Closed when that listen finishes after arming, so
        # a later press counts even when its own down is quick.
        self._wake_open = False
        self._arm_at: float | None = None
        self._wake_claimed = False
        # None until the first sample, so a panel that is already on at
        # start is not treated as a wake.
        self._panel_was_on: bool | None = None
        self._clock: Callable[[], float] = time.monotonic

    def _note_panel(self, on: bool, saw_arm: bool) -> bool:
        """Record panel state. A rise before this listen arms opens the
        wake window. A rise after arm does not: that press is the one
        the session was already waiting for, and it is scored.
        """
        was = self._panel_was_on
        rose = bool(on and was is False)
        if rose and not saw_arm:
            self._wake_open = True
            self._arm_at = None
            self._wake_claimed = False
        self._panel_was_on = on
        return rose

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

    def tell_hit(self) -> None:
        """The Android confirm click. Nothing extra is added for lockout."""
        self.haptic.play("success")

    def tell_miss(self, kind: str) -> None:
        """A finger was read and did not unlock. Say so right away.

        A partial press uses the same double pulse as a mismatch. The
        silent profile plays nothing. Lockout does not add a third pattern;
        the miss that reached 5 still plays this one.
        """
        self.note(f"miss {kind}")
        self.haptic.play("miss")
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
        # Stay armed while locked with the panel off. The wake press is
        # then a new edge on a sensor calibrated with no finger.
        # Unknown (None) is not locked: stop rather than guess.
        return lock_check.get() is True

    def wake_panel(self) -> None:
        """Ask the panel to come on. Never called for a miss."""
        request_panel_on()

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
                str(UNLOCK_HOLD_S),
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
            panel=panel_on,
        )

    def follow(self, sid: str, proc: subprocess.Popen[str],
               wanted: Callable[[], bool], poll_s: float = PANEL_POLL_S,
               panel: Callable[[], bool] | None = None,
               wake_wait_s: float = PANEL_WAKE_WAIT_S) -> str:
        """Read the session's lines until a hit, its exit, or not wanted.

        The session prints nothing while it waits for a finger, so the
        wait is on a queue with a timeout. Each timeout re-checks the
        lock. Unlocking with the PIN stops the session. The panel going
        off does not: the wake press has to land on an armed sensor.
        panel is None in tests, which score as if the screen were on.
        A hit unlocks only when that panel is on or comes on within
        wake_wait_s. A miss that leaves it off is not a strike.
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
        down_at: float | None = None
        saw_arm = False
        with path.open("w", encoding="utf-8") as log:
            while True:
                try:
                    line = lines.get(timeout=poll_s)
                except queue.Empty:
                    if panel is not None:
                        self._note_panel(panel(), saw_arm)
                    if not wanted():
                        outcome = "stop"
                        break
                    continue
                if line is None:
                    break
                if panel is not None:
                    self._note_panel(panel(), saw_arm)
                log.write(line)
                log.flush()
                if armed(line):
                    saw_arm = True
                    if self._wake_open and self._arm_at is None:
                        self._arm_at = self._clock()
                        down_at = None
                if is_down(line):
                    down_at = self._clock()
                fid = hit_fid(line)
                if fid is not None:
                    at = self._clock() if down_at is None else down_at
                    if self._wake_open and wake_press(at, self._arm_at):
                        self._wake_claimed = True
                    # A hit in the dark can turn the panel on. A miss never
                    # reaches this, so it cannot wake the screen.
                    if self.dark_arm and should_wake_panel(True, panel_on(), True):
                        self.wake_panel()
                    # PIN unlock, or the seat is no longer one we may listen on.
                    if not wanted():
                        outcome = "stop"
                        break
                    if panel is not None and not await_panel(
                        panel, wake_wait_s, wanted
                    ):
                        self.note("hit ignored: panel stayed off")
                        outcome = "stop"
                        break
                    if self.policy.why_not() is not None:
                        outcome = "stop"
                        break
                    outcome = "hit"
                    self.policy.on_hit()
                    self.tell_hit()
                    self.unlock(sid, fid)
                    break
                kind = miss_kind(line)
                if kind is not None:
                    at = self._clock() if down_at is None else down_at
                    down_at = None
                    if panel is not None and not await_panel(
                        panel, wake_wait_s, wanted
                    ):
                        # Touched while the screen stayed off. Not a try.
                        self.note(f"miss {kind} ignored: panel off")
                        kind = None
                    else:
                        arm_at = self._arm_at if self._wake_open else None
                        ignore, self._wake_claimed = consume_wake(
                            self._wake_claimed, at, arm_at
                        )
                        if ignore:
                            # The power-key press that woke the panel. Not a try.
                            self.note(f"miss {kind} ignored: wake press")
                            kind = None
                if kind is not None:
                    # A screen-on press. The strike still counts if the
                    # seat unlocked while it was scored; the notice does not.
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
        if saw_arm and self._wake_open:
            self._wake_open = False
        self._arm_at = None
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
        signals = LockSignals()
        if not signals.alive:
            self.note("no logind signals; polling LockedHint")
        loop = WarmLoop(self, signals=signals, dark_arm=self.dark_arm)
        try:
            while True:
                loop.step()
                time.sleep(PANEL_POLL_S)
        finally:
            loop.stop()
            signals.close()

    def run(self) -> None:
        """Listen while the seat is locked, including while the panel is off.

        The sensor is calibrated before the wake press. loginctl is asked
        every second while locked or while the panel is on, and slowly
        while unlocked with the panel off. A panel edge asks at once.
        """
        self.note("watcher start")
        last_lock_ask = 0.0
        believed_locked = False
        while True:
            on = panel_on()
            was = self._panel_was_on
            rose = self._note_panel(on, False)
            if rose or (was is True and not on):
                last_lock_ask = 0.0
            now = time.monotonic()
            interval = (
                LOCK_POLL_S if (believed_locked or on) else UNLOCKED_LOCK_POLL_S
            )
            if last_lock_ask and now - last_lock_ask < interval:
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
                believed_locked = False
                self._told_reason = None
                continue
            believed_locked = True
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
    - Panel on and locked: send auth. The panel and LockedHint are read
      again immediately before every auth, so a wake never arms on the
      lock state from before the panel went off. After each result the
      session says "SERVE idle" and can be armed again.
    - Panel off: send cancel, unless dark_arm is set. Unlocked: quit the
      session, which frees the sensor for the Finger app. The next lock (a
      logind LockedHint signal, or a poll) starts a new session at once,
      while unlocked screen-on or dark, so it is warm again before the next
      wake. LockedHint unknown (loginctl timed out, failed, or printed
      nothing): cancel, keep the session, do not arm.
    A hit unlocks only if it belongs to the auth we sent in this panel-on
    period and was not cancelled, the panel is on (or dark_arm is set),
    logind says locked right now, and the policy allows it. A scored
    non-match is a strike whatever the panel did meanwhile. It never wakes
    the screen. Feedback in the dark is played only when dark_arm is set.
    """

    def __init__(self, watcher: "UnlockWatcher",
                 panel: Callable[[], bool] = panel_on,
                 is_locked: Callable[[str], bool | None] = locked,
                 clock: Callable[[], float] = time.monotonic,
                 signals: "LockSignals | None" = None,
                 dark_arm: bool = False,
                 wake: Callable[[], None] | None = None) -> None:
        self.w = watcher
        self.signals = signals
        self.panel = panel
        self.is_locked = is_locked
        self.clock = clock
        self.dark_arm = dark_arm
        self.wake = wake if wake is not None else watcher.wake_panel
        self.warm: WarmSession | None = None
        self.armed = False
        # True from sending auth until that attempt is cancelled, the
        # panel is seen off, or the session reports its result.
        self.attempt_live = False
        self.believed_locked = False
        self.prev_panel: bool | None = None
        self.off_checks = 0
        self.next_lock_ask = 0.0
        self.retry_at = 0.0
        self.sid: str | None = None
        self._step_no = 0
        self._asked_step = -1
        self.dark_ask_at = 0.0
        # First listen after off -> on. Same rule as UnlockWatcher.
        self.wake_open = False
        self.arm_at: float | None = None
        self.saw_arm = False
        self.wake_claimed = False
        self.down_at: float | None = None

    def _signalled(self) -> bool:
        return self.signals is not None and self.signals.poll()

    def _have_signals(self) -> bool:
        return self.signals is not None and self.signals.alive

    def start(self) -> None:
        if self.warm is not None or self.clock() < self.retry_at:
            return
        warm = self.w.start_warm()
        if warm is None:
            self.retry_at = self.clock() + WARM_RETRY_S
            return
        self.warm = warm
        self.armed = False
        self.attempt_live = False

    def stop(self) -> None:
        if self.warm is not None:
            self.w.note("warm session quit")
            self.warm.close()
        self.warm = None
        self.armed = False
        self.attempt_live = False
        self._close_armed_listen()

    def _close_armed_listen(self) -> None:
        """The first armed listen is over. Later presses are real tries."""
        if not self.saw_arm:
            return
        self.wake_open = False
        self.saw_arm = False
        self.arm_at = None

    def cancel(self) -> None:
        """No hit may unlock from the running attempt any more."""
        self.attempt_live = False
        warm = self.warm
        if self.armed and warm is not None and not warm.cancel_sent:
            warm.send("cancel")
            warm.cancel_sent = True

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
                self.attempt_live = False
                self._close_armed_listen()
                continue
            if line.startswith("SERVE result"):
                # That attempt is over. Nothing after this is from it.
                self.attempt_live = False
                self._close_armed_listen()
                continue
            if line.startswith("SERVE reject"):
                self.w.note(f"warm session: {line.strip()}")
                continue
            if armed(line):
                self.saw_arm = True
                if self.wake_open and self.arm_at is None:
                    self.arm_at = self.clock()
                    self.down_at = None
                continue
            if is_down(line):
                self.down_at = self.clock()
                continue
            fid = hit_fid(line)
            if fid is not None:
                at = self.clock() if self.down_at is None else self.down_at
                self.down_at = None
                if self.wake_open and wake_press(at, self.arm_at):
                    self.wake_claimed = True
                self.on_hit(fid, panel)
                continue
            kind = miss_kind(line)
            if kind is not None:
                at = self.clock() if self.down_at is None else self.down_at
                self.down_at = None
                arm_at = self.arm_at if self.wake_open else None
                ignore, self.wake_claimed = consume_wake(
                    self.wake_claimed, at, arm_at
                )
                if ignore:
                    # The power-key press that woke the panel. Not a try.
                    self.w.note(f"miss {kind} ignored: wake press")
                    continue
                # A real press was scored for an auth we sent. It counts
                # even if the panel went off or we cancelled meanwhile.
                if kind == "nomatch":
                    self.w.policy.on_miss()
                # A miss never wakes the panel. In the dark the double
                # pulse still plays when dark arming is on.
                if self.believed_locked and self.attempt_live and (
                    panel or self.dark_arm
                ):
                    self.w.tell_miss(kind)
        if dead or not warm.alive():
            self.w.note("warm session ended")
            self.stop()
            self.retry_at = self.clock() + WARM_RETRY_S

    def on_hit(self, fid: int, panel: bool) -> None:
        live = self.attempt_live
        self.attempt_live = False
        sid = self.sid
        if not live:
            # After cancel, after panel-off (unless dark arming), or not
            # from an auth we sent for this attempt.
            self.w.note("hit refused: attempt was cancelled or stale")
            return
        if not self.believed_locked or sid is None:
            return
        if not panel and not self.dark_arm:
            return
        if not self.dark_arm and not self.panel():
            return
        if self.w.policy.why_not() is not None:
            return
        if self.is_locked(sid) is not True:
            return
        if should_wake_panel(True, panel, self.dark_arm):
            self.wake()
        self.w.policy.on_hit()
        self.w.tell_hit()
        self.w.unlock(sid, fid)
        self.believed_locked = False

    def ask_lock(self) -> bool | None:
        """LockedHint now: True, False, or None when unknown."""
        self.sid = self.w.session_id()
        state = None if self.sid is None else self.is_locked(self.sid)
        self.w.policy.observe_lock(state)
        if state is None:
            sid_cache = getattr(self.w, "_sid", None)
            if sid_cache is not None:
                sid_cache.forget()
        self._asked_step = self._step_no
        return state

    def _unlocked(self) -> None:
        self.believed_locked = False
        self.stop()
        self.w._told_reason = None

    def step(self) -> None:
        self._step_no += 1
        now = self.clock()
        panel = self.panel()
        signalled = self._signalled()
        if panel and self.prev_panel is False:
            self.wake_open = True
            self.arm_at = None
            self.saw_arm = False
            self.wake_claimed = False
            self.down_at = None
        self.drain(panel)
        if not panel and not self.dark_arm:
            self.cancel()
            if self.prev_panel:
                self.off_checks = OFF_LOCK_CHECKS
                self.next_lock_ask = now
            self.prev_panel = False
            if self.warm is not None:
                return
            due = False
            if signalled:
                # Locked (or unlocked) while dark: look now.
                due = True
            elif self.off_checks and now >= self.next_lock_ask:
                self.off_checks -= 1
                self.next_lock_ask = now + 1.0
                due = True
            elif not self._have_signals() and now >= self.dark_ask_at:
                due = True
            if due:
                self.dark_ask_at = now + DARK_LOCK_POLL_S
                if self.ask_lock() is True:
                    self.believed_locked = True
                    self.off_checks = 0
                    self.start()
            return
        # Panel on, or dark arming. Ask logind on the first step and on
        # off -> on. Staying dark must not ask on every poll.
        if (panel and not self.prev_panel) or self.prev_panel is None:
            self.next_lock_ask = now
        self.prev_panel = panel
        if signalled:
            self.next_lock_ask = now
        if now >= self.next_lock_ask:
            # Locked: every LOCK_POLL_S, as before. Unlocked with logind
            # signals: a slow safety poll only.
            slow = not self.believed_locked and self._have_signals()
            self.next_lock_ask = now + (UNLOCKED_LOCK_POLL_S if slow else LOCK_POLL_S)
            state = self.ask_lock()
            if state is None:
                self.believed_locked = False
                self.cancel()
                return
            self.believed_locked = state
            if not state:
                self._unlocked()
                return
        if not self.believed_locked:
            return
        reason = self.w.policy.why_not()
        if reason is not None:
            self.cancel()
            if reason != self.w._told_reason:
                self.w._told_reason = reason
                self.w.note(f"not listening: {reason}")
                self.w.notify(POLICY_TEXT[reason])
            return
        self.w._told_reason = None
        self.start()
        warm = self.warm
        if warm is None or not warm.idle or self.armed:
            return
        # Immediately before arming: the panel (unless dark arming), and
        # LockedHint unless it was read in this very step.
        if not should_arm(self.panel(), True, self.dark_arm):
            return
        if self._asked_step != self._step_no:
            self.next_lock_ask = now + LOCK_POLL_S
            state = self.ask_lock()
            if state is not True:
                self.believed_locked = False
                if state is False:
                    self._unlocked()
                return
        if self.w.policy.why_not() is not None:
            return
        warm.send("auth")
        warm.idle = False
        self.armed = True
        self.attempt_live = True
        self.down_at = None


def main() -> None:
    watcher = UnlockWatcher()
    watcher.dark_arm = DARK_ARM
    if WARM:
        watcher.run_warm()
    else:
        watcher.run()


if __name__ == "__main__":
    main()
