# Match tuning: why Android feels faster and more accurate

Status: 2026-10-21. Logs: phone `~/fp5-qtee-keep/logs` copied read-only
(522 session files, `unlock-watcher.log`). Android: logcat captures under
`grok_build/fp5/dumps/android` (Jul 25 - Oct 9 2026). Phone file names and
watcher stamps are UTC; days below are Oslo (UTC+2).
`tools/fp5_unlock_stats.py LOGDIR` reproduces every number here.

## Numbers

Hit rate is AUTH HIT / (AUTH HIT + AUTH FAIL itype=0x2), all log kinds:

| Oslo day | HIT/(HIT+FAIL) |
|---|---|
| Oct 7 | 12/19 |
| Oct 8 | 51/105 (49 %) |
| Oct 9 | 22/46 |
| Oct 10 | 50/119 (42 %) |
| Oct 11 | 20/42 |
| Oct 12 | 8/25 (32 %) |
| Oct 21 | 12/44 (27 %): 0/6 at 18:12, then 12/38 |

Unlock-watcher presses by session build (install times from `stat` on
the phone; the main 076720f source rebuilds byte-identical to the live
`fb4ee59b`):

| build | presses | hit rate | hits on scan 1/2/3 | wake presses hit |
|---|---|---|---|---|
| f5f41357 (Oct 8-10, one burst) | 54 | 14/44 = 32 % | 14/0/0 | - |
| f9bd8b60 parity (Oct 10) | 87 | 35/85 = 41 % | 34/1/0 | 1/2 |
| 80f43e44 (Oct 10-11) | 63 | 35/62 = 56 % | 33/2/0 | 1/2 |
| 7abd5be4 keep (Oct 11-19) | 27 | 8/25 = 32 % | 8/0/0 | 0/14 |
| fb4ee59b merge (live) | 44 | 12/44 = 27 % | 10/0/2 | 5/20 |

Across all multi-burst builds a second or third scan rescued 5 of the
114 presses whose first scan missed and the finger stayed down. Both `7abd5be4` and the live
`fb4ee59b` contain the "skip falling" rule (string in the binary; it
fired on 6 presses on Oct 21). Neither build is a one-burst build.

What a miss looks like (448 scored reports): image quality median 86,
coverage 99, and in 398 of 425 the best subtemplate score is 0 after
comparing all of them (matchCnts 20 with one finger, 40 with two). A hit:
quality median 88, best score median 14, found after a median of 7
subtemplates. The capture is good. The presented part of the finger is
not in the template: 20 subtemplates per finger (`total_subtemp = 20`).

Timing (TA QTimer stamps on qsee lines, 19.2 MHz; checked against the
watcher clock, 3852 s of ticks = 3853 s wall):

| | ours (fb4ee59b) | Android |
|---|---|---|
| TA match per report | 16 ms miss, 19 ms hit | 8-14 ms |
| one scan (3 CAP + REPORT) | 199 ms median (174-242) | 105-145 ms per rescan; IRQ -> authenticated 109-147 ms |
| wake press, listen start -> unlock | 2 s (cold `unlock` load before arm) | n/a (HAL already loaded) |
| after a nonzero miss | process exits, ~1 s sleep + reload | rescan within ~20 ms |

## Differences from the Android HAL

| Area | Android (evidence) | Here |
|---|---|---|
| Template update | `algorithm.enable_template_learning: on`, `template_update_threshold: 50`, `max_sub_template_num: 48`; HAL has `FF_CMD_TA_UPDATE_TEMPLATE` and a lazy data updater | none; 20 subtemplates per finger forever; no update command is sent |
| Calibration learning | `enable_post_calib_learning: on`, `calib_update_threshold: 50` | not configured |
| Rescan | `max_authentication_rescan_times` 5 (Jul) / "(n/2)" (C.108, Sep-Oct), `max_authentication_rescan_timing` 1000 ms, only while "check finger status: touch"; stops on "finger is leaving" | up to 3 scans, no time budget, plus a "skip falling" stop (avgv drop >= 80) the HAL does not have |
| Capture | FDT_DOWN_DETECT, IRQ -> one 3-frame burst at inte 128 (transmit_image 40-45 ms) | same mode and 3 frames, but 3 CAP commands plus a 128 KiB qsee log read per command; scan 50-90 ms slower |
| Finger-down | blocking IRQ in the HAL | `irq_count` sysfs poll every 20 ms |
| Quality gate | identify quality 40, coverage 70 | same (SYNC_CONFIG) |
| DAC | AVG DAC_Temp 119-130, middle 863-874 | 120 / 869: same |
| Strikes / lockout | one reject per touch; 5 -> 30 s, 20 -> PIN | same |
| Load | HAL and TA resident | cold `unlock` per listen (warm `serve` exists, off) |
| Wake press | HAL is armed before the wake | session starts at panel-on, so the wake finger meets a 1.5-3 s load |

## Ranked changes

1. Template learning like Android (largest accuracy effect: the template
   grows from 20 towards 48 subtemplates with the placements you use).
   It writes the template (an update or save after a hit). **Needs your
   explicit OK and a template backup first. Not implemented.**
   Without writes, re-enrolling with deliberately varied placements
   (tip, left, right, flat) is the closest manual substitute (your call;
   enroll was not run).
2. Warm `serve` mode (`FP5_QTEE_WARM=1`, step C below). Removes the
   1.5-3 s load at each wake and after each nonzero miss. Existing code,
   watcher setting only.
3. Android rescan rule + TIME lines (commit on `match-tuning`, binary B).
   Small accuracy effect (scans 2-3 rescued 5 of 114 held first-scan misses;
   the falling skip cut 6 presses on Oct 21) and the timing needed to
   find the 50-90 ms per scan.
4. Per-scan overhead (3 CAP + 128 KiB log read per command; the debug
   watch thread reads `/dev/qsee_log` every 30 ms in `unlock` mode). Wait
   for B's TIME lines before changing it.
5. Calibration learning (`enable_post_calib_learning`) may persist
   calibration data. Needs your OK.

## A/B plan

See `scripts/match-tuning/` (staged on the phone in
`~/fp5-qtee-keep/match-tuning-20261021/`).

## Template learning (deployed 2026-10-21, binary 7d0d322c)

From `fingerprint.default.so` (sha a1a4dd99…), `device_event_thread`:

- After every authentication result (after `reportEvent`, hit or miss)
  the HAL fills its 0x2e0 user buffer with zeros, copies `+0x2c8` and
  `+0x2d0` from the report event context, sets `+0x2d8` to 0x80000
  (constant at 0x97998) and `+0x2a4` to 0, and calls
  `ff_trustlet_update_template`: command table entry 0x979b8 =
  `{0x1015, 0x2e0}`.
- rc 0 and byte `+0x2a4` != 0: `0x52aac(0x60000000)` ORs the mask into
  the pending mask and (re)starts the 5000 ms "lazy data updater". Its
  callback calls `ff_trustlet_save_data(mask)` = `0x1014`, length 4,
  payload = mask, then clears the mask. On a failed auth the HAL first
  cancels a running lazy timer.
- After a report (rc 0 or -11), bit 0x40000 at `+0x2d8` of the answer
  queues mask 0x80000000 the same way.
- Config (TA reads these keys; the HAL dump shows them):
  `enable_template_learning` on, `template_update_threshold` 50,
  `max_sub_template_num` 48, `enable_post_calib_learning` on,
  `enable_prep_calib_learning` off, `calib_update_threshold` 50.

The session does the same, but saves right away instead of after 5 s,
because the watcher quits a warm session soon after an unlock (and
SIGTERMs a cold one at once). TERM/HUP/INT wait for a running
update/save. Log lines: `LEARN <n> update hit= rc= changed= stats=`,
`LEARN <n> save mask= rc= ms= rpmb_cmd= template= persist=`,
`TIME <n> learn`/`learn-saved`.

Finger waits now block on `/dev/focaltech_fp` (edge-triggered epoll on
the driver's wait queue; irq_count sysfs has no `sysfs_notify`, so
POLLPRI never fired). Fallback is 5 ms polling. The 128 KiB qsee ring
is still read per command (avgv comes from it), but the whole-ring
scan and the unlock-mode watch thread are off (`FP5_QSEE_FULL`,
`FP5_QSEE_WATCH`). `TIME caps` lines now show `tee=` (ms inside the
TEE invoke) and `cmd=` (ms in send_cmd) per burst, so the host share
of a burst can be read off directly.

Kit: `scripts/learn/` (deploy.sh, restore.sh, common.sh, README.md).
