# Battery-percent wakes (fp5-battwake, 2026-10-25)

## Symptom
Overnight 2026-10-24/25 (12 h, `~/fp5-qtee-keep/resuspend-20261023/{wake-recorder,irqtrace}.log`):
33 suspends, 72 % asleep, 2.2 %/h. Every traced wake started with `ipcc_0` (IRQ 13) then
`glink-smem` IRQ 241 (IPCC client ADSP, hwirq 196608) and lined up with a 1 % battery drop.
After each wake the lock screen lit for 60 s (phoc modeset at +0.2 s, "Turning off" at +60 s)
and the phone stayed up ~6 min (gsd-power sleep-inactive 300 s) -> ~22 % of the night awake.

## Cause
qcom_battmgr gets one notification per percent from the ADSP over pmic_glink. The GLINK
(and IPCC) IRQs are requested with `IRQF_NO_SUSPEND`, so the IRQ stays live in suspend.
- **deep** (`/sys/power/mem_sleep` default on this kernel): PSCI system suspend; the live
  IPCC interrupt resumes the SoC in hardware. Nothing in sysfs filters it
  (`/sys/kernel/irq/{13,241}/wakeup` are already `disabled`; there is no pmic_glink/glink
  `power/wakeup` knob). Verified 2026-10-25 10:05-10:25: with the battmgr wakeup knob
  below disabled the phone still resumed at 68->67 % with ipcc_0/241.
- **s2idle**: the IRQ runs, but the system only resumes for a *wakeup event*.
  `power_supply_changed()` calls `pm_stay_awake()` on the supply device, and
  `qcom-battmgr-bat/power/wakeup` is `enabled` by default (usb/wls/ucsi are already
  disabled by suspend-fix-wakes.sh) -> each percent is a wakeup event.
- Screen: Phosh (`screen-saver-manager.c`, `on_logind_prepare_for_sleep`) and gsd-power
  (`handle_resume_actions` -> `enable_monitors`) unblank after *every* resume; no setting.
  So the screen flash goes away only with the wake.

## What fp5-battwake does
- `fp5-battwake-apply` + udev rule `90-fp5-battwake.rules`: `qcom-battmgr-bat`
  `power/wakeup=disabled` on add/change; back to `enabled` at/below `LOW` % (default 10,
  `/etc/fp5-battwake.conf`) so the last percent steps can wake the phone for UPower's
  warning/critical action. Harmless in deep (also stops a percent step from aborting a
  suspend in progress); the real effect needs s2idle.
- Optional `--s2idle`: `/etc/systemd/sleep.conf.d/fp5-battwake-s2idle.conf`
  (`MemorySleepMode=s2idle`, systemd 262 reads it at every suspend, no reboot, no cmdline).
  See measurements below before turning it on.

Not touched: IPA/modem glink/smp2p (SMS, calls; fp5_sms_wake's SMEM poll is the s2idle
path), focaltech_fp (GPIO 34 / PDC 77), gpio-keys, pwrkey, RTC, USB/ucsi.

## Risks
- Low battery: above LOW %, percent steps do not wake the phone (s2idle). The rule
  re-enables wakeup only when it sees capacity <= LOW, i.e. at the next wake for another
  reason. A phone left untouched from > 10 % can run flat without the low-battery
  warning; UPower's critical PowerOff may not run (the PMIC still cuts off at empty).
- Charger plug/unplug while asleep: in s2idle it can no longer wake the phone through the
  battery supply (battmgr-usb and ucsi wakeups were already off before this change).
  Charging itself works; the UI updates at the next wake. In deep nothing changes.
- s2idle vs deep power: see measurements.

## Install / undo (on the phone)
    sudo sh scripts/battwake/install.sh [--s2idle]
    sudo sh scripts/battwake/restore.sh          # live, no reboot
    fp5-battwake-apply status
Tests: `python3 -m unittest scripts/battwake/test_fp5_battwake.py`

## Real fix (kernel, not installed)
RFC "soc: qcom: pmic_glink: Hold back notifications over system sleep" (Vladyslav
Tymoshchuk, 2026-10-22, lore 20261022164151.592-1-tvlad744@gmail.com): pmic_glink queues
notifications from ->prepare() to resume and reports a wakeup event only if the client may
wake and its filter accepts (battmgr: charger changes only, wakeup off by default). It acts
on wakeup events, i.e. s2idle; in deep the IPCC interrupt still resumes the SoC.
Applied cleanly to desk tree branch `pmic-glink-sleep-rfc-20261025` (off
`fp5-qtee-rpmb-v4-usb`) and built (O=/home/kb/src/build-pmic-glink-rfc), not flashed.
