#!/usr/bin/env python3
"""Cross-finger analysis tests. Run: python3 test_fp5_qtee_crossfinger.py

Every session line and score below is SYNTHETIC, written to look like
fp5-qtee-session output. None of it was captured from a phone.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import fp5_qtee_crossfinger as cf


def synthetic_press(hit: bool, scores: list[int], which: int = 1) -> list[str]:
    lines = [
        f"AUTH {which} arm mode=1 irq=10",
        "skip leftover itype=0x212 esd=0",
        f"AUTH {which} REAL DOWN itype=0x2",
        "CAP cmd=0x1013 rc=0 itype=0x2 esd=0 avgv=300 req_b=1536 rsp_b=2048",
    ]
    lines += [
        f"REPORT_EV5 qsee: focaltech-lib FtVerifySubTemplate() score = {s}, matchCnts = 2"
        for s in scores
    ]
    lines.append(f"AUTH {which} report rc={0 if hit else -1} fid={1253023920 if hit else 0}")
    if hit:
        lines.append(f"AUTH HIT {which} itype=0x2 avgv=300 fid=1253023920 rc=0")
    else:
        lines.append(f"AUTH FAIL {which} itype=0x2 esd=0 avgv=300 fid=0 rc=-1")
    return lines


def records(label: str, results: list[tuple[str, list[int]]]) -> list[dict]:
    return [
        {"label": label, "result": r, "scores": s, "index": i + 1}
        for i, (r, s) in enumerate(results)
    ]


def genuine(n: int, hit_every: int = 1, score: int = 20) -> list[dict]:
    return records("A", [("HIT" if i % hit_every == 0 else "FAIL", [score + i % 5]) for i in range(n)])


def impostor(n: int, hits: int = 0, score: int = 80) -> list[dict]:
    return records("B", [("HIT" if i < hits else "FAIL", [score + i % 7]) for i in range(n)])


class ParseTest(unittest.TestCase):
    def test_hit_with_scores(self) -> None:
        p = cf.parse_presses(synthetic_press(True, [31, 18]) + ["auth single 0", "session_exit:0"])
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].result, cf.HIT)
        self.assertEqual(p[0].scores, [31, 18])
        self.assertTrue(p[0].down)

    def test_fail_after_down_is_scored(self) -> None:
        p = cf.parse_presses(synthetic_press(False, [95]))
        self.assertEqual(p[0].result, cf.FAIL)
        self.assertEqual(p[0].scores, [95])

    def test_arm_rejected_is_error(self) -> None:
        p = cf.parse_presses(["AUTH FAIL 1 itype=0x0 esd=0 avgv=0 rc=-203 fid=0", "session_exit:2"])
        self.assertEqual(p[0].result, cf.ERROR)

    def test_no_finger_and_empty(self) -> None:
        p = cf.parse_presses(["AUTH 1 arm mode=1 irq=3", "AUTH 1 no finger"])
        self.assertEqual(p[0].result, cf.NOFINGER)
        p = cf.parse_presses([
            "AUTH 1 arm mode=1 irq=3",
            "AUTH 1 REAL DOWN itype=0x2",
            "AUTH 1 empty avgv=900,901,902",
        ])
        self.assertEqual(p[0].result, cf.EMPTY)

    def test_session_crash_is_error(self) -> None:
        p = cf.parse_presses(["firmware missing: focal32 needs ...", "session_exit:1"])
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].result, cf.ERROR)
        self.assertIn("session_exit", p[0].detail)

    def test_scores_outside_a_press_are_ignored(self) -> None:
        p = cf.parse_presses(
            ["SYNC_STATS qsee: FtVerifySubTemplate() score = 5, matchCnts = 1"]
            + synthetic_press(True, [22])
        )
        self.assertEqual(p[0].scores, [22])

    def test_auth_pair_gives_two_presses(self) -> None:
        p = cf.parse_presses(synthetic_press(True, [20], 1) + synthetic_press(False, [90], 2))
        self.assertEqual([x.result for x in p], [cf.HIT, cf.FAIL])


class ScoreTest(unittest.TestCase):
    def test_best_score_direction(self) -> None:
        self.assertEqual(cf.best_score([40, 12, 30], "distance", False), 12)
        self.assertEqual(cf.best_score([40, 12, 30], "similarity", False), 40)

    def test_zero_distance_is_no_features(self) -> None:
        self.assertEqual(cf.best_score([0, 33], "distance", False), 33)
        self.assertIsNone(cf.best_score([0], "distance", False))
        self.assertEqual(cf.best_score([0, 33], "distance", True), 0)
        self.assertIsNone(cf.best_score([], "distance", False))

    def test_default_max_b_hits(self) -> None:
        self.assertEqual(cf.default_max_b_hits(50), 1)
        self.assertEqual(cf.default_max_b_hits(10), 1)
        self.assertEqual(cf.default_max_b_hits(0), 1)
        self.assertEqual(cf.default_max_b_hits(100), 2)
        self.assertEqual(cf.default_max_b_hits(200), 4)


class AnalyzeTest(unittest.TestCase):
    def test_clean_separation_passes(self) -> None:
        s = cf.analyze(genuine(50) + impostor(50))
        self.assertEqual(s["verdict"], "PASS", s["reasons"])
        self.assertEqual(s["A"]["hits"], 50)
        self.assertEqual(s["B"]["hits"], 0)
        self.assertEqual(cf.exit_code(s["verdict"]), 0)

    def test_one_b_hit_in_fifty_is_allowed(self) -> None:
        s = cf.analyze(genuine(50) + impostor(50, hits=1))
        self.assertEqual(s["verdict"], "PASS", s["reasons"])

    def test_two_b_hits_in_fifty_fail(self) -> None:
        s = cf.analyze(genuine(50) + impostor(50, hits=2))
        self.assertEqual(s["verdict"], "FAIL")
        self.assertTrue(any("finger B matched 2" in r for r in s["reasons"]))
        self.assertEqual(cf.exit_code(s["verdict"]), 1)

    def test_max_b_hits_is_configurable(self) -> None:
        s = cf.analyze(genuine(50) + impostor(50, hits=2), cf.Thresholds(max_b_hits=2))
        self.assertEqual(s["verdict"], "PASS", s["reasons"])
        s = cf.analyze(genuine(50) + impostor(50, hits=1), cf.Thresholds(max_b_hits=0))
        self.assertEqual(s["verdict"], "FAIL")

    def test_b_scores_as_close_as_a_fail(self) -> None:
        s = cf.analyze(genuine(50) + impostor(50, score=19))
        self.assertEqual(s["verdict"], "FAIL")
        self.assertTrue(any("median B" in r for r in s["reasons"]))

    def test_one_close_b_press_fails_best_b_check(self) -> None:
        b = impostor(50)
        b[7]["scores"] = [15]  # closer than median A (about 22)
        s = cf.analyze(genuine(50) + b)
        self.assertEqual(s["verdict"], "FAIL")
        self.assertTrue(any("closest B press" in r for r in s["reasons"]))
        s = cf.analyze(genuine(50) + b, cf.Thresholds(best_b_check=False))
        self.assertEqual(s["verdict"], "PASS", s["reasons"])

    def test_margin(self) -> None:
        s = cf.analyze(genuine(50, score=20) + impostor(50, score=30), cf.Thresholds(margin=20))
        self.assertEqual(s["verdict"], "FAIL")
        s = cf.analyze(genuine(50, score=20) + impostor(50, score=30), cf.Thresholds(margin=5))
        self.assertEqual(s["verdict"], "PASS", s["reasons"])

    def test_similarity_direction_flips(self) -> None:
        a = genuine(50, score=80)
        b = impostor(50, score=10)
        self.assertEqual(cf.analyze(a + b, cf.Thresholds(direction="similarity"))["verdict"], "PASS")
        self.assertEqual(cf.analyze(a + b)["verdict"], "FAIL")

    def test_a_rarely_hits_is_inconclusive(self) -> None:
        s = cf.analyze(genuine(50, hit_every=4) + impostor(50))
        self.assertEqual(s["verdict"], "INCONCLUSIVE")
        self.assertEqual(cf.exit_code(s["verdict"]), 2)

    def test_too_few_presses_is_inconclusive(self) -> None:
        s = cf.analyze(genuine(5) + impostor(5))
        self.assertEqual(s["verdict"], "INCONCLUSIVE")

    def test_missing_scores_is_inconclusive_unless_disabled(self) -> None:
        a = records("A", [("HIT", [])] * 20)
        b = records("B", [("FAIL", [])] * 20)
        self.assertEqual(cf.analyze(a + b)["verdict"], "INCONCLUSIVE")
        self.assertEqual(cf.analyze(a + b, cf.Thresholds(score_check=False))["verdict"], "PASS")

    def test_unscored_attempts_do_not_count(self) -> None:
        recs = genuine(50) + impostor(50)
        recs += records("B", [("NOFINGER", []), ("EMPTY", []), ("ERROR", [])])
        s = cf.analyze(recs)
        self.assertEqual(s["B"]["presses"], 50)
        self.assertEqual(s["B"]["unscored_attempts"], 3)
        self.assertEqual(s["verdict"], "PASS", s["reasons"])

    def test_b_hits_fail_even_when_inconclusive(self) -> None:
        s = cf.analyze(genuine(5) + impostor(5, hits=3))
        self.assertEqual(s["verdict"], "FAIL")

    def test_compare(self) -> None:
        pre = cf.analyze(genuine(50) + impostor(50))
        post = cf.analyze(genuine(50, hit_every=2) + impostor(50, hits=2))
        c = cf.compare(pre, post)
        self.assertEqual(c["pre_verdict"], "PASS")
        self.assertEqual(c["post_verdict"], "FAIL")
        self.assertAlmostEqual(c["a_hit_rate_delta"], -0.5)
        self.assertEqual(c["b_hits_delta"], 2)


class RunTest(unittest.TestCase):
    def test_order(self) -> None:
        self.assertEqual(cf.press_order(2, "blocks"), [("A", 1), ("A", 2), ("B", 1), ("B", 2)])
        self.assertEqual(cf.press_order(2, "alternate"), [("A", 1), ("B", 1), ("A", 2), ("B", 2)])

    def test_run_retries_unscored_presses(self) -> None:
        script = {
            ("A", 1, 1): synthetic_press(True, [20]),
            ("A", 2, 1): ["AUTH 1 arm mode=1 irq=1", "AUTH 1 no finger"],
            ("A", 2, 2): synthetic_press(True, [21]),
            ("B", 1, 1): synthetic_press(False, [90]),
            ("B", 2, 1): synthetic_press(False, [91]),
        }
        recs = cf.run_test(
            2, "pre-reboot", "blocks", 3, cf.Thresholds(),
            lambda label, idx, att: cf.parse_presses(script[(label, idx, att)]),
            lambda _m: None,
            now=lambda: "2026-01-01T00:00:00+0000",
        )
        self.assertEqual(len(recs), 5)
        self.assertEqual([r["result"] for r in recs], ["HIT", "NOFINGER", "HIT", "FAIL", "FAIL"])
        self.assertEqual([r["counted"] for r in recs], [True, False, True, True, True])
        self.assertEqual(recs[2]["attempt"], 2)
        self.assertEqual(recs[3]["best_score"], 90)
        self.assertTrue(all(r["phase"] == "pre-reboot" for r in recs))

    def test_run_gives_up_after_retries(self) -> None:
        recs = cf.run_test(
            1, "single", "blocks", 1, cf.Thresholds(),
            lambda *_a: cf.parse_presses(["session_exit:1"]),
            lambda _m: None,
        )
        self.assertEqual(len(recs), 4)  # A: 2 attempts, B: 2 attempts
        self.assertFalse(any(r["counted"] for r in recs))

    def test_session_argv_matches_app(self) -> None:
        argv = cf.session_argv(Path("/x/fp5-qtee-session"), "auth1", "/lib/firmware/qsee", ["--flag"], 400)
        self.assertEqual(
            argv,
            ["sudo", "-n", "timeout", "-k", "15", "400", "/x/fp5-qtee-session", "--flag", "auth1",
             "/lib/firmware/qsee"],
        )


class FilesTest(unittest.TestCase):
    def test_write_read_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            recs = [{"phase": "pre-reboot", "seq": 1, "label": "A", "index": 1, "attempt": 1,
                     "result": "HIT", "counted": True, "scores": [20, 30], "best_score": 20,
                     "timestamp": "t", "detail": "AUTH HIT 1"}]
            j = dd / "crossfinger-20260101-000000-pre-reboot.jsonl"
            c = dd / "crossfinger-20260101-000000-pre-reboot.csv"
            cf.write_records(recs, j, c)
            self.assertEqual(cf.read_records(j), recs)
            text = c.read_text()
            self.assertIn("label", text.splitlines()[0])
            self.assertIn("20 30", text)
            (dd / "crossfinger-20260102-000000-pre-reboot.jsonl").write_text("")
            (dd / "crossfinger-20260103-000000-post-reboot.jsonl").write_text("")
            self.assertEqual(cf.latest_pre_reboot(dd).name, "crossfinger-20260102-000000-pre-reboot.jsonl")

    def test_main_end_to_end_with_fake_session(self) -> None:
        """Drive main() against a fake session script printing synthetic lines."""
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            fake = dd / "fake-session"
            fake.write_text(
                "#!/bin/sh\n"
                "n=$(cat \"$0.count\" 2>/dev/null || echo 0); n=$((n+1)); echo $n > \"$0.count\"\n"
                "echo 'AUTH 1 arm mode=1 irq=1'\n"
                "echo 'AUTH 1 REAL DOWN itype=0x2'\n"
                "if [ $n -le 12 ]; then\n"
                "  echo \"REPORT_EV5 qsee: FtVerifySubTemplate() score = $((15 + n % 4)), matchCnts = 2\"\n"
                "  echo 'AUTH HIT 1 itype=0x2 avgv=300 fid=7 rc=0'\n"
                "else\n"
                "  echo \"REPORT_EV5 qsee: FtVerifySubTemplate() score = $((70 + n % 9)), matchCnts = 2\"\n"
                "  echo 'AUTH FAIL 1 itype=0x2 esd=0 avgv=300 fid=0 rc=-1'\n"
                "fi\n"
                "echo 'session_exit:0'\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            # session_argv puts sudo/timeout first; replace them for the test.
            orig = cf.session_argv
            cf.session_argv = lambda b, m, f, e, l: [str(b), *e, m, f]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cf.main(["-n", "12", "--session", str(fake), "--log-dir", str(dd),
                                  "--phase", "pre-reboot"])
            finally:
                cf.session_argv = orig
            self.assertEqual(rc, 0)
            jsonl = sorted(dd.glob("crossfinger-*-pre-reboot.jsonl"))
            self.assertEqual(len(jsonl), 1)
            recs = cf.read_records(jsonl[0])
            self.assertEqual(len(recs), 24)
            self.assertEqual({r["label"] for r in recs[:12]}, {"A"})
            summary = json.loads(next(dd.glob("*-summary.json")).read_text())
            self.assertEqual(summary["verdict"], "PASS")
            self.assertTrue(list(dd.glob("*.csv")))
            self.assertTrue(list(dd.glob("*-session.txt")))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cf.main(["--analyze", str(jsonl[0])]), 0)
            self.assertIn("verdict: PASS", out.getvalue())

            # Post-reboot run compares with the pre-reboot file.
            (dd / "fake-session.count").unlink()
            cf.session_argv = lambda b, m, f, e, l: [str(b), *e, m, f]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cf.main(["-n", "12", "--session", str(fake), "--log-dir", str(dd),
                                  "--phase", "post-reboot"])
            finally:
                cf.session_argv = orig
            self.assertEqual(rc, 0)
            post = json.loads(next(dd.glob("*-post-reboot-summary.json")).read_text())
            self.assertEqual(post["compare_file"], str(jsonl[0]))
            self.assertEqual(post["compare"]["pre_verdict"], "PASS")
            self.assertEqual(post["compare"]["b_hits_delta"], 0)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    unittest.main()
