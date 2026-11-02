#!/usr/bin/env python3
"""Android haptic timings. No feedbackd and no real vibrator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import struct

import fp5_qtee_haptic
from fp5_qtee_haptic import (
    AMPLITUDE,
    GAIN,
    MISS_OFF_MS,
    MISS_ON_MS,
    RUMBLE_MAGNITUDE,
    SUCCESS_MS,
    Haptic,
    enroll_accept,
    fbcli_args,
    FEEDBACKD_GAIN,
    FULL_GAIN,
    ff_gain_event,
    ff_play_event,
    ff_steps,
    find_ff,
    find_led,
    parse_profile,
    plays,
    rumble_effect,
    sysfs_steps,
    waveform,
)


class WaveTest(unittest.TestCase):
    def test_success_is_one_click(self) -> None:
        self.assertEqual(waveform("success"), [(AMPLITUDE, SUCCESS_MS)])
        self.assertEqual(SUCCESS_MS, 60)

    def test_miss_is_two_pulses_160ms_apart(self) -> None:
        pulses = waveform("miss")
        self.assertEqual(
            pulses,
            [(AMPLITUDE, MISS_ON_MS), (0.0, MISS_OFF_MS), (AMPLITUDE, MISS_ON_MS)],
        )
        assert pulses is not None
        self.assertEqual(pulses[0][1] + pulses[1][1], 160)
        self.assertEqual(pulses[0][0], pulses[2][0])

    def test_lockout_is_three_long_pulses(self) -> None:
        # Out of tries: distinct from the double 60 ms miss.
        pulses = waveform("lockout")
        assert pulses is not None
        on = [ms for amp, ms in pulses if amp > 0]
        self.assertEqual(on, [150, 150, 150])
        self.assertEqual(fbcli_args("lockout"), ["fbcli", "-A", "org.fp5.qtee", "-E", "fp5-qtee-lockout"])
        self.assertIsNone(waveform("lockout-permanent"))
        self.assertIsNone(waveform(""))
        self.assertIsNone(fbcli_args("lockout-permanent"))

    def test_theme_json_has_every_event(self) -> None:
        theme = json.loads((Path(fp5_qtee_haptic.__file__).parent / "fp5-qtee-feedback.json").read_text())
        for profile in theme["profiles"]:
            names = {f["event-name"] for f in profile["feedbacks"]}
            for kind in ("success", "miss", "toofast", "lockout"):
                self.assertIn(fbcli_args(kind)[-1], names)
        self.assertIsNone(ff_steps("lockout-permanent"))

    def test_ff_steps_keep_the_160ms_gap(self) -> None:
        self.assertEqual(ff_steps("success"), [("rumble", SUCCESS_MS)])
        self.assertEqual(
            ff_steps("miss"),
            [("rumble", MISS_ON_MS), ("sleep", 160), ("rumble", MISS_ON_MS)],
        )

    def test_rumble_magnitude_is_android_gain(self) -> None:
        raw = rumble_effect(SUCCESS_MS)
        self.assertEqual(len(raw), 48)
        typ, effect_id = struct.unpack_from("<Hh", raw, 0)
        self.assertEqual(typ, 0x50)
        self.assertEqual(effect_id, -1)
        length, delay = struct.unpack_from("<HH", raw, 10)
        strong, weak = struct.unpack_from("<HH", raw, 16)
        self.assertEqual((length, delay), (SUCCESS_MS, 0))
        self.assertEqual(strong, RUMBLE_MAGNITUDE)
        self.assertEqual(strong, 0xFFFF)
        self.assertEqual(weak, 0)
        self.assertEqual(len(ff_play_event(3)), 24)
        gain = ff_gain_event(FULL_GAIN)
        self.assertEqual(len(gain), 24)
        code, value = struct.unpack_from("<Hi", gain, 18)
        self.assertEqual(code, 0x60)
        self.assertEqual(value, 0xFFFF)
        self.assertEqual(FEEDBACKD_GAIN, 0xC000)

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

    def test_profile_is_read_on_the_session_bus(self) -> None:
        completed = fp5_qtee_haptic.subprocess.CompletedProcess(
            args=[], returncode=0, stdout='s "full"\n'
        )
        with mock.patch(
            "fp5_qtee_haptic.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(fp5_qtee_haptic.read_bus_profile(), "full")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "busctl")
        self.assertIn("--user", argv)
        self.assertEqual(argv[argv.index("--user") + 1], "get-property")


class ThemeTest(unittest.TestCase):
    def test_fragment_matches_the_waveform(self) -> None:
        path = Path(fp5_qtee_haptic.__file__).with_name("fp5-qtee-feedback.json")
        theme = json.loads(path.read_text(encoding="utf-8"))
        names = [p["name"] for p in theme["profiles"]]
        self.assertEqual(theme["name"], "fp5-qtee")
        self.assertEqual(theme["parent-name"], "default")
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
            self.assertEqual(
                by_name["fp5-qtee-toofast"]["durations"],
                [ms for _a, ms in fp5_qtee_haptic.waveform("toofast")],
            )
            self.assertEqual(
                by_name["fp5-qtee-toofast"]["magnitudes"],
                [a for a, _ms in fp5_qtee_haptic.waveform("toofast")],
            )


class TooFastWaveTest(unittest.TestCase):
    def test_distinct_triple_tick(self) -> None:
        wave = fp5_qtee_haptic.waveform("toofast")
        pulses = [ms for a, ms in wave if a > 0]
        self.assertEqual(len(pulses), 3)
        self.assertNotEqual(wave, fp5_qtee_haptic.waveform("miss"))
        self.assertNotEqual(wave, fp5_qtee_haptic.waveform("success"))
        self.assertLess(sum(ms for _a, ms in wave), 250)
        self.assertEqual(
            fp5_qtee_haptic.fbcli_args("toofast")[-1], "fp5-qtee-toofast"
        )
        steps = fp5_qtee_haptic.ff_steps("toofast")
        self.assertEqual([op for op, _v in steps].count("rumble"), 3)
        self.assertEqual([v for op, v in steps if op == "sleep"], [85, 85])


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
            [("gain", str(GAIN)), ("duration", "60"), ("activate", "1"), ("activate", "0")],
        )
        self.assertEqual(sleeps, [0.06])

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
        self.assertEqual(writes.count(("duration", "60")), 2)
        self.assertEqual(sleeps, [0.06, 0.1, 0.06])

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
            self.assertEqual(haptic.play_sync("lockout-permanent"), "none")
        self.assertEqual(ran, [fbcli_args("miss")])

    def test_theme_without_the_event_falls_back_to_evdev(self) -> None:
        # feedbackd's active theme (e.g. fp5-strong) lacks fp5-qtee-match:
        # fbcli plays nothing, so the rumble must play instead.
        ran: list[list[str]] = []
        played: list[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            haptic = Haptic(
                profile="full",
                leds=Path(tmp),
                runner=lambda args: ran.append(args) or False,
                rumble=played.append,
                sleeper=lambda _s: None,
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("success"), "evdev")
        self.assertEqual(ran, [fbcli_args("success")])
        self.assertEqual(played, [SUCCESS_MS])

    def test_fbcli_missed(self) -> None:
        self.assertTrue(fp5_qtee_haptic.fbcli_missed(
            "Triggering feedback for event 'fp5-qtee-match'\n"
            "No feedback found for 'fp5-qtee-match' at level 'full'\n"))
        self.assertFalse(fp5_qtee_haptic.fbcli_missed(
            "Triggering feedback for event 'fp5-qtee-match'\n"))

    def test_no_feedbackd_uses_evdev_rumble(self) -> None:
        played: list[int] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            haptic = Haptic(
                profile="full",
                leds=Path(tmp),
                feedback=False,
                rumble=played.append,
                sleeper=sleeps.append,
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("miss"), "evdev")
            self.assertEqual(haptic.play_sync("success"), "evdev")
            self.assertEqual(haptic.play_sync("lockout-permanent"), "none")
        self.assertEqual(played, [MISS_ON_MS, MISS_ON_MS, SUCCESS_MS])
        self.assertEqual(sleeps, [0.16])

    def test_evdev_uses_full_gain_for_the_pulse_only(self) -> None:
        played: list[int] = []
        gains: list[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            haptic = Haptic(
                profile="full",
                leds=Path(tmp),
                feedback=False,
                rumble=played.append,
                gain=gains.append,
                sleeper=lambda _s: None,
                enabled=True,
            )
            self.assertEqual(haptic.play_sync("success"), "evdev")
            self.assertEqual(haptic.play_sync("miss"), "evdev")
        self.assertEqual(played, [SUCCESS_MS, MISS_ON_MS, MISS_ON_MS])
        self.assertEqual(gains, [FULL_GAIN, FEEDBACKD_GAIN, FULL_GAIN, FEEDBACKD_GAIN])

    def test_finds_aw869_event_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dev = root / "dev"
            sysfs = root / "sys"
            event = dev / "event3"
            event.parent.mkdir()
            event.write_text("")
            name = sysfs / "event3" / "device"
            name.mkdir(parents=True)
            (name / "name").write_text("aw86927-haptics\n")
            (sysfs / "event0" / "device").mkdir(parents=True)
            (dev / "event0").write_text("")
            (sysfs / "event0" / "device" / "name").write_text("gpio-keys\n")
            self.assertEqual(find_ff(dev, sysfs), event)
            self.assertIsNone(find_ff(dev / "missing", sysfs))

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
