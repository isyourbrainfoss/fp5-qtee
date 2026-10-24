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

## Use
    fp5-cell-backoff status     # packet state, backoff, next try
    sudo fp5-cell-backoff reset # after a top-up: try at the next resume/timer
    sh ~/fp5-qtee-keep/cell-backoff-20261024/restore.sh   # undo everything
