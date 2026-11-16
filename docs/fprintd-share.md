# Shared verify: run0/polkit fingerprint through the watcher's session

## Problem (2026-11-16)

`pam_fprintd` → `fp5-fprintd` used to take the sensor from the unlock
watcher: it asked the watcher to quit its kept serve session
(`/run/fp5-fprintd/want-sensor`) and started its own. Each qcomtee
session needs a fresh 4 MiB physically contiguous buffer (order 10), and
the watcher needs another one at the next lock. With RAM nearly full
(Waydroid, ~5 GB swap) `/proc/buddyinfo` shows 0 free order-10 blocks in
Normal even after compaction, so `memory_ok_for_yield()` refuses (on
purpose, after the 2026-11-01 lock screen without a session) and every
run0 prompt fell straight to the password.

## Design

The watcher's kept session is already loaded and idle while unlocked. A
verify only needs one `auth` on it.

- **Session (`session/fp5-qtee-session.c`, serve mode).** When stdin is a
  socket (started by `fp5-qtee-broker` for the watcher), the session also
  listens on the abstract unix socket `@fp5-qtee-share`. It accepts only
  peers with uid 0 (`SO_PEERCRED`), one at a time (a second one gets
  `SERVE reject busy`). A client gets `SERVE ready` / `SERVE idle` and may
  send `auth`, `cancel`, `quit` (the same line protocol as stdin). For a
  client `auth` the session runs one normal `auth_once` with stdout
  pointed at the client, so every `AUTH ...` line, `SERVE result` and
  `SERVE idle` go to the client only. The watcher sees nothing and its
  auth numbering is unchanged (shared attempts are numbered from
  100001).
- **The watcher always wins.** While idle the watcher's line is taken
  first. During a shared attempt a watcher `auth` cancels it and runs right
  after it (no extra `SERVE idle` to the watcher, so its attempt number
  still matches), a following `cancel` is applied to that auth, and
  `quit`/EOF cancels and ends the session as before. A client `auth`
  that comes during a watcher attempt waits until it is over.
- **fp5-fprintd.** `Claim` and `VerifyStart` first try `SharedSession`
  when the seat is unlocked (`seat_locked() is False`) and another
  fp5-qtee-session runs. It checks that the socket peer is uid 0 with comm
  `fp5-qtee-sessio`, then verifies through it with the same line parser
  (`verify_event`) and the same fid → user check in `fingers.json`. No
  want-sensor marker, no flock, no own session, no memory check. Release,
  VerifyStop, idle timeout, client exit and a seat lock just disconnect;
  the session is never signalled. If the socket is missing or refuses,
  the old handover path runs unchanged (including its 2-block rule).
  Enroll, delete and list still use their own session as before.
  `FP5_FPRINTD_SHARE=0` in a drop-in turns sharing off;
  `FP5_QTEE_SHARE=0` in the `fp5-qtee@.service` environment stops the
  session from listening.

Why root-only: a match on this socket authorizes run0. The user-owned
watcher could not be trusted to relay it (any process of that user could
pretend to be the watcher), so `fp5-fprintd` talks to the root session
directly and the session talks only to root.

Lock-screen safety: the watcher keeps its session and ownership the whole
time. Locked, fprintd neither connects nor verifies (and closes a shared
client within 2 s of a lock). Touch-to-wake and dark arming are watcher
`auth`s, which preempt any shared attempt.

## Tests

    make -C session test            # includes test_fp5_share.py (fake auth_once)
    cd fprintd && python3 -m unittest test_fp5_fprintd
    cd fprintd && dbus-run-session -- python3 -m unittest test_fp5_fprintd_share_dbus
    cd fprintd && dbus-run-session -- python3 -m unittest test_fp5_fprintd_dbus
    cd app && python3 -m unittest test_fp5_qtee_unlock

`session/test_fp5_share.c` builds the real serve loop with
`FP5_SERVE_TEST` (no TEE: `set_group` is a no-op and `auth_once` is a
fake that "hits" when a file appears). Not covered on the host: the
epoll wait on `/dev/focaltech_fp` (the harness uses the poll fallback),
and the real trustlet.
