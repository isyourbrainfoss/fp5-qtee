"""Android fingerprint haptics for the FP5 aw86927.

Success is one 60 ms pulse. A miss is two 60 ms pulses whose starts are
160 ms apart, at the same amplitude. A too-fast tap is three 35 ms ticks
85 ms apart. Running out of tries (lockout) is three long 150 ms pulses,
so it cannot be taken for a miss. The silent
feedbackd profile plays nothing, and so does an unreadable profile.

feedbackd plays nothing for an event its active theme lacks (fbcli says
"No feedback found"). A user theme such as fp5-strong that does not carry
the fp5-qtee-* events made every fingerprint unlock silent, so that answer
falls back to the evdev rumble.

The aw86927 on this phone is an input force-feedback device, not an LED.
feedbackd plays a named event from the theme beside this file when that
daemon has the device open. Otherwise the same timings are uploaded as
FF_RUMBLE. ff-memless stops the effect after replay.length. The driver's
gain register is ``strong_magnitude * 0x80 / 0xffff``, so magnitude
0xffff is the Android fingerprint gain 0x80 only when the device gain
is also 0xffff. feedbackd sets that gain to 0xc000 (75%) when it opens
the motor, which would store 0x60. The player raises it to 0xffff for
the pulse and puts 0xc000 back. An LED ``duration`` / ``activate``
device is still used when one exists.
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

# FP5 LRA (aw86927): 20/30 ms pulses were too short to feel.
SUCCESS_MS = 60
MISS_ON_MS = 60
MISS_OFF_MS = 100
# Too fast: three short ticks, so it is not confused with the single
# success click or the double miss pulse.
TOOFAST_ON_MS = 35
TOOFAST_OFF_MS = 50
LOCKOUT_ON_MS = 150
LOCKOUT_OFF_MS = 100
AMPLITUDE = 1.0
GAIN = 0x80
# aw86927_play_sine writes (magnitude * 0x80 / 0xffff) into PLAYCFG2.
RUMBLE_MAGNITUDE = 0xFFFF
FF_GAIN = 0x60
# ff-memless scales strong_magnitude by this device gain. feedbackd
# writes 0xc000 at open. 0xffff lets 0xffff through, which is register 0x80.
FEEDBACKD_GAIN = 0xC000
FULL_GAIN = 0xFFFF
# Gain put back after a fingerprint pulse. feedbackd's own value by
# default; a device whose motor is weak at 75% (BengalOS FP5, where a
# hook raises the device gain to 0xffff) sets FP5_QTEE_FF_RESTORE_GAIN.
try:
    RESTORE_GAIN = max(0, min(0xFFFF, int(os.environ.get("FP5_QTEE_FF_RESTORE_GAIN", ""), 0)))
except ValueError:
    RESTORE_GAIN = FEEDBACKD_GAIN
SUCCESS_EVENT = "fp5-qtee-match"
MISS_EVENT = "fp5-qtee-miss"
TOOFAST_EVENT = "fp5-qtee-toofast"
LOCKOUT_EVENT = "fp5-qtee-lockout"
# fbcli's answer when the active theme has no feedback for the event.
_NO_FEEDBACK = "No feedback found"
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

    None for anything else: that adds no vibration.
    """
    if kind == "success":
        return [(AMPLITUDE, SUCCESS_MS)]
    if kind == "miss":
        return [
            (AMPLITUDE, MISS_ON_MS),
            (0.0, MISS_OFF_MS),
            (AMPLITUDE, MISS_ON_MS),
        ]
    if kind == "lockout":
        return [
            (AMPLITUDE, LOCKOUT_ON_MS),
            (0.0, LOCKOUT_OFF_MS),
            (AMPLITUDE, LOCKOUT_ON_MS),
            (0.0, LOCKOUT_OFF_MS),
            (AMPLITUDE, LOCKOUT_ON_MS),
        ]
    if kind == "toofast":
        return [
            (AMPLITUDE, TOOFAST_ON_MS),
            (0.0, TOOFAST_OFF_MS),
            (AMPLITUDE, TOOFAST_ON_MS),
            (0.0, TOOFAST_OFF_MS),
            (AMPLITUDE, TOOFAST_ON_MS),
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
    event = {"success": SUCCESS_EVENT, "miss": MISS_EVENT,
             "toofast": TOOFAST_EVENT, "lockout": LOCKOUT_EVENT}.get(kind)
    if event is None:
        return None
    return ["fbcli", "-A", APP_ID, "-E", event]


def fbcli_missed(output: str) -> bool:
    """fbcli printed that the active theme has no feedback for the event."""
    return _NO_FEEDBACK in output


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


def ff_gain_event(value: int) -> bytes:
    """EV_FF / FF_GAIN. ff-memless applies it to every rumble on the device."""
    return struct.pack("<16xHHi", EV_FF, FF_GAIN, value)


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
    """Session bus. busctl without --user stays on the system bus."""
    try:
        proc = subprocess.run(
            [
                "busctl",
                "--user",
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
        gain: Callable[[int], None] | None = None,
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
        self._gain = gain
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
            played = [True]
            hold = max(0.5, sum(ms for _a, ms in waveform(kind) or []) / 1000.0 + 0.15)

            def run() -> None:
                played[0] = self._run(args, hold) is not False

            self._bracket_gain(run)
            if played[0]:
                return "fbcli"
            # The theme lacks the event: feedbackd played nothing.
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
            rumble = self._rumble

            def injected() -> None:
                for op, value in steps:
                    if op == "sleep":
                        self._sleeper(value / 1000.0)
                    else:
                        rumble(value)

            self._bracket_gain(injected)
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
            ok = True

            def uploaded() -> None:
                nonlocal ok
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
                        ok = False
                        return
                if last_id is not None:
                    # The kernel has already stopped the motor. Erase frees
                    # the slot before the gain goes back, so ff-memless does
                    # not replay this pulse at 75%.
                    self._sleeper(last_ms / 1000.0)
                    self._erase(fd, last_id)

            self._bracket_gain(uploaded, fd)
            return ok
        except OSError:
            return False
        finally:
            os.close(fd)

    def _bracket_gain(self, body: Callable[[], None], fd: int | None = None) -> None:
        """Device gain 0xffff for body, then RESTORE_GAIN (feedbackd's 0xc000).

        An injected player records the values and does not open a device.
        """
        opened = fd
        own = False
        if opened is None and self._gain is None and self._runner is None and self._rumble is None:
            opened = self._open_ff()
            own = opened is not None
        self._write_gain(opened, FULL_GAIN)
        try:
            body()
        finally:
            self._write_gain(opened, RESTORE_GAIN)
            if own and opened is not None:
                os.close(opened)

    def _open_ff(self) -> int | None:
        device = self._ff_dev if self._ff_dev is not None else find_ff(
            self._dev_root, self._sys_root
        )
        if device is None:
            return None
        try:
            return os.open(device, os.O_RDWR)
        except OSError:
            return None

    def _write_gain(self, fd: int | None, value: int) -> None:
        if self._gain is not None:
            self._gain(value)
            return
        if fd is None:
            return
        try:
            os.write(fd, ff_gain_event(value))
        except OSError:
            pass

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

    def _run(self, args: list[str], hold_s: float = 0.5) -> bool | None:
        """False when feedbackd had no feedback for the event."""
        if self._runner is not None:
            return self._runner(args)
        # fbcli ends the event when stdin hits EOF. A service has no tty,
        # so leave the pipe open until the pattern has finished.
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError:
            return None
        out = ""
        try:
            self._sleeper(hold_s)
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                out, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    out, _ = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    out = ""
        return not fbcli_missed(out or "")
