# Fast-tap user test plan (fast-tap-20261026)

Takes about 10 minutes. You need the phone locked with fingerprint unlock
working (PIN entered once since boot). Note the clock time when you start
each block; the logs are matched by time.

## What changed that you can feel
- Quick taps should unlock more often, and a little sooner.
- A tap that was too short for the sensor gives **three tiny ticks** and a
  short hint "Finger lifted too fast". It never counts as a failed attempt.
- The first wrong finger right after the screen turns on now gives the
  normal **double pulse** instead of nothing. It still does not count as an
  attempt.
- After unlocking and locking again, the sensor is ready at once (no ~1 s
  reload).

## Block A: screen on (about 3 min)
1. Lock, press power so the lock screen shows.
2. 10 x **quick tap** (touch and lift right away) with an enrolled finger.
   Wait for the unlock or a buzz each time, lock again between taps.
3. 10 x **normal hold** (rest the finger until it unlocks).
4. 5 x quick tap with a **non-enrolled** finger (or knuckle): expect the
   double pulse, and lockout after 5 as before.
Write down: how many quick taps unlocked, how many ticked "too fast",
how many did nothing at all.

## Block B: screen off (about 3 min)
1. Lock, let the screen go off, wait 30 s (phone asleep).
2. 10 x quick tap on the sensor with the screen off. A hit should unlock
   and turn the screen on.
3. 10 x normal hold with the screen off.
Write down the same three counts.

## Block C: relock (1 min)
1. Unlock with the finger, then lock with power right away and tap the
   sensor within 1 s. 5 times. Count unlocks.

## Block D: pocket sanity (1 min)
1. Screen off, phone in pocket, walk ~30 s. Expect at most an occasional
   tick, not a stream of buzzes. Then check the PIN still works normally.

## Block E: Finger app (1 min)
1. Unlocked: open Finger and run Match (not Enroll). It must start
   normally (it may take up to ~1 s longer the first time).

## What we will measure afterwards (from the logs)
From `~/fp5-qtee-keep/logs/finger-serve-*.txt` TIME lines and
`unlock-watcher.log` (millisecond stamps):
- IRQ -> QEV (`irq` -> `qev`), IRQ -> 3 frames (`frame i=2`), IRQ -> hit
  (`hit`), per-frame trustlet ms; targets: frames <= 80 ms, hit <= 150 ms.
- result -> next `arm` (re-arm gap).
- counts of `toofast`, `empty`, `fail`, `hit`, and `type from rc` (presses
  that used to be dropped), and `resume` queries.
- watcher reaction: session `hit` -> `unlock` note.
Baseline before this change (Oct 21-25 logs): IRQ -> 3 frames 143-176 ms,
IRQ -> hit 177-181 ms, result -> re-arm ~154 ms, relock reload 0.8-1.0 s.

## If something is wrong
`sh ~/fp5-qtee-keep/fasttap-20261026/restore.sh` puts the previous
binary and watcher back and restarts the watcher (no reboot).
