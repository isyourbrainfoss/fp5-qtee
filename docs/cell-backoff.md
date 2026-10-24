# Suspend-safe mobile-data retries (fp5-cell-backoff)

## Problem (2026-10-23/24)
The phone woke from suspend after 1-31 s, roughly every 6 min, all day and night:
- 12 h day run: 70 suspends, asleep only 4.9 h; 53 of 55 short sleeps started with a data connect in flight.
- 6 h night run (21:25-03:25 UTC): 31 suspends, asleep 2.8 h; all 21 short sleeps had a connect in flight.
- The first IRQ after resume was the modem link (ipcc -> glink-smem irq 254, MPSS) on every traced wake.

Mobile data is refused by the network: modem on GSM, PS detached, reject cause 7
`gprs-not-allowed`, QMI system info shows PS "call barring: all-calls" and "SIM reject info".
The SIM is a prepaid card that is probably out of balance (network side). The same symptoms
were seen on the lab's other prepaid SIM in August when its credit ran out. Config is fine:
APN `telenor.smart` (default profile 1, initial EPS bearer, ipv4v6) is the one that gave
working LTE data + ping on 2026-08-08/09; the ims profile (2) is separate.

Three things retried endlessly while awake: NetworkManager autoconnect of "Telenor smart"
(autoconnect-retries -1), imsd's `ims-pdn-up.sh` (14 min of tries, fail, restart every 2 min),
and `fp5-telephony-resume.sh` -> `fp5-lte-reattach.sh` (soft reattach + ModemManager restart on
every resume, run twice: sleep hook + service). A connect in flight at suspend gets its failure
reply from the modem later, which wakes the phone; it then stays up for the 300 s idle timeout.

## Fix
- `fp5-cell-backoff`: with packet service detached it turns NM autoconnect on qrtr0 off
  (runtime only), keeps imsd stopped, and makes one recovery attempt (radio off/on clears the
  modem's PS-invalid state after a cause-7 reject, register, wait for attach) with backoff
  15 min -> 30 -> 60 -> 120 -> 240 min (wall clock, sleep time counts; max 30 min while the screen
  is on). Attempts hold a sleep block inhibitor (max 150 s) and skip if a call exists.
  When attached: autoconnect on, data up, imsd started, backoff reset.
- `fp5-telephony-resume.sh`: calls `fp5-cell-backoff resume` instead of the reattach + MM
  restart (old behaviour with `FP5_RESUME_REATTACH=1`); runs once per resume (flock).
- `/usr/lib/systemd/system-sleep/fp5-cell-presleep`: before suspend, if detached: autoconnect off,
  cancel pending simple-connect, kill a running ims-pdn-up.sh.
- imsd drop-in `ExecCondition=fp5-cell-backoff ps-attached`: no ims bring-up loop without packet service.
- `fp5-cell-backoff.timer`: every 10 min while awake (attempts only when the screen is on or
  the backoff is 0).
Calls/SMS: CS registration is untouched except the ~20 s radio cycle inside an attempt.

## Idle suspend during an attempt (re-arm, 2026-10-24, not yet on the phone)
Risk: gsd-power 51 decides idle suspend once per idle stretch. When its sleep idle watch
fires it calls logind `Suspend(false)` asynchronously and sets `current_idle_mode = SLEEP`
(`idle_set_mode()`, gsd-power-manager.c). A logind refusal (our block inhibitor) is only
logged ("Error calling suspend action"); the mode stays SLEEP, the watch does not fire again
and "less idle" modes are ignored, so nothing suspends until the user touches the phone.
(gsd's own "inhibited -> retry later" path only covers gnome-session inhibitors.) An
attempt (<= 150 s) that overlaps the sleep timeout, e.g. a `timer` run with backoff 0 while
the phone sits idle, would leave the phone awake.

Options considered:
- delay inhibitor instead of block: logind suspends after `InhibitDelayMaxSec` (5 s) anyway,
  i.e. mid-attempt - the radio may be left off (no calls) or the attach reply wakes the phone
  again, which is the bug this kit fixes. Rejected.
- patch gsd-power to retry after a failed Suspend / on inhibitor release: needs a second gsd
  patch and rebuild on the phone, and gsd-power has no hook for logind inhibitor changes
  (`BlockInhibited` would need a new watch). Larger and only helps this one daemon. Not done.
- **re-arm from cell-backoff (chosen)**: after every attempt (success or not) `systemd-run`
  starts a transient timer `fp5-cell-rearm` 20 s later (outside the sleep hook, so it never
  asks for a suspend from inside a suspend transaction). `fp5-cell-backoff rearm <n>`
  suspends with `systemctl suspend` only if gsd-power itself would have by now:
  - the suspend count (`/sys/power/suspend_stats/success`) is still the one from scheduling
    (nothing slept meanwhile, including gsd itself),
  - every panel off (`bl_power != 0`),
  - logind `BlockInhibited` has no `sleep` (an SSH session, a call app, ... still win;
    our own lock is already released),
  - no voice call in ModemManager,
  - the active wayland session is locked (`LockedHint=yes`),
  - the session user's gsd setting `sleep-inactive-{battery|ac}-type` (by UPower
    `OnBattery`) is `suspend` and its timeout is > 0,
  - Mutter/Phosh idle time (`org.gnome.Mutter.IdleMonitor.GetIdletime` on the user's
    session bus) >= that timeout.
  Any unknown value means no suspend. It never suspends earlier than the user's own
  setting and never while the screen is on or someone is using the phone. Delay
  inhibitors (fp-wake's arming, NM, MM) are honoured by logind as for any suspend. After
  that suspend the resuspend patch resets gsd's idle mode on resume as usual.

Each decision is logged (`journalctl -t fp5-cell-backoff`): "no re-arm: <why>" or
"idle past the gsd-power timeout ...: suspending".
    sudo fp5-cell-backoff rearm-check        # dry run: what it would decide now
    sudo touch /etc/fp5-cell-backoff.no-rearm  # kill switch (no timer, no suspend)
Tests: `python3 -m unittest scripts/cell-backoff/test_fp5_cell_backoff.py` (fake
mmcli/loginctl/gsettings/gdbus/busctl/systemd-run in PATH, fake /sys).

## Use
    fp5-cell-backoff status     # packet state, backoff, next try
    sudo fp5-cell-backoff reset # after a top-up: try at the next resume/timer
    sh ~/fp5-qtee-keep/cell-backoff-20261024/restore.sh   # undo everything (also stops a pending fp5-cell-rearm)
    # re-arm only: sudo touch /etc/fp5-cell-backoff.no-rearm (rm it to turn back on)
