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

## Measured on the phone (FP5, kernel `7.2.0-nfc-test+`, 2026-10-09)

| What | Result |
|---|---|
| #6 panel-off / PIN stop | pass |
| #7 miss notice | fail as first pushed: the Phosh lock screen shows only the notification summary ("Fingerprint"), not the body. Fixed in #7: the sentence is now the summary (also the arm and lockout notices). |
| #8 timed lockout | pass |
| #8 light tap | reported `partial`, no strike (right), but buzzed, showed the notice, and could never match: `fp5_burst_ok()` needed all 3 frames in range and light taps had 1–2. Fixed in #8: auth scores a burst with at least one in-range frame (enrol still needs all 3); the trustlet still does the match. |
| #8 wake press | the power key is the sensor, so the press that wakes the panel was sometimes scored as a miss (strike + buzz). Fixed in #8: a no-match from a press down within `FP5_QTEE_WAKE_GRACE_MS` (400 ms) of panel-on is ignored; a hit still unlocks. |
| Cold start (`unlock`, or a fresh `serve`) to armed | 3–6 s |
| Re-arm inside one `serve` session | ~1 s |
| #9 warm match, sensor released after unlock | pass |
| #9 after unlock | every unlock quit `serve`, so each new lock paid the cold load again. Fixed in #9: a new `serve` starts as soon as logind reports the lock (signal), panel on or off. |
| Watcher idle CPU | ~0.8 % of one core (before the #9 change that drops the 1 s LockedHint poll while unlocked with the panel on) |
| Wake sources | only `pmic_pwrkey` and `gpio-keys` can wake the SoC. The fingerprint interrupt (IRQ 256, TLMM gpio34) has wakeup disabled. |

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
- Measured: the finger interrupt is IRQ 256 on TLMM gpio34, with wakeup
  disabled. Only `pmic_pwrkey` and `gpio-keys` are wakeup sources. The
  sensor is the power key, so today the press both wakes the panel (via
  pwrkey) and may be read as a finger; #8 ignores a miss from that press.

### Design: touch to wake (not in this round; needs the kernel)

Goal: a finger on the sensor with the panel off wakes the phone and the
same press unlocks, without giving up #6's rule that a dark phone never
unlocks from a press it did not mean (pocket, bag).

1. **Kernel/DT: make the finger interrupt a wakeup source.**
   - In `focaltech_fp_life` (or its upstream replacement):
     `device_init_wakeup(dev, true)`; in `suspend` call
     `enable_irq_wake(irq)` (gpio34 / IRQ 256) and `disable_irq_wake()` in
     `resume`; in the handler `pm_wakeup_dev_event(dev, 0, true)` so the
     system stays up long enough for userspace.
   - DT: a node for the sensor's interrupt (`interrupts-extended = <&tlmm 34
     IRQ_TYPE_EDGE_RISING>`, polarity to confirm from the vendor tree) with
     `wakeup-source`, plus the TLMM pin config so gpio34 is routed to the
     PDC (wake-capable) while suspended. Check `/sys/kernel/irq/256/wakeup`
     reads `enabled` and that `cat /sys/power/wakeup_count` moves on a touch.
   - The driver makes the event pollable (`sysfs_notify` on `irq_count`
     or `poll()` on the char device); #9 already waits with `POLLPRI`.
2. **Kernel: inject `KEY_WAKEUP`.** On a finger-down while the panel is
   off, the driver (or a tiny input device it registers) reports
   `KEY_WAKEUP` press+release. Phosh/logind already treat it like the
   power key, so the panel comes on through the normal path and nothing in
   userspace has to drive DPMS.
3. **Session (here): keep `wait-touch` armed while dark.** In warm mode
   the `serve` session stays armed in work mode 1 (finger-down detect)
   instead of cancelling on panel-off, and blocks in `poll()` with no
   timeout. The TEE session and sensor state must survive system suspend;
   test that first (if not, re-arm on resume, from the PM notifier or the
   logind `PrepareForSleep(false)` signal).
4. **Match the same press.** The finger that woke the phone is still down,
   so the session goes straight to capture → REPORT for that press. There
   is no second touch.
5. **Guard: only the waking press can unlock while dark.** Keep #6's
   pocket protection by allowing exactly one attempt per dark wake:
   - The watcher only accepts a hit when the press's down timestamp is
     within a short window (e.g. 300 ms) of a `KEY_WAKEUP` it saw on the
     input device (or of the panel-on edge), and only one hit per wake.
     A press that started while dark but did not produce a wake event, or
     a second press, is cancelled exactly as #6 does today.
   - A miss from the waking press is silent (no strike if within the #8
     wake grace, no notice) and the panel turns off again after the
     normal Phosh timeout.
   - **Proximity:** before accepting the hit, read iio-sensor-proxy
     (`ClaimProximity`, `ProximityNear`). Near → refuse the unlock, no
     strike, cancel and let the panel go off. If proximity is unavailable,
     fail closed (no dark unlock; power key still works).
   - Policy (#8) still applies: PIN after boot / 72 h, lockout.
   - Off by default, a user setting (`FP5_QTEE_TOUCH_WAKE=1`).
6. **Battery.** Only interrupts matter: with the IRQ as a wake source the
   SoC sleeps until a touch. Measure suspend current and wakeups/hour
   (`/sys/kernel/debug/wakeup_sources`) overnight with it on and off, and
   count false wakes in a pocket.

Until 1–2 exist, the power key is the screen-off path. The watcher's own
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
