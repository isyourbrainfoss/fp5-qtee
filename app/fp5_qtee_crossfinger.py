#!/usr/bin/env python3
"""Cross-finger rejection test for fp5-qtee-session.

Finger A is the enrolled finger, finger B any other finger. The driver runs
the session once per press (``auth1`` mode, invoked the same way the Finger
app invokes it: ``sudo -n timeout ... <session> auth1 <firmware>``). It asks
for N presses of A and N presses of B and logs every press: label, HIT/FAIL,
FtVerifySubTemplate scores and a timestamp.

Results go to ~/fp5-qtee-keep/logs/crossfinger-<stamp>-<phase>.{jsonl,csv},
with a -summary.json and the raw session output in -session.txt.

Verdict (see analyze(); every threshold is a CLI option):
  PASS          when all checks below pass.
  FAIL          (exit 1) when
                  - B hits > max_b_hits (default max(1, round(N_B / 50))), or
                  - B scores are not clearly worse than A scores: with
                    distance scores (lower = closer, the coach's reading),
                    median(B) must be > median(A) + margin, and the closest
                    B press (min B) must be > median(A). With similarity
                    scores the comparisons flip.
  INCONCLUSIVE  (exit 2) when there is not enough to judge: fewer than
                --min-presses scored presses per finger, A hit rate under
                --min-a-hit-rate (a matcher that rejects everything also
                "rejects" B), or no scores for A or B (unless --no-score-check).

Reboot cannot be automated here, so run once with --phase pre-reboot,
reboot, then run with --phase post-reboot. The post-reboot run is compared
with the newest pre-reboot file in the log directory (or --compare FILE).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fp5_qtee_coach import (  # noqa: E402  (shared session-line parsing)
    _AUTH_ARM,
    _AUTH_DOWN,
    _AUTH_EMPTY,
    _AUTH_FAIL,
    _AUTH_HIT,
    _AUTH_NO,
    _EXIT,
    _SCORE,
)

# Same places the Finger app looks (app/fp5-qtee-app.py).
SESSION_CANDIDATES = (
    Path("/home/user/fp5-qtee-keep/session/fp5-qtee-session"),
    Path("/tmp/fp5-qtee/fp5-qtee-session"),
)
FIRMWARE = "/lib/firmware/qsee"
DEFAULT_LOG_DIR = Path.home() / "fp5-qtee-keep" / "logs"

HIT = "HIT"
FAIL = "FAIL"
NOFINGER = "NOFINGER"
EMPTY = "EMPTY"
ERROR = "ERROR"
SCORED = (HIT, FAIL)  # results that count as a real, compared press


@dataclass
class Press:
    """One auth attempt parsed from session output."""

    result: str = ERROR
    scores: list[int] = field(default_factory=list)
    down: bool = False
    detail: str = ""


def parse_presses(lines: Iterable[str]) -> list[Press]:
    """Split session output into presses.

    A press starts at "AUTH n arm". "AUTH HIT n" / "AUTH FAIL n" after a
    REAL DOWN is a scored press. "AUTH FAIL n" without a down (the arm
    command itself failed), "no finger" and "empty" are not scored. A run
    that ends without any AUTH result yields one ERROR press.
    """
    presses: list[Press] = []
    cur: Press | None = None
    last = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        last = line
        if _AUTH_ARM.match(line):
            cur = Press()
            presses.append(cur)
            continue
        if _AUTH_HIT.match(line) or _AUTH_FAIL.match(line):
            if cur is None:
                cur = Press()
                presses.append(cur)
            hit = bool(_AUTH_HIT.match(line))
            if hit:
                cur.result = HIT
            else:
                cur.result = FAIL if cur.down else ERROR
            cur.detail = line
            cur = None
            continue
        if cur is None:
            continue
        if _AUTH_DOWN.match(line):
            cur.down = True
            continue
        m = _SCORE.search(line)
        if m:
            cur.scores.append(int(m.group(1)))
            continue
        if _AUTH_NO.match(line):
            cur.result = NOFINGER
            cur.detail = line
            cur = None
            continue
        if _AUTH_EMPTY.match(line):
            cur.result = EMPTY
            cur.detail = line
            cur = None
            continue
    if not presses:
        presses.append(Press(result=ERROR, detail=last))
    elif cur is not None and cur.result == ERROR and not cur.detail:
        cur.detail = last
    return presses


def best_score(scores: list[int], direction: str, keep_zero: bool) -> int | None:
    """Closest score of one press. Distance: lowest. Similarity: highest.

    With distance scores 0 means "no features" (coach), so it is dropped
    unless keep_zero is set.
    """
    vals = [s for s in scores if keep_zero or direction != "distance" or s != 0]
    if not vals:
        return None
    return min(vals) if direction == "distance" else max(vals)


def default_max_b_hits(n_b: int) -> int:
    """About one in fifty, and never below one."""
    return max(1, round(n_b / 50))


@dataclass
class Thresholds:
    direction: str = "distance"  # or "similarity"
    margin: float = 0.0
    max_b_hits: int | None = None  # None: default_max_b_hits(N_B)
    min_presses: int = 10
    min_a_hit_rate: float = 0.5
    score_check: bool = True
    best_b_check: bool = True
    keep_zero: bool = False


def _closer(x: float, y: float, direction: str) -> bool:
    """x is at least as close a match as y."""
    return x <= y if direction == "distance" else x >= y


def analyze(records: list[dict], th: Thresholds | None = None) -> dict:
    """Pure verdict from press records ({"label", "result", "scores", ...})."""
    th = th or Thresholds()
    out: dict = {"thresholds": asdict(th), "reasons": [], "warnings": []}
    per: dict[str, dict] = {}
    for label in ("A", "B"):
        rows = [r for r in records if r.get("label") == label and r.get("result") in SCORED]
        hits = sum(1 for r in rows if r["result"] == HIT)
        best = [
            b
            for b in (best_score(list(r.get("scores") or []), th.direction, th.keep_zero) for r in rows)
            if b is not None
        ]
        per[label] = {
            "presses": len(rows),
            "hits": hits,
            "hit_rate": (hits / len(rows)) if rows else 0.0,
            "scored": len(best),
            "median": statistics.median(best) if best else None,
            "min": min(best) if best else None,
            "max": max(best) if best else None,
            "unscored_attempts": sum(
                1 for r in records if r.get("label") == label and r.get("result") not in SCORED
            ),
        }
    out["A"], out["B"] = per["A"], per["B"]
    a, b = per["A"], per["B"]
    max_b = th.max_b_hits if th.max_b_hits is not None else default_max_b_hits(b["presses"])
    out["max_b_hits"] = max_b

    fail: list[str] = []
    inconclusive: list[str] = []
    if b["hits"] > max_b:
        fail.append(f"finger B matched {b['hits']} of {b['presses']} presses (allowed {max_b})")
    if a["presses"] < th.min_presses or b["presses"] < th.min_presses:
        inconclusive.append(
            f"too few scored presses (A {a['presses']}, B {b['presses']}, need {th.min_presses} each)"
        )
    if a["presses"] and a["hit_rate"] < th.min_a_hit_rate:
        inconclusive.append(
            f"finger A hit rate {a['hit_rate']:.0%} is under {th.min_a_hit_rate:.0%}; "
            "rejection of B means little"
        )
    if th.score_check:
        if a["median"] is None or b["median"] is None:
            inconclusive.append("no FtVerifySubTemplate scores for A or B (algorithm_log_level 2 needed)")
        else:
            worse = "higher" if th.direction == "distance" else "lower"
            if th.direction == "distance":
                median_ok = b["median"] > a["median"] + th.margin
                best_b = b["min"]
            else:
                median_ok = b["median"] < a["median"] - th.margin
                best_b = b["max"]
            if not median_ok:
                fail.append(
                    f"median B score {b['median']} is not clearly {worse} than median A "
                    f"{a['median']} (margin {th.margin}, {th.direction})"
                )
            if th.best_b_check and _closer(best_b, a["median"], th.direction):
                fail.append(
                    f"closest B press scored {best_b}, as close as median A {a['median']} ({th.direction})"
                )
    if fail:
        verdict = "FAIL"
    elif inconclusive:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "PASS"
    out["reasons"] = fail + inconclusive
    out["verdict"] = verdict
    return out


def compare(pre: dict, post: dict) -> dict:
    """Pre- vs post-reboot summary deltas (both from analyze())."""
    def delta(key: str, label: str):
        x, y = pre[label].get(key), post[label].get(key)
        return None if x is None or y is None else y - x

    return {
        "pre_verdict": pre["verdict"],
        "post_verdict": post["verdict"],
        "a_hit_rate_delta": delta("hit_rate", "A"),
        "b_hits_delta": delta("hits", "B"),
        "a_median_delta": delta("median", "A"),
        "b_median_delta": delta("median", "B"),
    }


def exit_code(verdict: str) -> int:
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


# ---- I/O -----------------------------------------------------------------

CSV_FIELDS = ("phase", "seq", "label", "index", "attempt", "result", "counted",
              "best_score", "scores", "timestamp", "detail")


def write_records(records: list[dict], jsonl: Path, csv_path: Path) -> None:
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["scores"] = " ".join(str(s) for s in r.get("scores") or [])
            w.writerow(row)


def read_records(jsonl: Path) -> list[dict]:
    with jsonl.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def latest_pre_reboot(log_dir: Path) -> Path | None:
    files = sorted(log_dir.glob("crossfinger-*-pre-reboot.jsonl"))
    return files[-1] if files else None


def session_bin() -> Path | None:
    for path in SESSION_CANDIDATES:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def session_argv(binary: Path, mode: str, firmware: str, extra: list[str], limit: int) -> list[str]:
    """Same shape as the Finger app: sudo -n timeout -k 15 <limit> <bin> ..."""
    return ["sudo", "-n", "timeout", "-k", "15", str(limit), str(binary), *extra, mode, firmware]


def run_session(argv: list[str], raw_log, on_line: Callable[[str], None]) -> tuple[int, list[str]]:
    lines: list[str] = []
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        raw_log.write(line)
        raw_log.flush()
        lines.append(line)
        on_line(line)
    return proc.wait(), lines


def press_order(n: int, order: str) -> list[tuple[str, int]]:
    if order == "alternate":
        seq = []
        for i in range(n):
            seq += [("A", i + 1), ("B", i + 1)]
        return seq
    return [("A", i + 1) for i in range(n)] + [("B", i + 1) for i in range(n)]


def run_test(
    n: int,
    phase: str,
    order: str,
    retries: int,
    th: Thresholds,
    run_one: Callable[[str, int, int], list[Press]],
    prompt: Callable[[str], None],
    now: Callable[[], str] | None = None,
) -> list[dict]:
    """Collect labelled presses. run_one(label, index, attempt) returns the
    parsed presses of one session run (normally exactly one)."""
    now = now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    records: list[dict] = []
    seq = 0
    last_label = None
    for label, idx in press_order(n, order):
        if label != last_label:
            prompt(f"\n=== Finger {label}: {n} presses. "
                   f"{'Enrolled finger.' if label == 'A' else 'A DIFFERENT finger (not enrolled).'} ===")
            last_label = label
        for attempt in range(1, retries + 2):
            prompt(f"[{label} {idx}/{n}] get ready with finger {label}; press when it says PRESS")
            presses = run_one(label, idx, attempt) or [Press(result=ERROR, detail="no output")]
            p = presses[0]
            counted = p.result in SCORED
            seq += 1
            rec = {
                "phase": phase,
                "seq": seq,
                "label": label,
                "index": idx,
                "attempt": attempt,
                "result": p.result,
                "counted": counted,
                "scores": p.scores,
                "best_score": best_score(p.scores, th.direction, th.keep_zero),
                "timestamp": now(),
                "detail": p.detail,
            }
            records.append(rec)
            prompt(f"[{label} {idx}/{n}] {p.result}"
                   f"{' scores ' + ','.join(map(str, p.scores)) if p.scores else ''}"
                   f"{'' if counted else ' (not counted, retry)'}")
            if counted:
                break
    return records


def format_summary(s: dict) -> str:
    a, b = s["A"], s["B"]
    lines = [
        f"verdict: {s['verdict']}",
        f"A: {a['hits']}/{a['presses']} hits ({a['hit_rate']:.0%}), scores median {a['median']} "
        f"min {a['min']} max {a['max']} (n={a['scored']}), unscored attempts {a['unscored_attempts']}",
        f"B: {b['hits']}/{b['presses']} hits ({b['hit_rate']:.0%}), scores median {b['median']} "
        f"min {b['min']} max {b['max']} (n={b['scored']}), unscored attempts {b['unscored_attempts']}",
        f"allowed B hits: {s['max_b_hits']}  score direction: {s['thresholds']['direction']}",
    ]
    lines += [f"  - {r}" for r in s["reasons"]]
    if "compare" in s:
        c = s["compare"]
        lines.append(
            f"vs pre-reboot ({s.get('compare_file')}): pre {c['pre_verdict']} -> post {c['post_verdict']}, "
            f"A hit rate delta {c['a_hit_rate_delta']}, B hits delta {c['b_hits_delta']}, "
            f"A median delta {c['a_median_delta']}, B median delta {c['b_median_delta']}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cross-finger rejection test: N presses of enrolled finger A, "
        "N presses of another finger B, one session run per press.",
        epilog="Exit status: 0 PASS, 1 FAIL, 2 INCONCLUSIVE or error.",
    )
    p.add_argument("-n", "--presses", type=int, default=50, help="presses per finger (default 50)")
    p.add_argument("--phase", choices=("pre-reboot", "post-reboot", "single"), default="single",
                   help="tag the run; post-reboot compares with the newest pre-reboot file")
    p.add_argument("--compare", type=Path, help="pre-reboot .jsonl to compare with (post-reboot)")
    p.add_argument("--order", choices=("blocks", "alternate"), default="blocks",
                   help="all A then all B (default), or A,B,A,B,...")
    p.add_argument("--retries", type=int, default=3,
                   help="re-asks per press after no finger / empty / error (default 3)")
    p.add_argument("--enroll", action="store_true",
                   help="run one session enroll of finger A first")
    p.add_argument("--session", type=Path, help="session binary (default: same paths as the app)")
    p.add_argument("--firmware", default=FIRMWARE, help=f"firmware dir (default {FIRMWARE})")
    p.add_argument("--session-arg", action="append", default=[], metavar="ARG",
                   help="extra session argument placed before the mode (repeatable)")
    p.add_argument("--mode", default="auth1",
                   help="session match mode (default auth1, one press per run; with 'auth' "
                   "only the first press of each run is used)")
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                   help=f"output directory (default {DEFAULT_LOG_DIR})")
    p.add_argument("--analyze", type=Path, metavar="JSONL",
                   help="only analyze an existing results file; no presses")
    g = p.add_argument_group("verdict thresholds")
    g.add_argument("--max-b-hits", type=int, help="allowed B hits (default max(1, round(N_B/50)))")
    g.add_argument("--score-direction", choices=("distance", "similarity"), default="distance",
                   help="distance: lower score = closer (coach default); similarity: higher = closer")
    g.add_argument("--margin", type=float, default=0.0,
                   help="median B must be worse than median A by more than this (default 0)")
    g.add_argument("--min-presses", type=int, default=10,
                   help="scored presses needed per finger for a verdict (default 10)")
    g.add_argument("--min-a-hit-rate", type=float, default=0.5,
                   help="A hit rate needed for a verdict (default 0.5)")
    g.add_argument("--no-score-check", action="store_true", help="judge on hits only")
    g.add_argument("--no-best-b-check", action="store_true",
                   help="skip the closest-B-press vs median-A check")
    g.add_argument("--keep-zero-scores", action="store_true",
                   help="count distance 0 (coach: no features) as a score")
    return p


def thresholds_from(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        direction=args.score_direction,
        margin=args.margin,
        max_b_hits=args.max_b_hits,
        min_presses=args.min_presses,
        min_a_hit_rate=args.min_a_hit_rate,
        score_check=not args.no_score_check,
        best_b_check=not args.no_best_b_check,
        keep_zero=args.keep_zero_scores,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    th = thresholds_from(args)
    if args.presses < 1:
        print("--presses must be at least 1", file=sys.stderr)
        return 2

    if args.analyze:
        records = read_records(args.analyze)
        summary = analyze(records, th)
        print(format_summary(summary))
        return exit_code(summary["verdict"])

    binary = args.session or session_bin()
    if binary is None or not Path(binary).is_file():
        print("session binary not found (try --session PATH)", file=sys.stderr)
        return 2
    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = args.log_dir / f"crossfinger-{stamp}-{args.phase}"
    raw_path = base.with_name(base.name + "-session.txt")
    compare_file = None
    if args.phase == "post-reboot":
        compare_file = args.compare or latest_pre_reboot(args.log_dir)
        if compare_file is None:
            print("no pre-reboot results found; run --phase pre-reboot first or pass --compare",
                  file=sys.stderr)
            return 2

    with raw_path.open("w", encoding="utf-8") as raw:
        def echo(line: str) -> None:
            s = line.strip()
            if _AUTH_ARM.match(s):
                print("    >>> PRESS now, hold about a second", flush=True)
            elif _AUTH_DOWN.match(s):
                print("    ... hold", flush=True)
            elif _AUTH_HIT.match(s) or _AUTH_FAIL.match(s) or _AUTH_NO.match(s) or _AUTH_EMPTY.match(s):
                print("    <<< LIFT", flush=True)
            elif s.startswith("unknown mode"):
                print(f"    session: {s} (older session binary? try --mode auth)", flush=True)
            elif _EXIT.match(s) and not s.endswith(":0"):
                print(f"    session: {s}", flush=True)

        def echo_enroll(line: str) -> None:
            if line.startswith(("wait finger", "rem=", "enroll done", "PRESS REJECT", "no finger")):
                print("    " + line.rstrip(), flush=True)

        if args.enroll:
            print("Enrolling finger A: press when 'wait finger' shows, lift after each 'rem='.")
            raw.write("=== enroll ===\n")
            rc, _ = run_session(session_argv(binary, "enroll", args.firmware, args.session_arg, 1500),
                                raw, echo_enroll)
            if rc != 0:
                print(f"enroll failed (exit {rc}); see {raw_path}", file=sys.stderr)
                return 2

        def run_one(label: str, idx: int, attempt: int) -> list[Press]:
            raw.write(f"=== {label} {idx} attempt {attempt} ===\n")
            _, lines = run_session(session_argv(binary, args.mode, args.firmware, args.session_arg, 400),
                                   raw, echo)
            return parse_presses(lines)

        try:
            records = run_test(args.presses, args.phase, args.order, args.retries, th, run_one,
                               lambda m: print(m, flush=True))
        except KeyboardInterrupt:
            print("\ninterrupted; nothing written", file=sys.stderr)
            return 2

    jsonl = base.with_suffix(".jsonl")
    csv_path = base.with_suffix(".csv")
    write_records(records, jsonl, csv_path)
    summary = analyze(records, th)
    summary["phase"] = args.phase
    summary["files"] = {"jsonl": str(jsonl), "csv": str(csv_path), "session": str(raw_path)}
    if compare_file is not None:
        pre = analyze(read_records(Path(compare_file)), th)
        summary["compare"] = compare(pre, summary)
        summary["compare_file"] = str(compare_file)
    summary_path = base.with_name(base.name + "-summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print()
    print(format_summary(summary))
    print(f"results: {jsonl}\n         {csv_path}\n         {summary_path}")
    return exit_code(summary["verdict"])


if __name__ == "__main__":
    raise SystemExit(main())
