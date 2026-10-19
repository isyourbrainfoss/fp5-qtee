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

## Settled on this phone

Compared with his `focaltechqsee` driver at `e5912e8` and with enroll and
match logs from this Fairphone 5. The stored finger stays on the path
that already matched after a reboot. His other choices are written down
so a later edit does not "fix" them.

1. Device id stays the integer 37777. That is `0x9391`, the `ft93xx`
   profile. His driver sends the string `"0x9391"` and says an integer
   is rejected as `JSON_INTEGER`. This phone enrolled and matched with
   the integer, including after a reboot on 2026-10-08 (fid 1403494260).
2. Match stays on work mode 1. That is his `FDT_DOWN_DETECT`. Enroll
   stays on work mode 9. Mode 9 is the SPI helper used while a scan is
   already running. A match armed with mode 9 does not move the IRQ.
   Enroll on this phone reached samples-remaining 0 with interrupt type
   `0x2` under mode 9.
3. The command sent after a gallery load stays opcode `0x1028`. focal32
   answers `-203` and does not delete the template. The finger count
   comes from the SET_GROUP log (`there is N template loaded`), which is
   what stops a new enroll from replacing slot 0. His list command is
   `0x2005`. Its return code is the finger count, so `1` is not a
   failure. Each entry is 8 bytes: the id, then a zero word, and the
   first id sits at offset 8. A stride of 4 reads the second finger as
   0. It stays off the unlock path until a probe logs those ids and
   ignores that status word.
4. The gallery path stays empty, and the save stays opcode `0x1014` with
   flag `0x40000000`. The trustlet then stores `/ff_template_0_0.bin`.
   That object matched after reboot. His path
   `/data/vendor_de/0/fpdata` looks up a different object on this
   listener and drops the RAM copy. His save mask is `0x60000000`,
   because a zero payload saves calibration only. `0x40000000` is the
   Android HAL flag, and it is the save that wrote this finger (about
   200 ms, `template=1`). He also sends POST_ENROLL (`0x2002`). This
   session does not. The finger still persisted.

Also kept, because an enroll on this phone already finished with them:
`max_enrolling_samples` 20 (he warns that 16 can never finish), image
columns and rows 0 (he says zero crashes), quality floors 40 and 70,
and SPI capture at 8030000. SYNC_CONFIG already runs before INIT_SPI,
which is his order. `enable_hw_reset_b4_scanning` is already false.
Trusted enrollment is already false.

Not copied: his NixOS module (nixos-fairphone-fp5 publishes no license),
the QSEECOM driver, ffsupplicant, and the RPMB listeners. The files in
`kernel/marcusramberg/` are his GPL-2.0-only patches, copied unchanged.
The C in this repo is a reimplementation under GPL-2.0-only. libfprint
itself is LGPL-2.1-or-later.

## Test checklist

1. Enroll one finger in the Finger app. It should reach samples-remaining
   0 and say it saved. The finger that is already stored should still
   match afterwards.
2. Lock the phone with the screen on. Press the enrolled finger once.
   The lock screen should go away.
3. Lock the phone and let the screen turn off. Press and hold the
   fingerprint. That button is also the power key, so the screen comes
   on by itself. The same hold should unlock. Do not press a second time.
4. Lock the phone with the screen on. Press a finger that is not
   enrolled. It should say it was not recognized, and the phone should
   stay locked. Press the enrolled finger next. That press should unlock.

The FREE_SPI and CANCEL checks below are for the same session. CANCEL
runs at the start of enroll. If the old finger stops matching after
that, restore the previous session binary and do not enroll again.

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
