# FP5: phone stays awake after an unattended wake (gsd-power idle state)

## Symptom
Something wakes the suspended phone with nobody touching it (RTC alarm, modem,
...). Phosh lights the screen, blanks it again after 60 s, and then the phone
never suspends again (~400 mW until someone touches it). Seen on 2026-10-22:
suspend 21:57:51 UTC, unattended wake 22:07:17 UTC, awake all night, 96 % -> 80 %.

## Root cause (reproduced, not a config problem)
`gnome-settings-daemon` 51.0, `plugins/power/gsd-power-manager.c`:

* `idle_set_mode()` ignores transitions to a "less idle" mode
  (`mode <= current_idle_mode`, except NORMAL).
* After an idle suspend `current_idle_mode` is `SLEEP` and is only reset to
  `NORMAL` by `idle_became_active_cb()` = a *user-active* watch on
  org.gnome.Mutter.IdleMonitor, i.e. by real input.
* `handle_resume_actions()` (PrepareForSleep(false)) only calls
  `enable_monitors()` + `inhibit_suspend()`; it does not touch the idle state.

So after a resume that nobody caused there is no input event, `current_idle_mode`
stays `SLEEP`, and every later dim/blank/sleep watch is dropped by
`idle_set_mode()` ("Not going to 'less idle' mode"). Phosh does its part
(on PrepareForSleep(false) it wakes the screen and re-creates its idle timers,
so the IdleMonitor watches *do* fire again), but gsd-power ignores them.

Evidence (test `scripts/resuspend/test-wake.sh`, stock gsd-power, 60 s RTC wake,
9 min watch, no SSH, no charger): screen on at wake, blank at +60 s, the
IdleMonitor watch gsd-power registered for sleep fired at +300 s (see
`idle-base.txt`), and the phone was still awake at +540 s. The same source
analysis applies to upstream master (nothing resets the idle mode on resume).

## What is NOT the cause
* No systemd timer with `WakeSystem=`, no process holding a CLOCK_*_ALARM
  timerfd, no cron job; in a clean suspend there is no alarmtimer at all
  (ftrace `alarmtimer_suspend` never fires). An RTC wake only happens when
  someone runs `rtcwake`/writes `wakealarm` (our own A/B tests did).
* The `rtc@6100` wakeup-source counter is **not** an alarm counter: ftrace shows
  it is bumped by `sync_hw_clock -> rtc_set_time -> pm_stay_awake`, i.e. the
  kernel's periodic NTP -> RTC write (and a clock step on every resume).
  `echo 0 > wakealarm` also leaves the alarm register at now+300 s with the IRQ
  disabled; that is harmless.
* `inhibitors` at idle: only delay locks (NM, MM, UPower, rtkit, fp5-qtee-unlock,
  gsd-media-keys). sshd's "Active PAM session" is a *block* lock: an open SSH
  session also prevents re-suspend (expected).

## Fix (root, upstream-style)
`scripts/resuspend/0001-power-reset-idle-mode-on-resume.patch`: in
`handle_resume_actions()` reset the idle state machine to NORMAL after a
resume. A user-caused resume still sets NORMAL via the user-active watch (no-op).
Result: after an unattended wake, Phosh blanks at +60 s and gsd-power suspends
again when its normal 300 s (battery) idle timer fires.

Built from the 51.0 release tarball (same version as the installed
`gnome-settings-daemon-51.0-r0`), release build, prefix /usr; only
`/usr/libexec/gsd-power` is replaced (stock `libgsd.so` is reused).

## Install / restore (no reboot)
    sh ~/fp5-qtee-keep/resuspend-20261023/deploy.sh      # backs up stock binary, swaps, restarts gsd-power
    sh ~/fp5-qtee-keep/resuspend-20261023/restore.sh     # stock binary back, gsd-power restarted

gsd-power has RefuseManualStart/Stop, so `restart-gsd-power.sh` SIGKILLs it and
systemd (`Restart=on-failure`) respawns it in ~100 ms; `gnome-session-ctl
--exec-stop-check` only acts on start-limit-hit. A future `apk upgrade` of
gnome-settings-daemon replaces the binary (re-run deploy.sh, or drop the fix
once upstream has it).

## Tests
* `test-wake.sh <tag> [sleep_s] [watch_s]` – suspend with an RTC alarm, watch.
* `test-natural.sh <tag> [alarm_s] [watch_s]` – arm an RTC alarm, let gsd-power
  suspend the idle phone, wake it by RTC with nobody touching it, log whether it
  re-suspends and which IRQ counters moved across each suspend (`irqdiff.sh`).
Run both detached as root with all SSH sessions closed.
