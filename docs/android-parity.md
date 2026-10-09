# Fingerprint unlock on Fairphone 5: gaps next to Android, and a plan

Status: design notes, 2026-10-09. File and line references are to `main`
at `0255472` unless a PR is named. Where something has not been measured
on the phone, this says so. Hardware details come only from this repo, the
mainline device tree, and the linked docs.

PRs in this repo that act on this plan:

| PR | What | Default |
|---|---|---|
| #6 | Stop the match when the panel goes off or the PIN unlocks; notice a wake within ~0.2 s | on |
| #7 | Haptic (feedbackd) and a message on a miss | on |
| #8 | Lockout after 5/20 misses; PIN after boot and every 72 h | on (PIN rule: `FP5_QTEE_REQUIRE_PIN_AFTER_BOOT=0` turns it off) |
| #9 | `serve` session mode, kept loaded while locked | opt-in, `FP5_QTEE_WARM=1` |

#7–#9 are stacked on #6, in that order.

---

## 1. Delay between waking the screen and a finger working

### Cause

Every attempt starts from nothing:

1. `app/fp5_qtee_unlock.py` `run()`: sleeps 1 s per loop and runs
   `loginctl list-sessions` and `show-session` for each session, every loop,
   before it even reads the panel.
2. `listen_once()`: `sudo -n load-qcomtee.sh` (an `lsmod` check when the
   module is already loaded), then `sudo -n timeout 120 fp5-qtee-session unlock`.
3. `session/fp5-qtee-session.c` `main()`: `mount_persist()`, `sensor_on()`
   (sysfs power, four reset ioctls, `usleep(50 ms)`), open `/dev/tee0`,
   supplicant thread, `usleep(20 ms)`, then `open_and_load()`: four listener
   registrations, read `focal32.mdt` and `.b00`–`.b07`, build the image,
   `loadFromBuffer`. On `ALREADY_LOADED` it does lookupTA, unload, and loads
   again.
4. `setup_ta()`: 13 trustlet commands. Then `set_group()`: SET_GROUP + ENUM,
   which reads the template back from secure storage.
5. Only then does `auth_once()` arm (AUTH_ARM, then WMODE_TOUCH, mode 1).

A miss ends the process (`unlock` mode is one press), so the next press
pays for steps 2–5 again, after another 1 s sleep.

None of this has been timed on the phone. The per-session logs
(`finger-unlock-*.txt`) have no timestamps. The step that dominates
(trustlet load or setup) is not known yet.

There is also a latency floor in the wait itself: `wait_irq()` polls
`/sys/class/focaltech_fp/focaltech_fp/irq_count` every 20 ms.

### Fixes

- **Now (#6):** read the panel every 0.2 s, run loginctl only while it is on,
  cache the session id, retry at once after a miss.
- **Now, opt-in (#9):** `serve` mode does steps 3–4 once, while the phone is
  locked and dark. A wake then costs one `auth` line.
- **Later, here:** put a timestamp on every session log line (monotonic ms)
  so the remaining delay can be split up. Once #9 is proven, make it the
  default, and then turn the watcher and session into one long-running
  root daemon (see section 4).
- **Kernel (out-of-tree `focaltech_fp_life`, not in this repo):** if
  `irq_count` gets `sysfs_notify()` on each interrupt (or the char device
  gets `poll()`), the 20 ms polling can become one blocking `poll()`. #9
  already polls `irq_count` for `POLLPRI` with a 20 ms timeout, so it uses
  this as soon as the driver provides it, with no other change.

## 2. No feedback when a finger is read but does not match

### Cause

The session already tells a miss apart from no press. `auth_once()` prints
`AUTH FAIL <n> itype=0x2 …` after a scored image with no template id, and
`AUTH <n> empty …` when the capture burst fails `fp5_burst_ok()`. The
watcher only matched `AUTH HIT` (`_HIT` in `fp5_qtee_unlock.py`) and ignored
everything else. The finger also never goes through fprintd, so Phosh never
hears about the attempt: there is no PAM conversation and no
`VerifyStatus` signal.

### Fixes

- **Now (#7):** on either line, `fbcli -A org.fp5.qtee -E bell-terminal`
  (configurable) plus a transient notification.
- **feedbackd (upstream proposal):** the
  [event naming spec](https://honk.sigxcpu.org/projects/feedbackd/doc/Event-naming-spec-0.0.0.html)
  has no authentication events. Proposal: system-component events such as
  `auth-failed` and `auth-succeeded` (names to be agreed upstream), with a
  short double rumble and a light tick in the default theme. Fingerprint
  helpers, polkit agents and the lock screen could then share them, and
  #7 would switch its default.
- **Phosh (upstream proposal):** a way for an external authenticator to
  show status on the lock screen ("Not recognized", "Too many attempts")
  and to trigger the existing PIN-entry shake. Right now the only channels
  are notifications and `loginctl unlock-session`. The cleanest version is
  Phosh talking to fprintd over D-Bus (`VerifyStart` / `VerifyStatus`)
  next to the PIN keypad. It cannot be a PAM module in Phosh's stack:
  Phosh answers every PAM prompt with the typed password, as
  [Catcrafts' fingerprintd README](https://forgejo.catcrafts.net/Catcrafts/fingerprintd)
  notes, and stacked PAM modules run one after another (see the discussion
  in [swaylock#445](https://github.com/swaywm/swaylock/pull/445)).

## 3. Touch to unlock while the screen is off, at low battery cost

### What is there today

- `fp5_qtee_unlock.py` arms only while the panel is on, by design. Before
  #6 that check could be missed during the wait (a pocket press could
  unlock); #6 closes that.
- The trustlet has a chip-side finger-detect mode: work mode 1, which the
  trustlet logs as `FDT_DOWN_DETECT` (comment in `auth_once()`), and which
  the session already uses for Match. Whether this mode is low-power on
  this sensor has not been measured. Treat it as likely but unverified.
- The interrupt is seen in userspace only as the `irq_count` sysfs counter
  of the out-of-tree `focaltech_fp_life` module, which is not in this repo.
  Nothing here shows that the interrupt is a wakeup source.
- Mainline `qcm6490-fairphone-fp5.dts` has no fingerprint node. It lists
  TLMM GPIOs 56–59 as `gpio-reserved-ranges` ("fingerprint reader (SPI)"),
  so that SPI bus belongs to the secure world. The interrupt and reset
  lines are set up by the out-of-tree module, and are not described here.

### What it would take

1. **Kernel/device tree:** the finger-down interrupt has to be able to
   wake the SoC from suspend. In the driver that means `device_init_wakeup()`
   and `enable_irq_wake()` while suspended, plus `pm_wakeup_event()` on the
   interrupt so userspace gets to run. The driver also needs a pollable
   event: `sysfs_notify` on `irq_count`, `poll()` on `/dev/focaltech_fp`, or
   an input event. Upstream this means a DT binding and node for the
   sensor's interrupt with `wakeup-source`. This needs the real GPIO
   number and polarity from the vendor kernel source, which this repo does
   not have.
2. **Session (this repo, after 1):** keep the #9 serve session armed in
   mode 1 while the panel is off, blocking in `poll()` on the event fd
   instead of 20 ms steps. Whether the TEE session and sensor state survive
   system suspend needs testing.
3. **Policy (this repo):** on a finger-down with the panel off, check
   proximity first (iio-sensor-proxy `ClaimProximity` / `ProximityNear`) so
   a pocket does not count. Then capture and match. On a hit, wake the
   panel through Phosh's `org.gnome.Mutter.DisplayConfig` `PowerSaveMode = 0`
   ([Phosh MonitorManager](https://phosh-a673aa.pages.gitlab.gnome.org/class.MonitorManager.html))
   and unlock. On a miss, leave the panel off and give a haptic only, as
   Android does. Make it a user setting, off by default.
4. **Battery:** what matters is userspace wakeups, not the chip. Today the
   session polls `irq_count` at 50 Hz while armed, and the debug watch
   thread reads `/dev/qsee_log` every 30 ms. #9 drops the watch thread in
   serve mode. With the panel off, nothing should run until the interrupt
   fires.

Until 1 exists, the power key is the screen-off path. The watcher's own
notice says the sensor is on the power button ("Hold the power button to
unlock"), so a press wakes the panel. With #9 the session is already
loaded at that point.

## 4. Other gaps next to Android

| Gap | Cause (where) | Fix | Where |
|---|---|---|---|
| Unlimited attempts; finger works straight after boot | no policy in the watcher | 5→30 s, 20→PIN; PIN after boot and every 72 h | here, #8 |
| Not an fprintd device: no GNOME Settings enrol, no `fprintd-verify`, no `pam_fprintd` for sudo/polkit | the session is a CLI; the Finger app runs it | a daemon that serves the `net.reactivated.Fprint` D-Bus API on top of the serve session, as Catcrafts' `fingerprintd` does on FP6. A libfprint driver is a poor fit: libfprint drivers talk to the sensor directly, and here the sensor is behind a trustlet | here (new daemon) |
| One template slot, no finger list or delete | `group_path()` returns `""`, so the template is `/ff_template_0_0.bin` | map fprintd finger names to template ids once the trustlet's multi-finger handling is understood | here, after research |
| Needs `sudo -n` from a user service; hardcoded `/home/user`, `/run/user/10000`, kernel `7.2.0-nfc-test+` | `load-qcomtee.sh`, `SESSION_CANDIDATES`, `LOAD_SCRIPT`, wrappers | a root system service with a D-Bus API and polkit; install paths from the build; drop the uname check | here |
| Out-of-tree `qcomtee` bridge loaded with `/proc/kallsyms` addresses | README "Kernel"; `load-qcomtee.sh` | mainline `drivers/tee/qcomtee` (`CONFIG_QCOMTEE`), plus whatever listener support the trustlet needs, upstreamed or carried as a small patch in the Nura kernel | kernel |
| Out-of-tree `focaltech_fp_life` (power, reset, IRQ counter) | `sensor_on()` loads it from `/lib/modules/*/extra` | upstream a minimal driver (or reuse a generic one) with a DT binding; see 3.1 | kernel/DT |
| Every TA log line written to `~/fp5-qtee-keep/logs` per attempt | `follow()` / `listen_once()` | rotate logs, and turn verbose logging off by default once stable | here |
| No "lift and retry" guidance (Android: partial, too fast, dirty) | only `nomatch` and `partial` are known | map more trustlet log lines (`avgv`, image quality) to messages if they prove reliable | here |
| No success haptic | none sent | `FP5_QTEE_HIT_EVENT` exists (#7); give it a default once feedbackd has an event for it | feedbackd + here |
| Sensor left powered after a session | no power-off at exit on `main` (it was in the closed #1) | power off at exit when not in serve mode; re-test enrol and match | here |

## Testing that only the phone can do

Each PR lists its own checks. Across all of them:

- Time wake → armed and press → unlock, before and after #6 and #9, from
  log timestamps.
- CPU and battery over a night, locked, with `FP5_QTEE_WARM=1` and without.
- Pocket test: screen off and phone in a pocket for 10 minutes, with no
  unlock in `unlock-watcher.log`.
