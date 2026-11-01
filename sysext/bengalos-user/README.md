# BengalOS FP5 user config (2026-10-31)

Per-user files, installed into the `fp5` home (no root, nothing in /usr):

| File | Goes to | Why |
|---|---|---|
| `20-fp5-dark-unlock.conf` | `~/.config/systemd/user/fp5-qtee-unlock.service.d/` | Nura's `zz-fp-wake.conf`: `FP5_QTEE_WARM=1`, `FP5_QTEE_WAKE=1`; unlock from a dark panel and from suspend |
| `fp5-strong.json` | `~/.config/feedbackd/themes/` + `gsettings set org.sigxcpu.feedbackd theme fp5-strong` | default theme vibrates calls at magnitude 0.5 periodic (~19 % of the aw86927 range after feedbackd's 75 % gain), buttons 7 ms at 0.2. It also carries the `fp5-qtee-match/miss/toofast/lockout` events: without them feedbackd plays nothing for a fingerprint unlock (fbcli: "No feedback found"; the watcher now falls back to the evdev rumble too) |
| `40-fp5-norsk.conf` | `~/.config/systemd/user/fp5-qtee-unlock.service.d/` | `FP5_QTEE_LANG=nb`: lock-screen notices in Norwegian ("For mange forsøk. Bruk PIN.") |
| `fp5-ff-gain.py`, `fp5-ff-gain.service` | `~/.local/bin/`, `~/.config/systemd/user/` | feedbackd writes FF_GAIN 0xc000 at open; this raises it to 0xffff. `FP5_QTEE_FF_RESTORE_GAIN=0xffff` stops the fingerprint player from putting 0xc000 back |

Kernel: `focaltech-fp` with DT `wakeup-source` arms `enable_irq_wake` on gpio34 (PDC) in suspend while the session listens.
