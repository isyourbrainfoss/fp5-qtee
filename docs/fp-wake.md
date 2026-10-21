# Touch-to-wake (fp-wake)

Goal: like Android, a touch on the power-key sensor of a suspended, locked
FP5 resumes the phone, matches, unlocks and turns the screen on.

## Findings (2026-10-21, Nura/pmOS, kernel 7.2.9)

- Suspend is `deep` (`/sys/power/mem_sleep`: `s2idle [deep]`), requested by
  gsd-power after 300 s idle on battery (`sleep-inactive-battery-type
  'suspend'`). `fp5-suspend-fix(-wakes).service` only turns off sticky
  USB/UCSI/battmgr/serial wakes; `fp5-call-resleep-ready.sh` is call
  cleanup. An SSH login holds a `block` sleep inhibitor (sshd PAM), so the
  phone does not suspend while someone is logged in over SSH.
- FP IRQ: `msmgpio 34 Edge focaltech_fp` (Linux IRQ 255). No DT node; the
  lab driver `focaltech_fp_life` 0.7 registers a software platform device.
  TLMM has `wakeup-parent` = PDC and `sc7280_pdc_map` has `{ 34, 77 }`, so
  the line can wake deep suspend in hardware.
- Driver 0.7 had no `device_init_wakeup`, no `enable_irq_wake`, no PM ops:
  `/sys/devices/platform/focaltech_fp/power/wakeup` did not exist. The IRQ
  stays enabled (`listen=1`) after a session, VDD (gpio 60) stays on, the
  driver never powers the sensor down for suspend.
- The watcher (warm mode) cancels the armed auth when the panel goes off
  and keeps the serve session idle; nothing arms the sensor for a dark
  touch unless `FP5_QTEE_DARK_ARM=1`. A dark hit never turned the screen on
  (`should_wake_panel` is always false; `request_panel_on` writes sysfs
  behind Phosh's back).
- Phosh 0.57: `org.gnome.ScreenSaver.SetActive(false)` calls
  `phosh_shell_enable_power_save(FALSE)` — screen on, lock state kept.
  Tested: DSI dpms Off -> On, LockedHint stays yes.

## Changes

- `kernel/focaltech_fp_life` 0.8-wake: wakeup-capable device, IRQ wake armed
  in suspend while listening, wakeup event per edge, `wake_count` sysfs.
- Watcher `FP5_QTEE_WAKE=1`: warm + dark arming; a logind `delay` sleep
  inhibitor; on `PrepareForSleep(true)` it arms the serve session (auth ->
  WMODE_TOUCH, finger-detect) if it is idle, waits up to 1.5 s for
  `AUTH n arm`, then releases the lock; it takes it again on resume. A hit
  while the panel is off unlocks, then calls `SetActive(false)`.
  `FP5_QTEE_WAKE_HOLD_S` (default 0) can add a short block inhibitor after
  resume; off because a refused suspend is not retried by gsd-power.

## Verified on the phone (no finger)

Short logind suspend ended by RTC (`scripts/fp-wake/test-suspend.sh`):

    watcher: sleep: locked=True session=True armed=True
    kernel:  focaltech_fp: suspend: irq_en=1 wake=1 armed=1 irq_count=9
    kernel:  focaltech_fp: resume: irq_count=9 wake_count=0
    watcher: resume: armed=True ...

The serve session kept working after resume (re-armed, TEE commands rc 0).
Not verified: a real touch while suspended (needs the user's finger).
