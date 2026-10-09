# Fairphone 5 — fingerprint over QTEE

Userspace session for the signed `focal32` trustlet on Fairphone 5
(Qualcomm SM7325 / sc7280, postmarketOS). A small Finger app enrolls and
matches through `/dev/tee0`. It does not load `qsee_fingerpr`. Phosh
unlock is a separate watcher, not this app.

The older in-kernel Register path is
[fp5-fingerprint](https://github.com/isyourbrainfoss/fp5-fingerprint).

Status: **experimental**. Not an official Fairphone or distribution package.

## Where this stands

On 2026-10-08 the Finger app enrolled a finger (samples-remaining 0, template
written) and matched it. After a reboot the same finger still matched:
template id 1403494260, a real finger-down (`itype` `0x2`), and
`auth success`. Other presses on both boots returned fid 0 and
`authentication failed`.

Phosh does not ask PAM until a PIN is submitted, so the finger is not a PAM
module. While the lock screen is showing and the panel is on, `fp5-qtee-unlock`
runs one match (`unlock` mode, a single press). A real hit calls
`loginctl unlock-session` on the Phosh session. The panel being off does not
listen unless `FP5_QTEE_DARK_ARM=1`. A miss does not unlock and does not
wake the screen. Only a hit can unlock. PIN still works. On 2026-10-08 the
user confirmed that this dismisses the Phosh lock screen.

The watcher reads the panel state from sysfs every 0.2 s and, unless
`FP5_QTEE_DARK_ARM=1`, asks logind only while the panel is on. While a
match waits for a finger it keeps checking: panel off, or an unlock with
the PIN, stops the session at once. A hit is checked once more before
`unlock-session`.

A real press that does not unlock gives feedback straight away: two
30 ms pulses, 130 ms from the start of one to the start of the next, at
the same amplitude, plus a transient notification ("Not recognized" or
"Finger not read"). A match is a single 20 ms click. The silent
feedbackd profile plays nothing. Lockout adds no extra vibration. Each
counted enroll sample in the Finger app uses that same click. The
waveform is written to the aw86927 LED device (`duration` then
`activate`) when that device is present, and otherwise named as the
feedbackd events `fp5-qtee-match` and `fp5-qtee-miss`
(`app/fp5-qtee-feedback.json`, which plays nothing until it is merged
into the installed theme). `FP5_QTEE_HAPTIC=0` turns both off.

The watcher follows Android's rules for when a finger may unlock:

- The PIN must be used once after each boot, and again if the last PIN
  unlock is more than 72 hours old. The PIN counts as used only when the
  watcher sees LockedHint go from `yes` to `no` in two samples of the same
  session, and not within 10 s of its own unlock. A loginctl call that
  times out, fails (for example a stale session id), or prints no
  LockedHint is "unknown": never an unlock, and the sensor is not armed.
  The first sample after a watcher start is not taken as the PIN, so a
  watcher started on an already unlocked phone waits for the next PIN
  unlock. It is kept in `$XDG_RUNTIME_DIR/fp5-qtee-pin-ok` (a tmpfs, so a
  reboot or logout forgets it). `FP5_QTEE_REQUIRE_PIN_AFTER_BOOT=0` turns
  this off.
- 5 rejected fingers in a row lock fingerprint unlock for 30 seconds,
  20 until the PIN is used. A match resets the count. A partial press
  ("Finger not read") does not count. A burst is scored when at least one
  of its three frames is in range, and all three frames are sent, including
  one with avgv 600 or more. While the finger stays down a miss is tried
  again, up to three bursts, and then one reject is one strike. Enrollment
  still needs all three frames in range. The power key is the sensor,
  so a no-match from the press that wakes the panel (down within
  `FP5_QTEE_WAKE_GRACE_MS`, default 400 ms, of panel-on) is ignored: no
  strike, no buzz, no notice. A hit from that press still unlocks. A scored non-match counts even if
  the panel went off while it was being scored. The count and the end of
  the timed lockout are kept in `$XDG_RUNTIME_DIR/fp5-qtee-lockout` (mode
  0600, written atomically), so a crash and `Restart=on-failure` keep the
  lockout. If that file is unreadable, or missing while the PIN marker
  exists, or a strike cannot be saved, fingerprint unlock stays off until
  the PIN is used.

While one of these applies the sensor is not armed at all, and one
notification says why.

### Warm mode (opt-in, `FP5_QTEE_WARM=1`)

By default every attempt starts a new `fp5-qtee-session unlock`: mount,
sensor power-up and reset, `/dev/tee0`, four listener registrations, the
trustlet load from `focal32.mdt`/`.bXX`, 13 setup commands, then
SET_GROUP/ENUM. Only then is the sensor armed. That is the wait after
waking the screen, and it is paid again after every miss.

`fp5-qtee-session serve` does that setup once and then takes line
commands on stdin: `auth`, `cancel`, `quit` (EOF is `quit`). It prints
`SERVE ready`, then `SERVE idle` whenever it waits for a command and
`SERVE result <rc>` after each `auth`. Idle is a blocking read; there is no
polling and no sensor command. A finger wait also watches stdin, so
`cancel` ends it at once (`AUTH <n> cancelled`, result 3). stdin is read
before every interrupt check, after the finger-down query, after each
capture and before the verdict, so a cancelled attempt never prints
`AUTH HIT`, even if the finger was already down or had matched
(`AUTH <n> cancelled after match`). A scored non-match is still one
`AUTH FAIL` for the whole press, after up to three bursts, so it counts as
one strike. Each `auth` starts from the
interrupt count read when it is armed, so a press while cancelled or dark
does not carry into it. An `auth` sent while one runs gets
`SERVE reject auth busy`, an unknown line `SERVE reject unknown`. Serve
mode does not start the `/dev/qsee_log` watch thread.

With `FP5_QTEE_WARM=1` the watcher starts one serve session when it sees
the session locked, including in the 10 s after the panel goes off, so
the load happens while the screen is dark. On wake it reads the panel and
LockedHint again and only then sends `auth` (one loginctl call, so arming
waits for it). After a miss it re-arms straight away, again after a fresh
LockedHint. Panel off sends `cancel`, unless `FP5_QTEE_DARK_ARM=1`.
An unknown LockedHint cancels and does not arm. An unlock (finger or PIN)
quits the session, which frees the
sensor for the Finger app; nothing holds the sensor while unlocked. The
next lock starts a new serve session at once, so it is warm again before
the next wake: the watcher follows logind's lock signals through one idle
`gdbus monitor --system --dest org.freedesktop.login1` process (a hint
only; the state is still read with loginctl), so a lock with the panel on,
or long after the panel went off, preloads straight away. With the
signals, an unlocked phone with the panel on is asked LockedHint only
every 15 s instead of every second. Without `gdbus`, a dark unlocked
phone is asked every 10 s until a session is loaded. A hit unlocks only if it is from the `auth` sent
in this panel-on period and not cancelled (no hit after `cancel`, after
panel-off, or after that attempt's `SERVE result`), and it still has to
pass the panel, logind and lockout checks. A scored non-match counts as a
strike even if the panel went off meanwhile.

Enable it with a drop-in:

```sh
systemctl --user edit fp5-qtee-unlock.service
# [Service]
# Environment=FP5_QTEE_WARM=1
```

### Screen off (`FP5_QTEE_DARK_ARM`, default off)

The focaltech fingerprint IRQ is not a wakeup source on this kernel, so a
press cannot resume the CPU from s2idle. The kernel change that would make
it one is `enable_irq_wake()` on that IRQ, which is gpio 34. This
repository does not patch the kernel.

With the flag unset, a dark phone does not arm the sensor and a press
cannot unlock. Warm mode may still preload the session while the phone is
locked and dark; it does not treat that press as a wake. The no-match from
the power-key press that turns the panel on is still ignored for
`FP5_QTEE_WAKE_GRACE_MS` (default 400 ms). A hit from that press still
unlocks.

`FP5_QTEE_DARK_ARM=1` arms while the panel is off. A hit then writes `0`
to the backlight `bl_power` file and `on` to the DSI `dpms` file. A miss
never does that, and only a hit can unlock. The write cannot resume a
suspended CPU until `enable_irq_wake()` is in place.

Log scans, the time-listener reply, and the group-path buffer stop at the
buffer they were given. A group path that does not fit is refused before it
is sent. The phone session includes those checks and a one-press `unlock` mode.

Match sends authenticate first, then arms chip wait-touch (work mode 1).
It captures only an exact finger-down. A hit is an image report that
succeeds with a real template id. Authenticate-command success, report
failure, fid 0, and the poison ids are misses.

Sync config sends `preferred_device_id` 37777, which is focal32 profile
`0x9391` (the Android config). The detected id 37841 is not in that profile
table, so a template enrolled under 37841 has no feature pointers.
`algorithm_log_level` is 2 so the trustlet can print the subtemplate distance.

The signed `focal32` image is not in this repository. Copy `focal32.mdt` and
`focal32.bXX` from a Fairphone Android vendor image to `/lib/firmware/qsee/`.

## Repository layout

```
session/   fp5-qtee-session, wire and serve-command helpers, and their host tests
app/       Phosh "Finger" app (idle until Enroll or Match)
scripts/   load-qcomtee.sh and fp5-qtee-load.service (not installed)
```

## Kernel

`/dev/tee0` comes from an out-of-tree `qcomtee` module. The `7.2.0-nfc-test+`
kernel used here has `CONFIG_QCOMTEE` unset and `CONFIG_QCOM_QSEECOM=y`.
The module on the test phone also exposes `/dev/qsee_log` (avgv and interrupt
type). That module binary and its patch against mainline `drivers/tee/qcomtee`
are not in this tree.

`scripts/load-qcomtee.sh` refuses to load when `qsee_fingerpr` is already
loaded, and it is a no-op when `qcomtee` is already loaded. As root it does
not call `sudo` (a boot unit has no tty). Any other caller still uses
`sudo -n`. Any kernel other than `7.2.0-nfc-test+` still exits 1. Do not
`rmmod` a live `qsee_fingerpr`. One listener registration per boot. Do not
program or erase the RPMB key.

`scripts/fp5-qtee-load.service` runs that script once at boot so
`/dev/tee0` exists without a manual `insmod`. It is a system unit in this
repository only: it is not installed and not enabled. Copy it to
`/etc/systemd/system/` on the phone after the script at
`/home/user/fp5-qtee-keep/load-qcomtee.sh` is this copy.

## Build

On the phone, Alpine gcc is enough. pthread is in musl, so there is no
`-lpthread`. `logf` and `logl` warn against libm; leave those names.

```sh
make -C session test
python3 app/test_fp5_qtee_unlock.py
python3 app/test_fp5_qtee_coach.py
python3 app/test_fp5_qtee_haptic.py
python3 app/test_fp5_qtee_boot.py
```

Install the session where the app already looks:

- `/home/user/fp5-qtee-keep/session/fp5-qtee-session`
- `/tmp/fp5-qtee/fp5-qtee-session` (lost on reboot)

The app is `app/fp5-qtee-app.py`, launched by `app/fp5-qtee-finger`.
It stays idle until Enroll or Match is tapped. Hold about a second on PRESS.

## Do not

- Load `qsee_fingerpr` on a boot where `qcomtee` is loaded. The old
  FP Fingerprint icon starts that module.
- Treat authenticate-command rc 0 as a finger match.
- Unlink the secure-storage segment. A real unlink destroys it.
- Program or erase the RPMB key (`req_resp=1`, listener command `0x101`).

## Android parity

Compared with the Android 14 capture on this phone (2026-10-09). The
screen was off for the dark presses. Integration time was 144 or 128.
Both matched.

What matches:

- Authentication captures three frames and sends all three when at least
  one is in range, including a sibling with avgv 600 or more. A burst
  with no in-range frame is not scored. Enrollment still requires all
  three frames in range.
- A miss is retried while the finger stays down, up to three bursts, and
  then one reject is one strike. A short press that lifts sooner stops
  early. An empty press is not a strike.
- A match vibrates once for about 20 ms. A miss vibrates twice for 30 ms,
  130 ms from the start of one pulse to the start of the next, at the
  same amplitude. The silent profile plays nothing. A counted enroll
  sample uses the match click. Lockout adds no extra vibration.
- Five misses in a row show "Too many attempts" and lock fingerprint
  unlock for 30 seconds. Twenty misses lock it until the PIN is used.
  The PIN clears the lockout immediately. The notice text is the
  notification summary, which is the line Phosh's lock screen shows.
- A miss never wakes the screen. Only a hit can unlock. The no-match
  from the press that turns the panel on, within 400 ms, is not a strike.
  A hit from that press still unlocks.

What does not match yet:

- The focaltech IRQ (gpio 34) is not a wakeup source.
  `enable_irq_wake()` on that IRQ is the kernel change that would let a
  finger resume s2idle. It is not in this tree. Screen-off arming is
  `FP5_QTEE_DARK_ARM=1` and defaults off. With the flag on, a hit only
  asks a running panel to unblank; it cannot wake a suspended CPU.
- There is no proximity veto. The Android capture did not gate
  authentication on the front proximity sensor. Add one only if a real
  match starts happening in a pocket.
- The finger enrolled on Android (template id 1250370140) is not the
  postmarketOS finger. The watcher uses the id the session prints.
- The haptic timings are not confirmed on the phone. `fbcli` cannot pass
  a custom waveform. The sysfs player (`duration`, then `activate`) is
  what produces the milliseconds above. `app/fp5-qtee-feedback.json`
  plays nothing until it is merged into the installed feedbackd theme.
- `scripts/fp5-qtee-load.service` is not installed. After a reboot
  `/dev/tee0` is still missing until that unit, or `load-qcomtee.sh`,
  has been run on the phone.
- The capture integration time is unchanged (Android used 144 and 128).
  Android's detect client is not used. The 30 second lockout is the
  length kept here; that capture did not measure the timed lockout
  expiring on its own.

## Acknowledgement

Catcrafts' [Fairphone 6 fingerprint bring-up](https://catcrafts.net/posts/fairphone-6-postmarketos-fully-working-fingerprint-sensor)
is what moved this project off the in-kernel `qsee_fingerpr` Register path.
Their stack enrolls and matches a signed trustlet from userspace over QTEE
([fingerprintd](https://forgejo.catcrafts.net/Catcrafts/fingerprintd)).
This tree is that idea on Fairphone 5: a userspace session over `qcomtee`
and `/dev/tee0`, talking to the signed `focal32` trustlet. It is not their
daemon, and it does not use their FP6 opcodes.

## License

GPL-2.0-only for the C session. See `LICENSE`.
