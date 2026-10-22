# fprintd on the FP5 (net.reactivated.Fprint over the QTEE session)

## Route chosen

Two ways to give the rest of the system (pam_fprintd for sudo/polkit,
GNOME Settings) the fingerprint:

1. A libfprint driver that talks to the trustlet, run by real fprintd.
   Marcus's `focaltechqsee` driver exists but needs his QSEECOM driver,
   ffsupplicant, his RPMB listener and his storage group path. Porting it
   to this phone's qcomtee session means moving the whole session (TEE
   transport, RPMB/FS listener threads, calibration, learning) into a
   libfprint fork, and rebuilding libfprint + fprintd for pmOS. Big, and
   it would be a second owner of the sensor next to the unlock watcher.
2. A small daemon that owns `net.reactivated.Fprint` and runs the same
   `fp5-qtee-session` binary the watcher uses (what Catcrafts'
   fingerprintd does on the FP6).

Route 2 is used: `fprintd/fp5_fprintd.py`. Same bus name, object paths,
methods, signals and error names as fprintd, so the stock pam_fprintd and
GNOME Settings work unchanged.

## One owner of the sensor

- Screen locked: the unlock watcher owns the sensor (warm serve
  session, touch-to-wake). The daemon does not open the trustlet; Verify,
  Enroll and Delete fail at once with `AlreadyInUse`, so PAM goes straight
  to the password. The Phosh lock screen keeps using the watcher (Phosh
  has no fprintd support; its PAM stack runs only when the PIN is
  submitted).
- Unlocked: the watcher has quit its session. The daemon takes
  `/tmp/fp5-qtee-phone.lock`, refuses if any other `fp5-qtee-session`
  runs, starts `fp5-qtee-session serve` on Claim/VerifyStart and quits it
  on Release, on the client leaving the bus, or after 60 s idle.
- If the seat locks while the daemon holds the sensor, it ends the
  operation (`verify-disconnected`) and quits within ~2 s; the watcher
  retries every 5 s and takes over.

## Fingers and names

The trustlet stores fids only. `/var/lib/fp5-qtee/fingers.json` maps
`fid -> {user, finger}`. Seed: 1558157930 = right-index-finger,
1979259351 = right-thumb (a guess; edit the file to rename, then
`systemctl restart fp5-fprintd`). Enroll writes the new fid with the name
GNOME asked for. A verify hit on a fid owned by another user is a
no-match.

## Calls

| call | what happens |
|---|---|
| ListEnrolledFingers | names from fingers.json (no trustlet) |
| Claim / Release | caller must be root or the user (polkit `verify`) |
| VerifyStart / VerifyStop | serve session, `auth`, AUTH HIT → verify-match; scored miss → verify-no-match; partial → verify-retry-scan; no finger → re-arm |
| EnrollStart / EnrollStop | storage backup, then `fp5-qtee-session enroll`; rem=N → enroll-stage-passed; reject → retry-scan; done rem=0 wrote=1 → enroll-completed |
| DeleteEnrolledFinger(s)(2) | storage backup, then `remove <fid> --yes-really`; the last finger is refused unless FP5_FPRINTD_ALLOW_LAST=1 |

Template learning after a hit happens inside the session, as in the
watcher. Logs: `/var/lib/fp5-qtee/logs/`, `journalctl -u fp5-fprintd`.

## Deploy, test, restore

Kit: `scripts/fprintd/` + `fprintd/` files copied to
`~/fp5-qtee-keep/fprintd-20261022/` on the phone.

    ./deploy.sh      # storage backup, drop-in for fp5-fprintd.service,
                     # /etc/pam.d/fp5-fprint-test, fp5-pam-test
    ./restore.sh     # back to the previous fp5-fprintd

Test without touching sudo (screen unlocked, in a terminal on the phone):

    fp5-pam-test                 # "Place your finger..." touch -> rc=0
    fp5-pam-test fp5-fprint-test # same; a miss falls back to the password

Note (found 2026-10-22): `/etc/pam.d/sudo`, `polkit-1` and `phosh` on
the phone already carry `auth sufficient pam_fprintd.so max-tries=1
timeout=8` from the September install (fp5-fingerprint repo), with the
password lines after it. With the old stub those always failed at once;
with this daemon sudo/polkit really ask for a finger while unlocked (8 s,
then the password). `sudo -n` (NOPASSWD) is not affected. Phosh's stack
runs only on PIN submit while locked, where the daemon refuses at once,
so the PIN is not delayed. Those files were not changed by this kit.

Tests: `python3 -m unittest fprintd/test_fp5_fprintd.py` and
`dbus-run-session -- python3 -m unittest fprintd/test_fp5_fprintd_dbus.py`
(fake session binary in `fprintd/testdata/`).
