# focaltech_fp_life 0.8-wake

The lab FP driver (`fp5-fingerprint/oot-fp/focaltech_fp_life.c`, 0.7,
srcversion 10A1052D52E5340E87CE9E6 = the module loaded on the phone) plus
touch-to-wake (`0001-irq-wake.patch`):

- `device_init_wakeup()` at probe (`wake_default=1`): the platform device
  gets `power/wakeup` (enabled) and a wakeup source named `focaltech_fp`.
- `dev_pm_ops` suspend/resume: `enable_irq_wake()` on the FP IRQ while it
  is enabled (`listen=1`), `disable_irq_wake()` on resume. GPIO 34 maps to
  PDC pin 77 on sc7280 (`sc7280_pdc_map`), so deep suspend can resume on it.
- The IRQ handler reports a wakeup event (`wake_hold_ms`, default 3000;
  "hard" while suspending so a touch during suspend entry aborts it) and
  counts edges seen while suspended in `/sys/class/focaltech_fp/focaltech_fp/wake_count`.
- dmesg: `focaltech_fp focaltech_fp: suspend: irq_en=1 wake=1 armed=1 ...`
  and `resume: irq_count=... wake_count=...`.

Off switch without unloading:
`echo disabled | sudo tee /sys/devices/platform/focaltech_fp/power/wakeup`.

Build: `./build.sh` (desk, KDIR = 7.2.9 tree). Deploy: `scripts/fp-wake/`.
