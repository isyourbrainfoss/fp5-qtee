# integration-20261024

Branch `integration-20261024`, cut from `main` 076720f on 2026-10-24.
Not merged into `main` (that needs the owner's OK). Nothing here was
deployed to the phone by the merge itself; every piece keeps its own kit
and restore script.

## What is in it

| Branch | Tip | What | Phone status |
|---|---|---|---|
| `match-tuning` | f47b369 | Android-HAL rescan + per-step timing (916e6d7), A/B kit, `finger-manage` (read-only `fp5-qtee list`, gated remove), template learning + warm mode (e154956, f47b369) | Deployed and used since 2026-10-21 (template learning, warm mode). `remove` never run on the phone. |
| `fp-wake` | 772bb57 | Touch-to-wake: `focaltech_fp_life` 0.8-wake (FP IRQ as wakeup source), watcher arms before sleep, screen on after a dark hit | Phone-tested (owner). `docs/fp-wake.md` records the armed suspend/resume via RTC; a touch while suspended is listed there as not verified by the agent. |
| `fprintd` | bfa5540 (9a11ab8) | `fp5-fprintd`: Python `net.reactivated.Fprint` stand-in on top of `fp5-qtee-session`; unknown lock state counts as locked | Phone-tested (owner), kit `fprintd-20261022`. |
| `resuspend` | a45d135 | gsd-power patch: reset idle mode on resume (phone re-suspends after an unattended wake) + build/deploy/restore | Deployed 2026-10-23, tested with RTC wakes. |
| `fprint-integration` | 780e017 | GTK4 Fingerprints app, `org.fp5.Fprint.Manage` (`ListDetailed`, `RelabelFinger`), GNOME Settings `org.gnome.login-screen` schema shim | Deployed 2026-10-23; list + Settings "Fingerprint Login" seen. **Not user-tested: app rename / enroll / delete, and sudo via `pam_fprintd`.** |
| `cell-backoff` | 5eac7d5 (4227de9) | Suspend-safe mobile-data retry backoff (NM/imsd/resume hook, pre-sleep cancel); 5eac7d5 adds the idle-suspend re-arm | 4227de9 deployed 2026-10-24, **only a ~45 min test**. **5eac7d5 (re-arm) not deployed.** |

## Order and how the branches relate

`git log --graph` shows the fingerprint work is one linear stack on `main`:

    main 076720f
      916e6d7, bbd3282                (match-tuning A/B)
      e934145  merge finger-manage    (b89a810, cb39621, b5b6813)
      e154956, f47b369                = match-tuning
      772bb57                         = fp-wake
      9a11ab8, bfa5540                = fprintd
      a45d135                         = resuspend
      780e017                         = fprint-integration
    cell-backoff 4227de9: one commit directly on main 076720f

So the merge is:

1. `git merge --ff-only fprint-integration` (fast-forward; brings
   match-tuning, finger-manage, fp-wake, fprintd and resuspend with it, in
   their original order and with their original commit ids).
2. `git merge --no-ff cell-backoff` (8ebdbab), and again after the re-arm commit 5eac7d5 (no conflicts).

## Conflicts

None. `cell-backoff` only adds new files (`docs/cell-backoff.md`,
`scripts/cell-backoff/*`); the stack never touches them. The stack
itself was already linear. No file needed a hand resolution.

Checked by hand for coherence (not textual conflicts):

* Every kit writes to its own place: `learn` -> session binary +
  `warm.conf`; `fp-wake` -> module + `zz-fp-wake.conf` (sorts last, so its
  `ExecStart` wins and it also sets `FP5_QTEE_WARM=1`); `fprintd` ->
  `/usr/local/lib/fp5-qtee/fp5_fprintd.py`, drop-in, polkit policy, test PAM
  service; `fprint-integration` -> overwrites the same `fp5_fprintd.py`
  (saves the `fprintd` one in its `orig/`), schema shim, app;
  `resuspend` -> `/usr/libexec/gsd-power`; `cell-backoff` ->
  `/usr/local/bin/fp5-telephony-resume.sh`, `fp5-cell-*`, a system-sleep
  hook and an imsd drop-in. No two kits install the same file except the
  intended `fprintd` -> `fprint-integration` daemon upgrade.
* `app/` and `session/` are unchanged after 772bb57 (fp-wake), so the
  watcher the fp-wake drop-in runs and the live session binary are the
  same code as in this branch.
* Two system-sleep paths now exist: the watcher's logind delay lock
  (fp-wake, arms the sensor) and `fp5-cell-presleep` (systemd-sleep hook,
  `timeout 5`). They do not share state.

## Restore, per branch (on the phone, newest first)

Undo in reverse deploy order. Each script only reverts its own branch.

| Branch | Restore |
|---|---|
| cell-backoff | `sh ~/fp5-qtee-keep/cell-backoff-20261024/restore.sh` (also stops a pending `fp5-cell-rearm`); re-arm alone: `sudo touch /etc/fp5-cell-backoff.no-rearm` |
| fprint-integration | `sh ~/fp5-qtee-keep/fprint-integration-20261023/restore.sh` (old `fp5_fprintd.py` back, shim + app removed) |
| resuspend | `sh ~/fp5-qtee-keep/resuspend-20261023/restore.sh` (stock gsd-power back) |
| fprintd | `sh ~/fp5-qtee-keep/fprintd-20261022/restore.sh` (old stub daemon; storage backup path printed) |
| fp-wake | `scripts/fp-wake/restore.sh` from its kit dir (module 0.7 e9579dca, `zz-fp-wake.conf` removed) |
| match-tuning / learn | `~/fp5-qtee-keep/learn-20261021/restore.sh` (binary A fb4ee59b, `warm.conf` off; `--storage <backup>` only if templates are lost); `scripts/match-tuning/restore-A.sh` for the older A/B kit |

## Tests run for this branch (desk, 2026-10-24)

* `make -C session` - builds (two pre-existing `-Wbuiltin-declaration-mismatch`
  warnings for the local `logl`/`logf` helpers); `make -C session test` - 4/4 ok.
  `fp5-qtee-load` (no Makefile target) links with all `fp5_*.c`.
* `python3 -m compileall app fprintd tools` - ok.
* `scripts/cell-backoff/test_fp5_cell_backoff.py`: 21 passed (bash; also busybox ash and dash off-desk).
* pytest: app 159 passed (boot 4, coach 13, haptic 19, unlock 123);
  fprintd 9 + D-Bus flow 7 passed; UI flow 5 passed; tools 3 passed.
* `ruff --select E9,F`: two pre-existing nits (unused `any_known` in
  `seat_locked`, unused `Gtk` import in `test_ui.py`). Left as is: the
  daemon is the deployed code.
* shellcheck 0.11 (`-S warning`, run off-desk on a `git archive` of this
  branch) over every sh/bash script in `scripts/`, `app/`, `kernel/`: no
  errors in real scripts. Only: SC2148/SC2034 in the sourced
  `scripts/{learn,fp-wake}/common.sh` (no shebang, vars used by the
  caller - expected), SC2034 unused loop var in
  `resuspend/restart-gsd-power.sh`, SC2046 unquoted `$(date +%s)` in
  `resuspend/wake-recorder.sh`, SC2024 in `app/fp5-qtee` (log redirect is
  meant to be the user's file). `scripts/cell-backoff/*` is clean.

## Risk / still to test on the phone

* cell-backoff for a full day (only ~45 min so far). The block-inhibitor /
  gsd-power risk (an idle suspend refused during an attempt is never retried
  by gsd-power 51) is addressed by 5eac7d5: a transient `fp5-cell-rearm`
  timer 20 s after each attempt suspends only if gsd-power would have by
  then (screen off, locked, idle >= gsd timeout, action suspend, no call, no
  other sleep block inhibitor, no suspend meanwhile). See
  `docs/cell-backoff.md`. Needs a phone test; kill switch
  `/etc/fp5-cell-backoff.no-rearm`.
* Fingerprints app: rename, enroll, delete (with a finger).
* sudo / polkit via `pam_fprintd` while unlocked with the new daemon.
