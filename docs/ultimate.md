# The combined version: fp5-qtee + Marcus Ramberg's NixOS bring-up

Two independent FP5 fingerprint bring-ups exist:

- **A.** Marcus Ramberg: [nixos-fairphone-fp5](https://github.com/marcusramberg/nixos-fairphone-fp5)
  @ `9fb54a4` (a fork of gian-reto/nixos-fairphone-fp5 that adds
  `modules/fingerprint`), the [libfprint `focaltechqsee` driver](https://github.com/marcusramberg/libfprint)
  @ `e5912e8`, and [fp5-fingerprint-tools](https://github.com/marcusramberg/fp5-fingerprint-tools).
  It uses a QSEECOM TEE driver, the `focaltech-fp` misc driver with a DT node,
  ffsupplicant, and libfprint/fprintd.
- **B.** This repo: upstream QCOMTEE via a bridge, one C session with
  built-in listeners, and the Phosh watcher with Android-parity UX (#6–#10).

## What this branch takes from each

| Area | Kept from | Why |
|---|---|---|
| TEE path (QCOMTEE, no new TEE driver) | B | It is closer to what upstream accepts. A's 3.2k-line QSEECOM driver is not copied. |
| Bring-up order (VERSION, SET_KM, CHIP, CALIB, HEALTH, avgv gating, itype filter) | B | Measured on the phone, and A lacks these opcodes. |
| **FREE_SPI (0x1007) then INIT_SPI retry when INIT_SPI fails** | A | focal32 keeps the bus across clients and answers -5. That matters now that serve mode keeps sessions long and a killed one can leave it held. |
| **FREE_SPI on session exit** | A | Same reason: hand the bus back so the next session's INIT_SPI works. |
| **CANCEL (0x2004) before PRE_ENROLL** | A | Clears enroll state left by an aborted enroll. TA globals outlive the client. |
| **DT node + `focaltech-fp` misc driver** (`kernel/marcusramberg/`) | A | The in-tree replacement for the out-of-tree `focaltech_fp_life.ko`, with a pollable IRQ and a wakeup source. Vendored as-is (GPL-2.0-only). |
| Lockout 5/20, PIN after boot / 72 h, haptics (aw86927 click / double click), warm serve, wake-press filtering | B | Android parity. A has none of it. |
| Nix | A (idea) | `flake.nix` builds the session. Full system integration stays in A's `modules/fingerprint`. |

Not taken: A's libfprint/fprintd path. Phosh cannot use it to unlock today
(see `android-parity.md`: Phosh answers every PAM prompt with the password).
The long-term meeting point is one fprintd-facing daemon with a pluggable
transport.

## Still open between A and B (settle on one phone)

1. Device id: A sends string `"0x9391"`, B sends integer 37777.
2. Enroll detect mode: A uses WORK_MODE 1, B uses 9.
3. ENUMERATE: A uses 0x2005 with 8-byte entries, B uses 0x1028.
4. Gallery path / save: A uses the `fpdata` path + POST_ENROLL, B uses `""` + SAVE 0x1014|0x40000000.

## On-phone checks for this branch

1. `make -C session` on the phone, then `fp5-qtee-session unlock`: the log
   shows `INIT_SPI rc=0` and `FREE_SPI_EXIT` at the end, and a match still
   works.
2. Kill a serve session with `kill -9` mid-wait, then start a new one. If the
   log shows `INIT_SPI rc=-5`, it must be followed by `FREE_SPI` and
   `INIT_SPI2 rc=0`, and auth works.
3. Enroll a finger in the Finger app, abort halfway, then enroll again:
   `CANCEL_STALE` is logged, the second enroll completes, and both old and new
   fingers still match. Check that CANCEL does not wipe the gallery.
4. Everything in #6–#10's check lists (haptics, lockout, warm serve).
5. Kernel (optional): build with `kernel/marcusramberg/*.patch` and check
   `/dev/focaltech_fp` and the DT node probe. The session does not use that
   driver yet.
