#!/usr/bin/env python3
"""Hit rate and latency from fp5-qtee session logs.

Reads finger-unlock-*.txt, finger-serve-*.txt and finger-auth-*.txt from a
copy of ~/fp5-qtee-keep/logs (read-only; it never touches the phone).

    tools/fp5_unlock_stats.py LOGDIR [--since 2026-10-21T18:00] [--tz-hours 2]

A press is one "AUTH <n> REAL DOWN". Its result is the next AUTH HIT,
AUTH FAIL (itype 0x2), AUTH <n> empty or AUTH <n> cancelled. Hit rate is
HIT / (HIT + FAIL), the same ratio as counting the two line types.

Timing comes from two clocks:
  * TA ticks: the [hex] stamp on qsee lines, a 19.2 MHz QTimer. It gives
    the report span and the burst-to-burst period on every build.
  * TIME lines (match-tuning builds and later): monotonic ms in the
    session, so finger-down -> AUTH HIT is measured directly.
Files are grouped by "variant": the TUNE line if present (B builds),
otherwise "A(no TUNE)". File names carry the phone clock (UTC).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import re
import statistics
import sys
from pathlib import Path

TICK_HZ = 19.2e6

TICK = re.compile(r"\[([0-9a-f]{8,12})\]")
R_DOWN = re.compile(r"^AUTH (\d+) REAL DOWN")
R_ALREADY = re.compile(r"^AUTH (\d+) (already down|held at panel)")
R_BURST = re.compile(r"^AUTH (\d+) burst avgv=(\d+),(\d+),(\d+)")
R_FALL = re.compile(r"^AUTH (\d+) skip falling")
R_HIT = re.compile(r"^AUTH HIT (\d+) .*fid=(\d+)")
R_FAIL = re.compile(r"^AUTH FAIL (\d+) itype=0x2")
R_EMPTY = re.compile(r"^AUTH (\d+) empty")
R_CANCEL = re.compile(r"^AUTH (\d+) cancelled")
R_SCORE = re.compile(r"score = (-?\d+), matchCnts = (\d+)")
R_QUAL = re.compile(r"image quality = (\d+), coverage = (\d+)")
R_AUTHED = re.compile(r"authenticated fid = (\d+)")
R_CAP = re.compile(r"^CAP cmd=0x1013 rc=(-?\d+) .* avgv=(\d+)")
R_REPORT = re.compile(r"^REPORT_EV5 cmd=0x1017 rc=(-?\d+)")
R_TUNE = re.compile(r"^TUNE (.*)$")
R_TIME = re.compile(r"^TIME (\d+) (\S+) (-?\d+)(?: (.*))?$")
R_NAME = re.compile(r"finger-(\w+)-(\d{8})-(\d{6})\.txt$")


def parse_file(path: Path) -> dict | None:
    m = R_NAME.search(path.name)
    if not m:
        return None
    kind = m.group(1)
    t0 = dt.datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S")
    presses: list[dict] = []
    cur: dict | None = None
    rep: dict | None = None
    caps: list[int] = []
    burst_av: list[int] | None = None
    already: set[int] = set()
    tune = None
    times: dict[int, dict[str, list[int]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    with path.open(errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            tm = TICK.search(line)
            tk = int(tm.group(1), 16) if tm else None
            mm = R_TUNE.match(line)
            if mm:
                tune = mm.group(1)
                continue
            mm = R_TIME.match(line)
            if mm:
                times[int(mm.group(1))][mm.group(2)].append(int(mm.group(3)))
                continue
            mm = R_ALREADY.match(line)
            if mm:
                already.add(int(mm.group(1)))
            mm = R_DOWN.match(line)
            if mm:
                n = int(mm.group(1))
                cur = dict(n=n, bursts=[], result=None, fid=None,
                           wake=n in already, falling=False)
                presses.append(cur)
                rep, caps, burst_av = None, [], None
                continue
            if cur is None:
                continue
            mm = R_CAP.match(line)
            if mm:
                caps.append(int(mm.group(2)))
                continue
            mm = R_BURST.match(line)
            if mm:
                burst_av = [int(mm.group(i)) for i in (2, 3, 4)]
                continue
            if R_FALL.match(line):
                cur["falling"] = True
                continue
            mm = R_REPORT.match(line)
            if mm:
                rep = dict(avgv=burst_av or caps[-3:], scores=[], q=None,
                           cov=None, t_first=None, t_last=None, authed=None,
                           rc=int(mm.group(1)))
                cur["bursts"].append(rep)
                caps, burst_av = [], None
                continue
            if rep is not None and line.startswith("REPORT_EV5 qsee:"):
                if tk is not None:
                    if rep["t_first"] is None:
                        rep["t_first"] = tk
                    rep["t_last"] = tk
                rep["scores"] += [(int(a), int(b)) for a, b in R_SCORE.findall(line)]
                q = R_QUAL.search(line)
                if q:
                    rep["q"], rep["cov"] = int(q.group(1)), int(q.group(2))
                q = R_AUTHED.search(line)
                if q:
                    rep["authed"] = int(q.group(1))
                continue
            mm = R_HIT.match(line)
            if mm:
                cur["result"], cur["fid"] = "hit", int(mm.group(2))
                cur = None
                continue
            for rx, name in ((R_FAIL, "fail"), (R_EMPTY, "empty"),
                             (R_CANCEL, "cancel")):
                if rx.match(line):
                    cur["result"] = name
                    cur = None
                    break
    for p in presses:
        t = times.get(p["n"], {})
        if t.get("down"):
            end = (t.get("hit") or t.get("fail") or t.get("empty") or [None])[-1]
            p["down_to_end_ms"] = None if end is None else end - t["down"][-1]
            p["scan_ms"] = [b - a for a, b in zip(t["down"][-1:] + t.get("report", []),
                                                  t.get("report", []))]
    return dict(file=path.name, kind=kind, t0=t0, presses=presses,
                variant=f"B({tune})" if tune else "A(no TUNE)")


def pct(h: int, f: int) -> str:
    return f"{h}/{h + f} = {100.0 * h / (h + f):.0f}%" if h + f else "-"


def med(v: list[float]) -> str:
    return f"{statistics.median(v):.0f}" if v else "-"


def summarize(files: list[dict], key) -> list[str]:
    groups: dict = collections.OrderedDict()
    for r in files:
        for p in r["presses"]:
            groups.setdefault(key(r), []).append(p)
    out = ["group | presses | HIT | FAIL | empty | hit rate | hits on scan 1/2/3+ "
           "| wake hit/total | down->result ms (median, TIME) | scan period ms (median, ticks)"]
    for k, ps in groups.items():
        c = collections.Counter(p["result"] for p in ps)
        hb = collections.Counter(min(len(p["bursts"]), 3) for p in ps if p["result"] == "hit")
        wk = [p for p in ps if p["wake"]]
        lat = [p["down_to_end_ms"] for p in ps if p.get("down_to_end_ms") is not None
               and p["result"] == "hit"]
        gaps = []
        for p in ps:
            prev = None
            for b in p["bursts"]:
                if prev and prev["t_first"] and b["t_first"] and b["t_first"] > prev["t_first"]:
                    gaps.append((b["t_first"] - prev["t_first"]) / TICK_HZ * 1000)
                prev = b
        out.append(f"{k} | {len(ps)} | {c['hit']} | {c['fail']} | {c['empty']} | "
                   f"{pct(c['hit'], c['fail'])} | {hb[1]}/{hb[2]}/{hb[3]} | "
                   f"{sum(1 for p in wk if p['result'] == 'hit')}/{len(wk)} | "
                   f"{med(lat)} | {med(gaps)}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logdir", type=Path)
    ap.add_argument("--since", help="UTC ISO time; older files are skipped")
    ap.add_argument("--tz-hours", type=float, default=2.0,
                    help="offset added to the phone's UTC for the day column")
    a = ap.parse_args(argv)
    since = dt.datetime.fromisoformat(a.since) if a.since else None
    files = []
    for p in sorted(a.logdir.glob("finger-*-2*.txt")):
        r = parse_file(p)
        if r and (since is None or r["t0"] >= since):
            files.append(r)
    off = dt.timedelta(hours=a.tz_hours)
    print("## by local day and kind")
    print("\n".join(summarize(files, lambda r: ((r["t0"] + off).strftime("%m-%d"), r["kind"]))))
    print("\n## by variant and kind")
    print("\n".join(summarize(files, lambda r: (r["variant"], r["kind"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
