# Haptic on match (BengalOS login-pin watcher)

Since 008a50a (2026-10-20, "Check loginctl unlock before treating a hit as
success") the success click waited for `loginctl unlock-session`; 772bb57
(2026-10-21) then put the dark-hit screen-on and resume-lock release in
front of it too. Measured 2026-11-17: AUTH HIT -> TriggerFeedback
median ~123 ms (60-176 ms), versus ~0 ms meant by the Android HAL.

This branch plays `success` right after the AUTH HIT passes the cheap
gates and logind says the session is locked, before wake, unlock,
screen-on and the fsync'd strike file. A failed unlock still keeps the
strikes (the part of 008a50a that matters for security).

The miss pattern is unchanged: it plays on `AUTH FAIL`, which the session
prints after up to three bursts (d5b518e / 916e6d7), ~0.5 s after
finger-down, the same as in every serve log since 2026-11-01.

Stage dir on the phone: `/var/lib/fp5-backup/20261117-fp-haptic/`.
`install.sh` keeps `fp5_qtee_unlock.py.orig`; `rollback.sh` restores it.
