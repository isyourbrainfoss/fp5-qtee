# Fairphone 5 — fingerprint over QTEE

Userspace session for the signed `focal32` trustlet on Fairphone 5
(Qualcomm SM7325 / sc7280, postmarketOS). A small Finger app enrolls and
matches through `/dev/tee0`. It does not load `qsee_fingerpr` and it does
not unlock Phosh.

The older in-kernel Register path is
[fp5-fingerprint](https://github.com/isyourbrainfoss/fp5-fingerprint).

Status: **experimental**. Not an official Fairphone or distribution package.

## Where this stands

Enroll can reach samples-remaining 0, and the same trustlet can reopen that
template on the same boot. Match sends authenticate first, then arms chip
wait-touch (work mode 1). It captures only an exact finger-down
(`itype` `0x2`, not an ESD or leftover). A hit is an image report that
succeeds with a real template id. Authenticate-command success, report
failure, fid 0, and the poison ids are misses.

Sync config sends `preferred_device_id` 37777, which is focal32 profile
`0x9391` (the Android config). The detected id 37841 is not in that profile
table, so a template enrolled under 37841 has no feature pointers. Enroll
again on this session before matching. `algorithm_log_level` is 2 so the
trustlet can print the subtemplate distance. A match of the enrolled finger
against a different finger has not been witnessed from this tree. See
[Cross-finger test](#cross-finger-test) to measure it.

The signed `focal32` image is not in this repository. Copy `focal32.mdt` and
`focal32.bXX` from a Fairphone Android vendor image to `/lib/firmware/qsee/`.

## Repository layout

```
session/   fp5-qtee-session, wire helpers, and their host tests
app/       Phosh "Finger" app (idle until Enroll or Match)
scripts/   load-qcomtee.sh for the test phone
```

## Kernel

`/dev/tee0` comes from an out-of-tree `qcomtee` module. The `7.2.0-nfc-test+`
kernel used here has `CONFIG_QCOMTEE` unset and `CONFIG_QCOM_QSEECOM=y`.
The module on the test phone also exposes `/dev/qsee_log` (avgv and interrupt
type). That module binary and its patch against mainline `drivers/tee/qcomtee`
are not in this tree.

`scripts/load-qcomtee.sh` refuses to load when `qsee_fingerpr` is already
loaded, and it is a no-op when `qcomtee` is already loaded. Do not `rmmod` a
live `qsee_fingerpr`. One listener registration per boot. Do not program or
erase the RPMB key.

## Build

On the phone, Alpine gcc is enough. pthread is in musl, so there is no
`-lpthread`. `logf` and `logl` warn against libm; leave those names.

```sh
make -C session
python3 app/test_fp5_qtee_coach.py
make -C session test
```

Install the session where the app already looks:

- `/home/user/fp5-qtee-keep/session/fp5-qtee-session`
- `/tmp/fp5-qtee/fp5-qtee-session` (lost on reboot)

The app is `app/fp5-qtee-app.py`, launched by `app/fp5-qtee-finger`.
It stays idle until Enroll or Match is tapped. Hold about a second on PRESS.

## Cross-finger test

`app/fp5_qtee_crossfinger.py` checks that a different finger is rejected.
Finger A is the enrolled finger, finger B any other finger. It runs the
session once per press in the new `auth1` mode (one auth per run, logs
`auth single <rc>`), the same way the app does (`sudo -n timeout ...
<session> auth1 /lib/firmware/qsee`). It prompts in the terminal: get ready,
`PRESS now` when the reader is armed, then `LIFT`. No finger, empty and error
runs are logged but not counted, and that press is asked for again.

```sh
# finger A already enrolled with the app (or add --enroll)
python3 app/fp5_qtee_crossfinger.py --phase pre-reboot      # 50 x A, then 50 x B
sudo reboot
python3 app/fp5_qtee_crossfinger.py --phase post-reboot     # compares with the newest pre-reboot run
python3 app/fp5_qtee_crossfinger.py --analyze ~/fp5-qtee-keep/logs/crossfinger-...-pre-reboot.jsonl
```

Each run writes `~/fp5-qtee-keep/logs/crossfinger-<stamp>-<phase>.jsonl` and
`.csv` (label, HIT/FAIL, scores, timestamp per press), `-summary.json`, and
the raw session output in `-session.txt`.

Verdict (exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE). Every threshold is an option:

- FAIL if B hits more than `max(1, round(N_B/50))` times (`--max-b-hits`).
- FAIL if B scores are not clearly worse than A scores. Each press is scored
  by its closest `FtVerifySubTemplate() score`. Scores are read as distances,
  lower is closer, as the coach reads them (`--score-direction similarity`
  flips that). Distance 0 means no features and is dropped
  (`--keep-zero-scores` keeps it). Median B must be above median A plus
  `--margin` (default 0), and the closest B press must be above median A
  (`--no-best-b-check` skips this).
- INCONCLUSIVE if either finger has fewer than `--min-presses` (10) scored
  presses, if A hits less than `--min-a-hit-rate` (50%) of the time (a reader
  that rejects everything also rejects B), or if there are no scores
  (`--no-score-check` judges on hits only).

`-n` sets the presses per finger, `--order alternate` interleaves A and B,
and `--session-arg ARG` passes extra session flags. Host tests, with
synthetic log lines only: `python3 app/test_fp5_qtee_crossfinger.py`.

## Do not

- Load `qsee_fingerpr` on a boot where `qcomtee` is loaded. The old
  FP Fingerprint icon starts that module.
- Treat authenticate-command rc 0 as a finger match.
- Unlink the secure-storage segment. A real unlink destroys it.
- Program or erase the RPMB key (`req_resp=1`, listener command `0x101`).

## License

GPL-2.0-only for the C session. See `LICENSE`.
