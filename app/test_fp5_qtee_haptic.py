#!/usr/bin/env python3
"""Android haptic timings. No feedbackd and no real vibrator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fp5_qtee_haptic
from fp5_qtee_haptic import (
    AMPLITUDE,
    GAIN,
    MISS_OFF_MS,
    MISS_ON_MS,
    SUCCESS_MS,
    Haptic,
    enroll_accept,
    fbcli_args,
    find_led,
    parse_profile,
    plays,
    sysfs_steps,
    waveform,
)


class WaveTest(unittest.TestCase):
    def test_success_is_one_click(self) -> None:
        self.assertEqual(waveform("success"), [(AMPLITUDE, SUCCESS_MS)])
        self.assertEqual(SUCCESS_MS, 20)

    def test_miss_is_two_pulses_130ms_apart(self) -> None:
        pulses = waveform("miss")
        self.assertEqual(
            pulses,
            [(AMPLITUDE, MISS_ON_MS), (0.0, MISS_OFF_MS), (AMPLITUDE, MISS_ON_MS)],
        )
        assert pulses is not None
        self.assertEqual(pulses[0][1] + pulses[1][1], 130)
        self.assertEqual(pulses[0][0], pulses[2][0])

    def test_lockout_adds_nothing(self) -> None:
        self.assertIsNone(waveform("lockout"))
        self.assertIsNone(waveform("lockout-permanent"))
        self.assertIsNone(waveform(""))
        self.assertIsNone(sysfs_steps("lockout"))
        self.assertIsNone(fbcli_args("lockout"))

    def test_profile(self) -> None:
        self.assertFalse(plays(None))
        self.assertFalse(plays(""))
        self.assertFalse(plays("silent"))
        self.assertFalse(plays(" Silent "))
        self.assertTrue(plays("full"))
        self.assertTrue(plays("quiet"))

    def test_enroll_accept_is_a_counted_sample(self) -> None:
        self.assertTrue(enroll_accept("rem=19 sample=1"))
        self.assertTrue(enroll_accept("rem=0 sample=19\n"))
        self.assertFalse(enroll_accept("PRESS REJECT avgv=0,966,600"))
        self.assertFalse(enroll_accept("skip leftover itype=0x212 esd=0"))
        self.assertFalse(enroll_accept("AUTH FAIL 1 itype=0x2 esd=0 avgv=291 fid=0 rc=-11"))

    def test_busctl_profile_line(self) -> None:
        self.assertEqual(parse_profile('s "quiet"\n'), "quiet")
        self.assertEqual(parse_profile("no quotes"), "")


class ThemeTest(unittest.TestCase):
    def test_fragment_matches_the_waveform(self) -> None:
        path = Path(fp5_qtee_haptic.__file__).with_name("fp5-qtee-feedback.json")
        theme = json.loads(path.read_text(encoding="utf-8"))
        names = [p["name"] for p in theme["profiles"]]
        self.assertEqual(names, ["full", "quiet"])
        self.assertNotIn("silent", names)
        for profile in theme["profiles"]:
            by_name = {f["event-name"]: f for f in profile["feedbacks"]}
            self.assertEqual(by_name["fp5-qtee-match"]["durations"], [SUCCESS_MS])
            self.assertEqual(by_name["fp5-qtee-match"]["magnitudes"], [AMPLITUDE])
            self.assertEqual(
                by_name["fp5-qtee-miss"]["durations"],
                [MISS_ON_MS, MISS_OFF_MS, MISS_ON_MS],
            )
            self.assertEqual(
                by_name["fp5-qtee-miss"]["magnitudes"],
                [AMPLITUDE, 0.0, AMPLITUDE],
            )


class PlayTest(unittest.TestCase):
    def _led(self, root: Path, name: str = "aw86927") -> Path:
        led = root / name
        led.mkdir()
        for leaf in ("duration", "activate", "gain"):
            (led / leaf).write_text("0")
        return led

    def test_finds_aw869_and_skips_the_backlight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backlight").mkdir()
            (root / "backlight" / "duration").write_text("0")
            (root / "backlight" / "activate").write_text("0")
            led = self._led(root)
            self.assertEqual(find_led(root), led)
            self.assertIsNone(find_led(root / "missing"))

    def test_silent_writes_nothing(self) -> None:
        writes: list[tuple[str, str]] = []
        ran: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            self._led(Path(tmp))
            haptic = Haptic(
                profile="silent",
                leds=Path(tmp),
                writer=lambda path, text: writes.append((path.name, text)),
                sleeper=lambda _s: None,
                runner=ran.append,
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("success"), "none")
            self.assertEqual(haptic.play_sync("miss"), "none")
        self.assertEqual(writes, [])
        self.assertEqual(ran, [])

    def test_success_is_one_sysfs_click(self) -> None:
        writes: list[tuple[str, str]] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            self._led(Path(tmp))
            haptic = Haptic(
                profile="quiet",
                leds=Path(tmp),
                writer=lambda path, text: writes.append((path.name, text)),
                sleeper=sleeps.append,
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("success"), "sysfs")
        self.assertEqual(
            writes,
            [("gain", str(GAIN)), ("duration", "20"), ("activate", "1"), ("activate", "0")],
        )
        self.assertEqual(sleeps, [0.02])

    def test_miss_is_two_pulses_with_one_gain(self) -> None:
        writes: list[tuple[str, str]] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            self._led(Path(tmp), name="haptic_hv")
            haptic = Haptic(
                profile="full",
                leds=Path(tmp),
                writer=lambda path, text: writes.append((path.name, text)),
                sleeper=sleeps.append,
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("miss"), "sysfs")
        self.assertEqual(writes.count(("gain", str(GAIN))), 1)
        self.assertEqual(writes.count(("duration", "30")), 2)
        self.assertEqual(sleeps, [0.03, 0.1, 0.03])

    def test_no_led_falls_back_to_fbcli(self) -> None:
        ran: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            haptic = Haptic(
                profile="full",
                leds=Path(tmp),
                runner=ran.append,
                sleeper=lambda _s: (_ for _ in ()).throw(AssertionError("slept")),
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("miss"), "fbcli")
            self.assertEqual(haptic.play_sync("lockout"), "none")
        self.assertEqual(ran, [fbcli_args("miss")])

    def test_env_profile_overrides_the_bus(self) -> None:
        called = []
        haptic = Haptic(
            read_profile=lambda: called.append(1) or "full",
            enabled=True,
        )
        with mock.patch.dict("os.environ", {"FP5_QTEE_HAPTIC_PROFILE": "silent"}):
            self.assertEqual(haptic.profile(), "silent")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
