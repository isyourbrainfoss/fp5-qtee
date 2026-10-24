# Fingerprint lockout on D-Bus

Branch `fprint-lockout` (on `fprint-integration` 780e017). Prepared
2026-10-24, **not deployed, not run on the phone**.

## Where lockout lives today

* Only the unlock watcher (`app/fp5_qtee_unlock.py`, `UnlockPolicy`) has a
  lockout, Android style: 5 rejected fingers in a row -> 30 s, 20 -> until
  the PIN. Only a scored non-match (`AUTH FAIL ... itype=0x2`) counts. The
  count is kept in `$XDG_RUNTIME_DIR/fp5-qtee-lockout` as
  `<failed> <until>` (`until` in CLOCK_BOOTTIME), next to the PIN marker
  `fp5-qtee-pin-ok`. Fail closed: unreadable/garbled file, negative count,
  or a missing file while the PIN marker exists = until the PIN. A PIN
  unlock (or a finger hit) rewrites it with 0.
* The session binary (`session/`) has no lockout; the trustlet did not
  show one in our logs.
* `fp5-fprintd` had none: a verify for sudo/polkit while the watcher is
  locked out still matched.
* The watcher told the user with a non-transient notification ("Too many
  attempts..."), so it stayed in the notification list long after the
  lockout ended.

## What this branch adds

`fp5-fprintd`, on `/net/reactivated/Fprint/Device/0`, interface
`org.fp5.Fprint.Manage` (our extension; `net.reactivated.Fprint.Device` is
unchanged, so pam_fprintd / GNOME Settings see exactly the fprintd API):

| Property | Type | Meaning |
|---|---|---|
| `locked-out` | b | the watcher would refuse a finger now because of strikes |
| `lockout-permanent` | b | until the PIN (20 strikes, or fail-closed) |
| `lockout-remaining` | u | seconds left of a timed lockout (0 if none/permanent); computed at `Get` time |
| `failed-attempts` | u | strikes in a row |
| `lockout-reason` | s | `""`, `lockout`, `lockout-permanent` (same words as the watcher's `why_not`) |

* `org.freedesktop.DBus.Properties.PropertiesChanged(org.fp5.Fprint.Manage, ...)`
  and a custom signal `org.fp5.Fprint.Manage.LockoutChanged(b locked_out,
  u remaining, s reason)` when locked/permanent/count/reason change
  (file polled every 2 s; the countdown itself is not signalled, read
  `lockout-remaining` or count down locally).
* Opt-in `FP5_FPRINTD_HONOR_LOCKOUT=1`: `VerifyStart` during a lockout
  does not open the sensor and emits
  `VerifyStatus("verify-disconnected", true)`. That is the fprintd status
  pam_fprintd and GNOME already handle as "reader not available": sudo /
  polkit fall back to the password. fprintd has no lockout status of its
  own, so this is the closest compatible answer. Default off (behaviour
  as before).
* The rules are a mirror of `UnlockPolicy`; `test_fp5_fprintd.py`
  `test_matches_watcher_policy` drives the real `UnlockPolicy` through 25
  misses and checks the daemon reads the same decision from the file.
* Fingerprints app: an `Adw.Banner` under the header bar, "Too many
  attempts. Fingerprint unlock is paused for N s." with a local countdown,
  or "...Unlock with your PIN once to use your finger again." Hidden on a
  stock fprintd (no such interface). A verify ended by
  `verify-disconnected` during a lockout shows the lockout text.
* Watcher (optional): lockout notices are sent transient, so they show
  for their 6 s on the lock screen but are not kept in the list.

Not done: the daemon's own verify misses (sudo) do not add strikes to the
watcher's count (the daemon would have to write the user's tmpfs file).

## Paths and sandbox

The daemon runs as root and reads `/run/user/<uid of FP5_FPRINTD_USER,
default user>/fp5-qtee-lockout`. Override with `FP5_FPRINTD_LOCKOUT_FILE`
(and `FP5_FPRINTD_PIN_MARKER`). If the unit had `ProtectHome=yes`,
`/run/user` would be hidden and the state reads as "none"; the daemon
logs `lockout file ... (dir visible: ...)` at start and `deploy.sh`
warns.

## Deploy note (not done)

Kit on the phone, e.g. `~/fp5-qtee-keep/fprint-lockout-YYYYMMDD/`: copy
`scripts/fprint-lockout/*`, `fprintd/fp5_fprintd.py`,
`fprintd/ui/fp5_fingerprints.py` and (for `--watcher`)
`app/fp5_qtee_unlock.py` flat into it. Screen unlocked:

    sh deploy.sh                  # daemon + app banner (read-only state)
    sh deploy.sh --honor          # + sudo/polkit refuse the finger during a lockout
    sh deploy.sh --watcher        # + transient lockout notices (restarts the watcher)
    sh restore.sh                 # everything back (saved in orig/ on first deploy)

Needs `fprint-integration` deployed first (the daemon it replaces is
that one; `orig/` keeps it). No reboot, no storage, PAM or sensor change.
Restarting fp5-fprintd makes PAM fall back to the password for ~2 s.

## Phone test

1. `busctl --system get-property net.reactivated.Fprint /net/reactivated/Fprint/Device/0 org.fp5.Fprint.Manage locked-out lockout-reason`
   -> `b false`, `s ""`.
2. Over SSH (after the battery run), `busctl --system monitor net.reactivated.Fprint`.
   Lock the phone, press a wrong finger 5 times: `LockoutChanged true 30 lockout`
   (and the lockout notice on the lock screen). After 30 s:
   `LockoutChanged false 0 ""` (count stays 5). Unlock with the PIN:
   count back to 0 (`failed-attempts` 0). The app can only be opened
   unlocked, and a PIN unlock clears the lockout, so to see the banner use
   the file trick in step 4.
3. With `--watcher`: the "Too many attempts" notice is gone from the
   list after it is shown.
4. With `--honor`: hard to see, because a PIN unlock clears the lockout
   and fprintd verify only runs while unlocked. Test by writing the file
   by hand while unlocked: `echo "20 0" > $XDG_RUNTIME_DIR/fp5-qtee-lockout`,
   open Fingerprints (banner says use PIN), `sudo -k; sudo true` asks for
   the password without a finger prompt; then
   `echo "0 0" > $XDG_RUNTIME_DIR/fp5-qtee-lockout` (or lock + PIN).
   The banner part works without `--honor` too. The watcher reads this
   file only at its start, so the trick does not change its own decision;
   if it restarts while the file says 20 it wants the PIN once (harmless).

## Tests (desk)

    cd fprintd && python3 -m pytest -q test_fp5_fprintd.py         # + 3 lockout tests
    cd fprintd && dbus-run-session -- python3 -m pytest -q test_fp5_fprintd_dbus.py   # + test_7_lockout
    cd fprintd/ui && dbus-run-session -- python3 -m pytest -q test_ui.py              # + banner test
    cd app && python3 -m pytest -q test_fp5_qtee_unlock.py         # + transient notice test
