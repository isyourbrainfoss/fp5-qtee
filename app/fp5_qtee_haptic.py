"""Android fingerprint haptics for the FP5 aw86927.

Success is one 20 ms pulse. A miss is two 30 ms pulses whose starts are
130 ms apart, at the same amplitude. Lockout adds nothing. The silent
feedbackd profile plays nothing, and so does an unreadable profile.

The aw86927 on this phone is an input force-feedback device, not an LED.
feedbackd plays a named event from the theme beside this file when that
daemon has the device open. Otherwise the same timings are uploaded as
FF_RUMBLE. ff-memless stops the effect after replay.length. The driver's
gain register is ``strong_magnitude * 0x80 / 0xffff``, so magnitude
0xffff is the Android fingerprint gain 0x80. An LED ``duration`` /
``activate`` device is still used when one exists.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

SUCCESS_MS = 20
MISS_ON_MS = 30
MISS_OFF_MS = 100
AMPLITUDE = 1.0
GAIN = 0x80
# aw86927_play_sine writes (magnitude * 0x80 / 0xffff) into PLAYCFG2.
RUMBLE_MAGNITUDE = 0xFFFF
SUCCESS_EVENT = "fp5-qtee-match"
MISS_EVENT = "fp5-qtee-miss"
APP_ID = "org.fp5.qtee"
LED_MARKS = ("aw869", "haptic", "vibrator")
FF_MARKS = ("aw869", "haptic")
FF_RUMBLE = 0x50
EV_FF = 0x15
EVIOCSFF = 0x40304580
EVIOCRMFF = 0x40044581

_ACCEPT = re.compile(r"^rem=(\d+) sample=(\d+)")
_PROFILE = re.compile(r'"([^"]*)"')


def waveform(kind: str) -> list[tuple[float, int]] | None:
    """Pulses as (amplitude, milliseconds). A gap is amplitude 0.

    None for lockout and anything else: those add no vibration.
    """
    if kind == "success":
        return [(AMPLITUDE, SUCCESS_MS)]
    if kind == "miss":
        return [
            (AMPLITUDE, MISS_ON_MS),
            (0.0, MISS_OFF_MS),
            (AMPLITUDE, MISS_ON_MS),
        ]
    return None


def plays(profile: str | None) -> bool:
    """Full and quiet vibrate. Silent, and a profile we could not read, do not."""
    if profile is None:
        return False
    text = profile.strip().lower()
    if not text or text == "silent":
        return False
    return True


def enroll_accept(line: str) -> bool:
    """A counted enroll sample. An empty PRESS REJECT is not one."""
    return _ACCEPT.match(line.strip()) is not None


def parse_profile(text: str) -> str:
    """busctl prints `s "quiet"`. Anything else is unreadable."""
    match = _PROFILE.search(text)
    return match.group(1) if match else ""


def find_led(root: Path) -> Path | None:
    """An LED whose name looks like the aw86927 and has duration + activate."""
    if not root.is_dir():
        return None
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        name = path.name.lower()
        if not any(mark in name for mark in LED_MARKS):
            continue
        if (path / "duration").is_file() and (path / "activate").is_file():
            return path
    return None


def sysfs_steps(kind: str) -> list[tuple[str, int]] | None:
    """Writes and sleeps for one waveform. Sleeps are milliseconds.

    Gain is set once, to the same value for both miss pulses. A gap is
    only a sleep, so the second pulse starts MISS_ON_MS + MISS_OFF_MS
    after the first.
    """
    pulses = waveform(kind)
    if pulses is None:
        return None
    steps: list[tuple[str, int]] = [("gain", GAIN)]
    for amp, ms in pulses:
        if amp <= 0:
            steps.append(("sleep", ms))
            continue
        steps.append(("duration", ms))
        steps.append(("activate", 1))
        steps.append(("sleep", ms))
        steps.append(("activate", 0))
    return steps


def fbcli_args(kind: str) -> list[str] | None:
    event = {"success": SUCCESS_EVENT, "miss": MISS_EVENT}.get(kind)
    if event is None:
        return None
    return ["fbcli", "-A", APP_ID, "-E", event]


def ff_steps(kind: str) -> list[tuple[str, int]] | None:
    """Rumble uploads and the sleep between their starts.

    The kernel ends each rumble after its own length, so the sleep is the
    start-to-start gap (the pulse plus the off time), not the off time alone.
    """
    pulses = waveform(kind)
    if pulses is None:
        return None
    steps: list[tuple[str, int]] = []
    gap = 0
    previous = 0
    started = False
    for amp, ms in pulses:
        if amp <= 0:
            gap += ms
            continue
        if started:
            steps.append(("sleep", previous + gap))
        steps.append(("rumble", ms))
        previous = ms
        gap = 0
        started = True
    return steps


def rumble_effect(duration_ms: int, magnitude: int = RUMBLE_MAGNITUDE) -> bytes:
    """One FF_RUMBLE effect. id -1 asks the kernel to allocate a slot.

    Layout matches ``struct ff_effect`` on LP64: replay.length at byte 10,
    strong_magnitude at byte 16, size 48.
    """
    buf = bytearray(48)
    struct.pack_into("<HhHHHHH", buf, 0, FF_RUMBLE, -1, 0, 0, 0, duration_ms, 0)
    struct.pack_into("<HH", buf, 16, magnitude & 0xFFFF, 0)
    return bytes(buf)


def ff_play_event(effect_id: int) -> bytes:
    """EV_FF input_event, time left zero. 24 bytes on LP64."""
    return struct.pack("<16xHHi", EV_FF, effect_id, 1)


def find_ff(dev_root: Path, sys_root: Path) -> Path | None:
    """The event node whose input name looks like the aw86927."""
    if not dev_root.is_dir():
        return None
    for node in sorted(dev_root.glob("event*")):
        try:
            name = (sys_root / node.name / "device" / "name").read_text(encoding="utf-8")
        except OSError:
            continue
        lowered = name.strip().lower()
        if any(mark in lowered for mark in FF_MARKS):
            return node
    return None


def feedbackd_has_haptic() -> bool:
    """True when feedbackd exported the haptic interface for this session.

    That interface exists only after it has opened a rumble device. A
    missing bus, or a daemon that never opened the aw86927, is false.
    """
    try:
        proc = subprocess.run(
            [
                "busctl",
                "--user",
                "introspect",
                "org.sigxcpu.Feedback",
                "/org/sigxcpu/Feedback",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return "org.sigxcpu.Feedback.Haptic" in proc.stdout


def read_bus_profile() -> str:
    try:
        proc = subprocess.run(
            [
                "busctl",
                "get-property",
                "org.sigxcpu.Feedback",
                "/org/sigxcpu/Feedback",
                "org.sigxcpu.Feedback",
                "Profile",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return parse_profile(proc.stdout)


class Haptic:
    """Plays one waveform. Tests pass profile, a writer, and a sleeper."""

    def __init__(
        self,
        profile: str | None = None,
        leds: Path | None = None,
        writer: Callable[[Path, str], None] | None = None,
        sleeper: Callable[[float], None] | None = None,
        runner: Callable[[list[str]], None] | None = None,
        read_profile: Callable[[], str] | None = None,
        enabled: bool | None = None,
        feedback: bool | None = None,
        rumble: Callable[[int], None] | None = None,
        ff_dev: Path | None = None,
        sys_root: Path | None = None,
        dev_root: Path | None = None,
        has_haptic: Callable[[], bool] | None = None,
    ) -> None:
        self._profile = profile
        self._leds = leds if leds is not None else Path("/sys/class/leds")
        self._writer = writer
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._runner = runner
        self._read_profile = read_profile
        # None probes feedbackd. False skips it and uses the evdev device.
        self._feedback = feedback
        self._rumble = rumble
        self._ff_dev = ff_dev
        self._sys_root = sys_root if sys_root is not None else Path("/sys/class/input")
        self._dev_root = dev_root if dev_root is not None else Path("/dev/input")
        self._has_haptic = has_haptic if has_haptic is not None else feedbackd_has_haptic
        self._haptic_known: bool | None = None
        if enabled is None:
            enabled = os.environ.get("FP5_QTEE_HAPTIC", "1") != "0"
        self.enabled = enabled

    def profile(self) -> str:
        if self._profile is not None:
            return self._profile
        env = os.environ.get("FP5_QTEE_HAPTIC_PROFILE")
        if env is not None:
            return env
        if self._read_profile is not None:
            return self._read_profile()
        return read_bus_profile()

    def play(self, kind: str, *, background: bool = True) -> None:
        """Return at once. The GTK app and the watcher must not wait on it."""
        if background:
            threading.Thread(
                target=self.play_sync, args=(kind,), daemon=True
            ).start()
            return
        self.play_sync(kind)

    def _want_feedback(self) -> bool:
        if self._feedback is False:
            return False
        if self._runner is not None or self._feedback is True:
            return True
        if self._haptic_known is None:
            self._haptic_known = self._has_haptic()
        return self._haptic_known

    def play_sync(self, kind: str) -> str:
        """'none', 'sysfs', 'fbcli', or 'evdev'."""
        if not self.enabled or waveform(kind) is None or not plays(self.profile()):
            return "none"
        led = find_led(self._leds)
        if led is not None:
            self._play_sysfs(led, kind)
            return "sysfs"
        if self._want_feedback():
            args = fbcli_args(kind)
            if args is None:
                return "none"
            self._run(args)
            return "fbcli"
        if self._play_ff(kind):
            return "evdev"
        return "none"

    def _play_sysfs(self, led: Path, kind: str) -> None:
        for op, value in sysfs_steps(kind) or []:
            if op == "gain":
                if (led / "gain").is_file():
                    self._write(led / "gain", str(value))
                continue
            if op == "sleep":
                self._sleeper(value / 1000.0)
                continue
            self._write(led / op, str(value))

    def _write(self, path: Path, text: str) -> None:
        if self._writer is not None:
            self._writer(path, text)
            return
        try:
            path.write_text(text)
        except OSError:
            pass

    def _play_ff(self, kind: str) -> bool:
        steps = ff_steps(kind)
        if not steps:
            return False
        if self._rumble is not None:
            for op, value in steps:
                if op == "sleep":
                    self._sleeper(value / 1000.0)
                else:
                    self._rumble(value)
            return True
        device = self._ff_dev if self._ff_dev is not None else find_ff(
            self._dev_root, self._sys_root
        )
        if device is None:
            return False
        try:
            fd = os.open(device, os.O_RDWR)
        except OSError:
            return False
        try:
            last_id: int | None = None
            last_ms = 0
            for op, value in steps:
                if op == "sleep":
                    self._sleeper(value / 1000.0)
                    if last_id is not None:
                        self._erase(fd, last_id)
                        last_id = None
                    continue
                last_id = self._upload_play(fd, value)
                last_ms = value
                if last_id is None:
                    return False
            if last_id is not None:
                # The kernel has already stopped the motor. Erase frees the slot.
                self._sleeper(last_ms / 1000.0)
                self._erase(fd, last_id)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def _upload_play(self, fd: int, duration_ms: int) -> int | None:
        buf = bytearray(rumble_effect(duration_ms))
        try:
            fcntl.ioctl(fd, EVIOCSFF, buf)
        except OSError:
            return None
        effect_id = struct.unpack_from("<h", buf, 2)[0]
        os.write(fd, ff_play_event(effect_id))
        return effect_id

    def _erase(self, fd: int, effect_id: int) -> None:
        try:
            fcntl.ioctl(fd, EVIOCRMFF, struct.pack("i", effect_id))
        except OSError:
            pass

    def _run(self, args: list[str]) -> None:
        if self._runner is not None:
            self._runner(args)
            return
        # fbcli ends the event when stdin hits EOF. A service has no tty,
        # so leave the pipe open until the double pulse has finished.
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            self._sleeper(0.5)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
