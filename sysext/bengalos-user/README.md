# BengalOS FP5 user config (2026-10-31)

Per-user files, installed into the `fp5` home (no root, nothing in /usr):

| File | Goes to | Why |
|---|---|---|
| `20-fp5-dark-unlock.conf` | `~/.config/systemd/user/fp5-qtee-unlock.service.d/` | Nura's `zz-fp-wake.conf`: `FP5_QTEE_WARM=1`, `FP5_QTEE_WAKE=1`; unlock from a dark panel and from suspend |
| `fp5-strong.json` | `~/.config/feedbackd/themes/` + `gsettings set org.sigxcpu.feedbackd theme fp5-strong` | default theme vibrates calls at magnitude 0.5 periodic (~19 % of the aw86927 range after feedbackd's 75 % gain), buttons 7 ms at 0.2. It also carries the `fp5-qtee-match/miss/toofast/lockout` events: without them feedbackd plays nothing for a fingerprint unlock (fbcli: "No feedback found"; the watcher now falls back to the evdev rumble too) |
| `fp5-ff-gain.py`, `fp5-ff-gain.service` | `~/.local/bin/`, `~/.config/systemd/user/` | feedbackd writes FF_GAIN 0xc000 at open; this raises it to 0xffff. `FP5_QTEE_FF_RESTORE_GAIN=0xffff` stops the fingerprint player from putting 0xc000 back |

Not needed any more (2026-11-02): `30-fp5-release-when-unlocked.conf`
(`FP5_QTEE_KEEP_WARM=0`, let go of the sensor at every unlock so
fp5-fprintd can use it for run0/polkit). The default stays
`FP5_QTEE_KEEP_WARM=1`: the watcher now quits its kept session only while
fp5-fprintd holds `/run/fp5-fprintd/want-sensor` and loads it again when
fprintd is done, so the lock screen does not pay the ~1 s reload.
`FP5_QTEE_KEEP_WARM=0` in a user drop-in under
`~/.config/systemd/user/fp5-qtee-unlock.service.d/` is still the fallback
if fprintd ever reports "another fp5-qtee-session is running".

Sleep: the watcher now disarms the sensor before s2idle and arms it again after resume (`FP5_QTEE_SLEEP_DISARM=1`, default). Add `Environment=FP5_QTEE_SLEEP_DISARM=0` to `20-fp5-dark-unlock.conf` for the old touch-to-wake from suspend (docs/fp-wake.md).

Kernel: `focaltech-fp` with DT `wakeup-source` arms `enable_irq_wake` on gpio34 (PDC) in suspend while the session listens.
