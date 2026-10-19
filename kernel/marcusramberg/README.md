# Kernel patches from Marcus Ramberg's NixOS FP5 bring-up

These two files are copied unchanged from
[marcusramberg/nixos-fairphone-fp5](https://github.com/marcusramberg/nixos-fairphone-fp5)
at commit `9fb54a4` (`packages/kernel/patches/`). They were written by Marcus
Ramberg (`Signed-off-by: Marcus Ramberg <marcus@means.no>`). They are Linux
kernel patches and are licensed GPL-2.0-only. The driver carries
`SPDX-License-Identifier: GPL-2.0-only`. That is also this repo's license.

| File | What it adds |
|---|---|
| `dts-fp5-fingerprint-sensor.patch` | `fingerprint { compatible = "focaltech,ft9362"; }` node on `qcm6490-fairphone-fp5.dts`: IRQ gpio34 rising edge, reset gpio35, power gpio60, pinctrl states, `firmware-name = "focal32"` |
| `misc-focaltech-fp-driver.patch` | `drivers/misc/focaltech-fp`: `/dev/focaltech_fp` with ENABLE_IRQ / DISABLE_IRQ / RESET ioctls, IRQ delivered through `poll()`/`read()`, a wakeup source held while an event is pending, and `IRQF_NO_AUTOEN` |

## Why they are here

fp5-qtee still runs against the out-of-tree `focaltech_fp_life.ko`, which is
not in this repo. That module has sysfs `irq_count`, `vdd`/`spiclk`/`listen`,
and its own ioctl numbers. Marcus's patches are the in-tree, upstreamable
replacement for that module. They are the base we want to reach:

- a DT node instead of a module that hard-codes the pins
- a pollable event instead of polling `irq_count` every 20 ms, so serve mode
  can block in `poll()` while the screen is off (battery)
- a wakeup source, which is half of "touch the sensor to wake the phone"

## Not done yet (kernel work, unverified)

- **Wake from suspend.** The driver registers a wakeup source but does not
  call `device_init_wakeup()` / `enable_irq_wake()`, and the DT node has no
  `wakeup-source`. gpio34 must also be a wake-capable PDC pin on sc7280. That
  is unverified, and #10's `docs/android-parity.md` covers the gap.
- **Session backend.** `session/fp5-qtee-session.c` does not talk to
  `/dev/focaltech_fp` with these ioctl numbers yet. The ioctls differ from
  `focaltech_fp_life.ko` (`_IO('f', 2/3/5/7)`). Porting `sensor_on()`,
  `read_irq()` and `wait_irq()` to this driver is the next step. It has to be
  tested on the phone.
- The QSEECOM TEE driver and the scm allowlist from the same tree are **not**
  copied. fp5-qtee uses upstream QCOMTEE instead.
