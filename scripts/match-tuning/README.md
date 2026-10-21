# A/B test: session A (live) vs B (match-tuning)

| | file | sha256 | source |
|---|---|---|---|
| A | `fp5-qtee-session.A` | `fb4ee59b867ce41635f1dac9a5ccc0ac3c39b7ab96e007f2bd71f1d27db85b83` | main 076720f (byte-identical rebuild of the live binary) |
| B | `fp5-qtee-session.B` | `09b9f0f14e8fe0ddee6f760bbe0c833775cbd8f525501d77e8c9cc34a0596670` | match-tuning 916e6d7, built on the phone (Alpine gcc 15.2) |

B differs from A only in: rescan within 1000 ms of finger-down (max 3
scans, as before), no "skip falling" stop, and TIME/TUNE log lines. No
template, calibration or storage write was added. The watcher and its
lockout/PIN rules are unchanged.

Scripts (run on the phone as `user`, from this directory):

- `./deploy-B.sh`  checks both hashes, refuses if the live binary is not
  A (saves it), takes `/tmp/fp5-qtee-phone.lock`, swaps by rename,
  restarts the watcher.
- `./restore-A.sh` puts A back the same way.
- `./warm-on.sh` / `./warm-off.sh` optional step C (watcher env drop-in).

## Protocol (about 40 minutes)

Use the same enrolled finger(s) for both variants, dry hands, and press
the way you normally do. Enter the PIN whenever the phone asks for it
(after 5 misses there is a 30 s lockout; after 20 the PIN). Count only
presses where the finger really touched the sensor; the logs do the
counting, you only need to note the block start and end times.

1. Block A1 (A is live now): 20 presses with the screen on and locked
   (lift fully between presses, wait for unlock or buzz, re-lock with
   the power key + wait for the lock screen), then 10 wake presses
   (screen off for > 10 s, then press and hold the sensor until it
   unlocks or buzzes).
2. `./deploy-B.sh`, then block B1: the same 20 + 10.
3. Block B2: the same 20 + 10 again.
4. `./restore-A.sh`, then block A2: the same 20 + 10.
5. Optional C (only if B is not worse): `./deploy-B.sh`, `./warm-on.sh`,
   block C1: the same 20 + 10. Undo with `./warm-off.sh` (and
   `./restore-A.sh` if you want A back).

ABBA order cancels drift (finger moisture, practice).

## Read the result (desk)

    mkdir -p /tmp/ab && ssh fp5-pmos 'cd ~/fp5-qtee-keep && tar -czf - logs' | tar -xzf - -C /tmp/ab
    tools/fp5_unlock_stats.py /tmp/ab/logs --since 2026-10-22T00:00

"by variant" separates A (no TUNE line) from B (TUNE scans=3
rescan_ms=1000 skip_falling=0). Compare hit rate, hits on scan 2/3,
wake hits, and for B the median down->result ms (TIME lines; A only has
the tick-based scan period).

Expected: B's hit rate equal to A or a few points higher (more scored
rescans on presses that A cut as "falling"); a hit on scan 1 equally
fast; B's TIME lines show where the ~200 ms per scan goes. C should cut
the wake press from ~2 s to roughly the scan time, and remove the
reload after a miss. Stop and restore A if B gives a false accept (an
unlock by a finger that is not enrolled) or the watcher stops arming.
