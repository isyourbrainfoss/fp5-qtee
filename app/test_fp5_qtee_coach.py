#!/usr/bin/env python3
"""Coach lines from fp5-qtee-session. Run: python3 test_fp5_qtee_coach.py"""

from __future__ import annotations

import unittest

from fp5_qtee_coach import Coach


def play(mode: str, lines: list[str]) -> Coach:
    c = Coach()
    c.begin(mode)
    for line in lines:
        c.on_line(line)
    return c


class CoachTest(unittest.TestCase):
    def test_ready_ignores_irq_noise(self) -> None:
        c = play(
            "enroll",
            [
                "qsee off 1->2 bytes=10 d_avg=863 saw_avg=1 d_ity=0x2 saw_ity=1",
                "interrupt type: 0x0(1=idle 2=down ...)",
                "irq_count 11",
            ],
        )
        self.assertEqual(c.hero, "STARTING")
        self.assertFalse(c.ok)

    def test_press_then_reject_then_remaining(self) -> None:
        c = play(
            "enroll",
            [
                "loadFromBuffer ioctl_rc=0 qret=0 success dist='tzappfingerprint'",
                "wait finger sample=0 irq=11",
                "REAL DOWN itype=0x2",
                "PRESS REJECT avgv=900,901,902",
                "wait finger sample=1 irq=12",
                "REAL DOWN itype=0x2",
                "rem=19 sample=1",
            ],
        )
        self.assertEqual(c.hero, "LIFT")
        self.assertEqual(c.rem, 19)
        self.assertIn("19 presses left", c.sub)
        self.assertFalse(c.saved)

    def test_no_finger_is_not_saved(self) -> None:
        c = play(
            "enroll",
            [
                "wait finger sample=0 irq=19",
                "no finger irq sample=0",
                "enroll done rem=4294967295 wrote=0 save_ms=0",
                "session_exit:2",
            ],
        )
        self.assertEqual(c.hero, "NO FINGER")
        self.assertFalse(c.saved)
        self.assertFalse(c.ok)
        self.assertTrue(c.done)
        self.assertEqual(c.exit_code, 2)

    def test_short_save_is_not_saved(self) -> None:
        c = play(
            "enroll",
            [
                "rem=0 sample=19",
                "enroll done rem=0 wrote=1 save_ms=5",
                "session_exit:0",
            ],
        )
        self.assertEqual(c.hero, "NOT SAVED")
        self.assertFalse(c.ok)

    def test_missing_firmware_names_the_fetch_script(self) -> None:
        c = play(
            "enroll",
            [
                "firmware missing: cannot read /lib/firmware/qsee/focal32.mdt (No such file or directory)",
                "firmware missing: focal32 needs focal32.mdt and focal32.b00..focal32.b07 in /lib/firmware/qsee",
                "firmware missing: copy them from a Fairphone 5 stock image with scripts/fetch-focal32-firmware.sh (see README, Firmware)",
                "session_exit:1",
            ],
        )
        self.assertEqual(c.hero, "FAILED")
        self.assertIn("fetch-focal32-firmware.sh", c.sub)
        self.assertFalse(c.ok)

    def test_template_write_is_saved(self) -> None:
        c = play(
            "enroll",
            [
                "rem=0 sample=19",
                "SAVE ms=210 rpmb_cmd=0x103 rpmb_result=0x0 valid=1 template=1 name=ff_template_0_1.bin",
                "enroll done rem=0 wrote=1 save_ms=210",
                "session_exit:0",
            ],
        )
        self.assertEqual(c.hero, "SAVED")
        self.assertTrue(c.saved)
        self.assertTrue(c.ok)

    def test_wrote_without_clean_exit_is_not_ok(self) -> None:
        c = play(
            "enroll",
            [
                "enroll done rem=0 wrote=1 save_ms=200",
                "session_exit:1",
            ],
        )
        self.assertTrue(c.saved)
        self.assertFalse(c.ok)
        self.assertEqual(c.hero, "SAVED")

    def test_auth_pair(self) -> None:
        c = play(
            "auth",
            [
                "AUTH 1 arm mode=9",
                "AUTH 1 REAL DOWN itype=0x2",
                "AUTH HIT 1 itype=0x2 avgv=280 rc=0",
                "AUTH 2 arm mode=9",
                "AUTH HIT 2 itype=0x2 avgv=290 rc=0",
                "auth pair 0 0",
                "session_exit:0",
            ],
        )
        self.assertEqual(c.hero, "MATCH")
        self.assertTrue(c.matched)
        self.assertTrue(c.ok)

    def test_one_hit_is_not_a_match(self) -> None:
        c = play(
            "auth",
            [
                "AUTH 1 arm mode=9",
                "AUTH HIT 1 itype=0x2 avgv=280 rc=0",
                "AUTH 2 arm mode=9",
                "AUTH 2 empty avgv=966,966,966",
                "auth pair 0 2",
                "session_exit:2",
            ],
        )
        self.assertEqual(c.hero, "NO MATCH")
        self.assertFalse(c.matched)
        self.assertFalse(c.ok)

    def test_score_diary_is_not_a_match(self) -> None:
        c = play(
            "auth",
            [
                "AUTH 1 arm mode=1",
                "AUTH 1 REAL DOWN itype=0x2",
                "REPORT_EV5 qsee: FtVerifySubTemplate() score = 0, matchCnts = 0",
                "AUTH FAIL 1 itype=0x2 esd=0 avgv=300 fid=0 rc=-11",
                "AUTH 2 arm mode=1",
                "REPORT_EV5 qsee: FtVerifySubTemplate() score = 12, matchCnts = 1",
                "AUTH FAIL 2 itype=0x2 esd=0 avgv=347 fid=0 rc=-11",
                "auth pair 2 2",
                "session_exit:2",
            ],
        )
        self.assertEqual(c.hero, "NO MATCH")
        self.assertFalse(c.matched)
        self.assertFalse(c.ok)
        self.assertIn("0", c.sub)
        self.assertIn("12", c.sub)

    def test_leftover_is_not_a_finger(self) -> None:
        c = play(
            "auth",
            [
                "AUTH 1 arm mode=9",
                "AUTH 1 skip leftover itype=0x212 esd=0",
            ],
        )
        self.assertEqual(c.hero, "LIFT")
        self.assertNotIn("MATCH", c.hero)
        self.assertFalse(c.ok)

    def test_enroll_rejected(self) -> None:
        c = play("enroll", ["ENROLL rejected rc=-203"])
        self.assertEqual(c.hero, "FAILED")
        self.assertFalse(c.ok)

    def test_report_crash_leaves_hold(self) -> None:
        c = play(
            "enroll",
            [
                "wait finger sample=0 irq=29",
                "REAL DOWN itype=0x2",
                "REPORT_EV5 invoke rc=0 qret=0xffffffa6 errno=0",
                "session_exit:-1",
            ],
        )
        self.assertEqual(c.hero, "FAILED")
        self.assertFalse(c.ok)
        self.assertTrue(c.done)
        self.assertIn("stopped", c.sub)

    def test_lines_after_exit_do_not_count(self) -> None:
        c = play(
            "enroll",
            [
                "no finger irq sample=0",
                "enroll done rem=4294967295 wrote=0 save_ms=0",
                "session_exit:2",
                "REAL DOWN itype=0x2",
                "rem=0 sample=1",
            ],
        )
        self.assertEqual(c.hero, "NO FINGER")
        self.assertFalse(c.saved)
        self.assertIsNone(c.rem)


if __name__ == "__main__":
    unittest.main()
