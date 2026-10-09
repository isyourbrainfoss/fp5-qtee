"""Android fingerprint haptics for the FP5 aw86927.

Success is one 20 ms pulse. A miss is two 30 ms pulses whose starts are
130 ms apart, at the same amplitude. Lockout adds nothing. The silent
feedbackd profile plays nothing, and so does an unreadable profile.

fbcli can only name an event. The timings are written to an LED class
device (duration, then activate) when one is present. The theme fragment
beside this file is the fallback, and it does nothing until it is merged
into the installed feedbackd theme.
"""

from __future__ import annotations

import os
import re
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
SUCCESS_EVENT = "fp5-qtee-match"
MISS_EVENT = "fp5-qtee-miss"
APP_ID = "org.fp5.qtee"
LED_MARKS = ("aw869", "haptic", "vibrator")

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
    ) -> None:
        self._profile = profile
        self._leds = leds if leds is not None else Path("/sys/class/leds")
        self._writer = writer
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._runner = runner
        self._read_profile = read_profile
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

    def play_sync(self, kind: str) -> str:
        """'none', 'sysfs', or 'fbcli'."""
        if not self.enabled or waveform(kind) is None or not plays(self.profile()):
            return "none"
        led = find_led(self._leds)
        if led is not None:
            self._play_sysfs(led, kind)
            return "sysfs"
        args = fbcli_args(kind)
        if args is None:
            return "none"
        self._run(args)
        return "fbcli"

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

    def _run(self, args: list[str]) -> None:
        if self._runner is not None:
            self._runner(args)
            return
        try:
            subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
