# Fairphone 5 — fingerprint over QTEE

> **Note**
> This is very experimental. Assume nothing here is written, or necessarily read through or fully understood, by a human unless stated, and that includes this text. Use it at your own risk, but you're encouraged to reuse any parts you find useful. The human work here is mainly ideas, testing in reality, and persistence with a vision.

Userspace session for the signed `focal32` trustlet on Fairphone 5
(Qualcomm SM7325 / sc7280). It talks to the trustlet through `/dev/tee0`.
It does not load `qsee_fingerpr`. The Phosh unlock watcher is a demo.
The useful part is how the sensor and the trustlet respond, which is
awkward to find and easy to check once it is written down.

The older in-kernel Register path is
[fp5-fingerprint](https://github.com/isyourbrainfoss/fp5-fingerprint).
A userspace session over QTEE is the approach in Catcrafts'
[Fairphone 6 bring-up](https://catcrafts.net/posts/fairphone-6-postmarketos-fully-working-fingerprint-sensor)
([fingerprintd](https://forgejo.catcrafts.net/Catcrafts/fingerprintd)).
The FP6 opcodes are in the table below, beside the FP5 ones.

Status: **experimental**. Not an official Fairphone or distribution package.

Enroll can reach samples-remaining 0 and write a template. A finger
enrolled that way matched after a reboot on 2026-10-08, and that match
dismissed the Phosh lock screen while the panel was on. The sensor IRQ
does not wake the CPU from suspend. A later enroll replaces the template
whose id ends in the same digit unless the current gallery is loaded
first.

## QTEE

Measured on kernel `7.2.0-nfc-test+`. `CONFIG_QCOMTEE` is unset,
`CONFIG_QCOM_QSEECOM=y`, and tzmem is generic
(`CONFIG_QCOM_TZMEM_MODE_GENERIC=y`). In generic mode
`qcom_tzmem_shm_bridge_create` is the header stub that returns 0.
Small invokes still complete. An out-of-tree `qcomtee` module bound the
firmware's unbound platform device and created `/dev/tee0` without a
kernel rebuild. That module, and the patch against `drivers/tee/qcomtee`,
are not in this tree. On the test phone it also exposes `/dev/qsee_log`,
where `focal32` writes `frame raw` and `interrupt type`. Those strings
are not in the QTEE response buffers.

`CONFIG_QCOM_TZMEM_MODE_SHMBRIDGE` would call
`qcom_scm_shm_bridge_enable()` once during boot. An image with that
option was built and has not been booted here. Fairphone 5
(`fairphone,fp5` / `qcom,qcm6490`) is not on the upstream blacklist
(`sc7180`, `sc8180x`, `sdm670`, `sdm845`, `sm7150`, `sm8150`). The call
is global, and on the blacklisted SoCs it has reset rmtfs. Whether a
booted SHMBRIDGE kernel keeps the modem up on this phone is unknown.
`shm_bridge_create` without enable was accepted on a session buffer
(a handle came back) and the following capture still failed to allocate
the image plane. That mode is for copying the image plane out to Linux.
This session does not need it. Do not call `shm_bridge_enable` from a
module.

Load the trustlet named `focal32`. The signed `focal32.mdt` and
`focal32.b00` through `focal32.b07` are not in this repository. Copy
them from a Fairphone Android vendor image to `/lib/firmware/qsee/`.
Open `/dev/tee0`, take a client environment, open service UID 122, and
call loader `loadFromBuffer`. Load qret 0 is success. qret 11 is
`PIL_ROLLBACK` (the trustlet image version), 12 is `ELF_SIGNATURE`,
16 is `ALREADY_LOADED`. Privileged root op 5 returns result 2, so a log
line `QTEE version 0.0.0` is not a version.

Before the load, register four listeners. One registration per boot.

| id | role | buffer |
| --- | --- | --- |
| `0x7000` | GPFILE | `0x7e000` |
| `0x2000` | RPMB | `0x6400` |
| `11` | time | `0x5000` |
| `0x3000` | SSD | `0x1000` |

SCM may already own RPMB id `0x2000`. The session continues if that
register fails, and requests for that id are not delivered on this
buffer. Do not register the listeners twice on the same boot.

Invoke buffers are a 1536-byte request and a 2048-byte response. A pair
of 2048-byte buffers returns `MAXDATA` (`-95`).

`scripts/load-qcomtee.sh` insmods
`/home/user/fp5-qtee-keep/qcomtee-bridge.ko` (not in this repo). It
exits 1 on any kernel other than `7.2.0-nfc-test+`, exits 1 when
`qsee_fingerpr` is loaded, and does nothing when `qcomtee` is already
loaded. Do not `rmmod` a live `qsee_fingerpr`.

## Wire format

Little-endian. Payload starts at offset `0x10`.

| offset | word |
| --- | --- |
| 0 | opcode |
| 4 | payload length |
| 8 | status. Sent as 0. The trustlet writes the return code here. |
| `0x10` | payload |

On a report, the template id is the word at payload `+0x10` and samples
remaining is the word at payload `+0x24`.

`focal32`'s switch covers `0x1004`..`0x1023` and `0x2000`..`0x2008`.
Any other opcode, including `0x1024` and `0x1028`, returns `-203`.

### Setup, after the trustlet is loaded

The session turns the sensor on first. If
`/sys/class/focaltech_fp/focaltech_fp` is missing it insmods
`/lib/modules/$(uname -r)/extra/focaltech_fp_life.ko` with
`auto_spiclk=1` (that module is not in this repo). It writes `1` to
`vdd`, `spiclk`, and `listen`, then on `/dev/focaltech_fp` issues
`_IO('f', n)` for `n` = `0x07`, `0x05`, `0x02`, `0x03`. The return of
`0x02` is the reset result. It waits 50 ms. Then these thirteen
commands, in order:

| | opcode | name | length | payload |
| --- | --- | --- | --- | --- |
| 1 | `0x0205` | VERSION | 4 | `02 00 00 00` |
| 2 | `0x1011` | SET_KM | 0 | |
| 3 | `0x100d` | SYNC | JSON bytes + 1 | config below |
| 4 | `0x1006` | INIT_SPI | 0 | |
| 5 | `0x1008` | SET_SPI | 4 | speed `8030000` |
| 6 | `0x100a` | PROBE | 1 | one `0` byte |
| 7 | `0x100a` | PROBE | 1 | one `0` byte |
| 8 | `0x100b` | INIT_DEV | 0 | |
| 9 | `0x1022` | CHIP | 0 | |
| 10 | `0x1004` | INIT | 0 | |
| 11 | `0x100f` | CALIB | 0 | |
| 12 | `0x100e` | SYNC_STATS | `0x230` | zeros |
| 13 | `0x1016` | HEALTH | 4 | one `0` word |

SYNC_STATS publishes the stats object. `do_enroll` writes a timestamp
through that pointer. Until this command runs the pointer is NULL and
report event 5 drops the app.

The SYNC JSON is `fp5_sync_config()` in `session/fp5_wire.c`. The fields
that change behaviour: `preferred_device_id` 37777,
`enable_trusted_enrollment` false, `max_enrolling_fingers` 5,
`max_enrolling_samples` 20, `burst_acquisition_num` 3,
`algorithm_log_level` 2, `min_identify_quality_threshold` 40,
`min_identify_coverage_threshold` 70, `other_press_inte` 128,
`dead_pixel_inte` 128, SPI capture rate `8030000`. The same object
names SPI bus 14.

### Match

| | opcode | length | payload |
| --- | --- | --- | --- |
| 1 | `0x2007` SET_GROUP | 4 + path + NUL | a `0` word, then the path. This tree sends an empty path. |
| 2 | `0x1028` ENUM | 0 | returns `-203`. Does not delete a template. |
| 3 | `0x2008` AUTH | `0xe` | stores operation mode 2 (`do_authenticate`). Return code 0 means that store worked. |
| 4 | `0x101f` work mode | 4 | mode `1`. The trustlet names it `FDT_DOWN_DETECT` (chip wait-touch). |
| 5 | `0x101c` query | 4 | zeros. Once at arm, again on each new IRQ, and once if the panel rises during the wait and the IRQ count did not move. |
| 6 | `0x1013` capture | `0x24` | three frames. Burst count at `+0x0c`, `1` at `+0x10`, frame index at `+0x14`, flag `0xC0040002` at `+0x1c`. |
| 7 | `0x1017` report | `0x2e0` | event `5` at `+4`, burst count `3` at `+0x2c8`, flag `4` at `+0x2d8`. |

Send AUTH before the wait. Mode `9` only runs an SPI helper and does
not move the IRQ count, so a match armed with mode 9 never sees the pin.
Enroll still uses mode 9, because enroll has already started the scan.

Before the report, write `0xaaaaaaaa` at payload `+0x10` and
`0xa5a5a5a5` at `+0x24`. A word left over from the capture is then
visible as unread instead of as an id or a remaining-count.

The wait polls `/sys/class/focaltech_fp/focaltech_fp/irq_count` every
20 ms, in 200 ms slices, for up to 90 s. Send all three frames when at
least one has avgv in `(0, 600)`, including a sibling at 600 or above.
A burst with none in range is not reported. While the finger stays
down, run up to three bursts. The first hit returns immediately.

SET_GROUP return code `-2` means the share template is absent. The
finger template can still load. The session treats the invoke as the
success check, so that `-2` does not abort the match.

### Enroll

Load the gallery with SET_GROUP on the empty path before choosing an
id. The new id's last digit is the first empty RAM slot. An empty
gallery is always slot 0, and the new finger then replaces
`/ff_template_0_0.bin`. ENUM does not remove a file. Continuing when
the trustlet did not report a loaded template mints that slot-0 id.

| | opcode | length | payload |
| --- | --- | --- | --- |
| 1 | `0x2000` PRE_ENROLL | 0 | challenge is 8 bytes at the start of the response payload |
| 2 | `0x2001` ENROLL | `0x4a` | challenge at byte 1, authenticator type 2 at `0x1c`, `1` at `0x49` |
| 3 | `0x101f` work mode | 4 | mode `9` |
| 4 | `0x1013` capture | `0x24` | three frames, same layout as match. Keep the burst only when all three are in range. |
| 5 | `0x1017` report | `0x2e0` | event `5`. Samples remaining at payload `+0x24`, from 19 down to 0. |
| 6 | `0x1014` SAVE | 4 | flag `0x40000000`, sent when remaining is 0 |
| 7 | lift | | work mode `2`, report event `6`, work mode `9` |

Trusted enrollment stays off. Turning it on fails the keyed hash and
returns no id. `0x1015` is the short quality command. It returns in
about 5 ms and does not write the finger. Do not put flag `0x40000000`
on `0x1015`. A real save is `0x1014` and takes on the order of 200 ms
(the Android capture measured 186 ms). This session counts the save
when a template write was seen, or an RPMB command `0x103` came back
with result 0, and the command took at least 30 ms. The enroll that
matched after reboot was a GPFILE write with `rpmb_cmd` 0.

`/data/vendor_de/0/fpdata` is a different object. A SET_GROUP to that
path drops the RAM copy.

## Device id

The id the silicon reports is 37841 (`0x93d1`). It is not in `focal32`'s
profile table, so the feature preprocessor is never set and a template
enrolled under 37841 has empty feature pointers. The SYNC config sends
`preferred_device_id` 37777 (`0x9391`), which is the Android profile and
the one that matches.

## FP5 and FP6 opcodes

| | FP5 `focal32` | FP6 `focal64` |
| --- | --- | --- |
| report | `0x1017` | `0x1018` |
| work mode | `0x101f` | `0x1020` |
| save | `0x1014`, length 4, flag `0x40000000` | `0x1014` |
| quality / update | `0x1015` does not write the finger | `0x1015` is update-template |

Sending `0x1018` as the FP5 report is the wrong command. What `0x1018`
does inside `focal32` is unknown.

## Finger down

The sensor is an FT9362, board node `focalfp_ft9362`. IRQ is gpio 34,
reset is gpio 35, vdd is gpio 60.

A finger-down is interrupt type `0x2` with the ESD flag clear.

| type | meaning |
| --- | --- |
| `0x2`, ESD clear | finger down. The only type this session captures. |
| `0x4` | lift |
| `0x212` | reset, invalid, and the down nibble. Capturing it stores an empty frame (avgv around 966). `(itype & 7) == 2` is the test that lets this through. |
| `0x210`, `0x10` | leftovers |
| `0x0`, `0x1` | idle |

Avgv in `(0, 600)` is in range. A real finger image, in the Android
capture and in the postmarketOS logs, sat around 200–350.

Already down. The IRQ count is sampled before AUTH, so a finger that is
already on the sensor sits inside that baseline and the wait does not
see it. Query once at arm. An exact `0x2` is captured. The report then
raises the IRQ count, and the next query returns idle `0x0` because the
confirming query already consumed the `0x2`. That idle result is not a
lift. Keep the remaining bursts, up to three, while the finger stays
down. A lift (`0x4`), an ESD, or any other type stops the retries.

Calibration. Command 11 of setup runs CALIB before any wait. The wake
press is a new edge on a sensor calibrated with no finger on it.
Starting a new session while the finger is already down runs CALIB
under the finger. The demo watcher holds one session for 1800 s so it
does not tear the session down and calibrate again during a wake press.
What image CALIB stores with a finger on the glass is unknown.

The IRQ is not a wake source on this kernel. A press cannot resume the
CPU from s2idle. The power button can. `enable_irq_wake()` on gpio 34
would make the sensor a wake source. It is not in this tree. While the
CPU is awake, the session still queries once if the panel turns on and
the IRQ count did not move.

## Return codes and scores

AUTH `0x2008` return code 0 means the mode word was stored.

A hit is report return code 0 and a template id other than 0,
`0xaaaaaaaa`, and `0xa5a5a5a5`.

Report return code `-11` with id 0 is the trustlet's no-match. The
diary line is `authentication failed`. A hit's diary line is
`authenticated fid`. A failing compare has been logged together with
`scan score:0x01000000`. That word is not a similarity percentage.

With `algorithm_log_level` 2 the trustlet prints
`FtVerifySubTemplate() score = N`. Score 0 is an empty feature pointer,
which is what an enroll under device id 37841 produces. Any other
score, including a negative one, means the trustlet compared the image,
and this session counts that press as a real try. The accept or reject
is the report code and the id. The numeric threshold inside the
trustlet is unknown.

The first listen after the panel is already on ignores one no-match,
and only when every `FtVerifySubTemplate` score on it was 0. The window
is `FP5_QTEE_WAKE_GRACE_MS` (default 1000), measured from the
`AUTH N arm` line, not from panel-on. A down that is already pending
shows up about 300 ms after that line. A nonzero score inside the
window is a real try. A hit inside the window still unlocks. After the
session has armed, one "seat unlocked" or "lock unknown" reading has to
last 2 s before the listen is dropped.

Android, from a capture on this phone on 2026-10-09. A hit is one
3-frame burst. Two screen-off hits finished in 146 ms and 157 ms
(transmit about 40 ms, compare about 8–10 ms) at integration time 144.
The fastest hit in that log was 118 ms at integration time 128. A miss
while the finger stayed down ran three bursts and one framework reject,
about 390–460 ms. A short press stopped after one or two bursts. A miss
did not wake the screen. A hit woke it with `WAKE_REASON_BIOMETRIC`.
This session's capture payload does not carry an integration time. The
SYNC JSON sets `other_press_inte` and `dead_pixel_inte` to 128. Whether
the trustlet uses 128 on a match frame is unknown. Android's detect
client was queued during lockout and cancelled without arming the chip.
Screen-off authentication is the authentication client.

## Secure storage

One gallery in RAM. The trustlet's filename is
`%s/ff_template_%d_%d.bin`. With an empty base that is
`/ff_template_<gid>_<fid % 10>.bin`. The load loop opens indices
`0 .. max_enrolling_fingers-1`. The config sends 5, so that is 0 through
4. An id whose last digit is 5–9 is not opened. The share file is
`%s/ff_share_template_%d.bin`. `fingerprint_remove` logs
`removing 'ff_template_<gid>_<index>.bin'`. Enroll itself does not
remove a file.

The bytes on disk are GP/SFS objects, not files with those names. The
session mounts the `persist` partition and serves GPFILE from it. A
real unlink destroys the segment. This session's unlink performs
nothing, and its writes do not truncate. Do not program or erase the
RPMB key. Listener command `0x101` and `req_resp` 1 are refused.

Whether booting the other OS invalidates an enrolled finger is unknown.
The Android finger in the 2026-10-09 capture was a different id from
the postmarketOS finger, and this session did not match it. A
postmarketOS finger that disappeared on a later enroll was the slot-0
replace above, on the same OS. Load qret 11 (`PIL_ROLLBACK`) is a check
on the trustlet image, not on the template objects. The note that FP6
templates are sealed to the stock anti-rollback describes that phone's
stack and was not measured here.

## Android behaviour this session follows

From the same 2026-10-09 capture, and from the lockout rules in
`app/fp5_qtee_unlock.py`:

- Match captures three frames and sends all three when one is in range.
  Enroll requires all three.
- A miss is retried while the finger stays down, up to three bursts,
  and is then one strike. An empty press is not a strike. Five rejected
  fingers lock fingerprint unlock for 30 s. Twenty lock it until the
  PIN is used. The PIN clears the lockout. The PIN is also required
  after boot and at least every 72 h.
- A partial press does not count. A miss never wakes the screen. Only
  a hit can unlock, and the panel has to be on or come on within about
  a second.
- Haptics on the aw86927. A match is one 20 ms pulse, theme event
  `fp5-qtee-match`, magnitude 1.0. A miss is 30 ms on, 100 ms off,
  30 ms on (`fp5-qtee-miss`), so the second pulse starts 130 ms after
  the first, at the same magnitude. The driver's gain register is
  `strong_magnitude * 0x80 / 0xffff`, so magnitude `0xffff` is register
  `0x80` when the device gain is also `0xffff`. feedbackd writes gain
  `0xc000` (75%) when it opens the motor, which would store `0x60`.
  The player sets the device gain to `0xffff` for the pulse and writes
  `0xc000` back. Lockout adds no vibration. The silent feedbackd
  profile plays nothing. A counted enroll sample uses the match click.
  The theme is `app/fp5-qtee-feedback.json`. It parents `default`.

Still different from that capture: gpio 34 is not a wake IRQ, and the
capture command does not set integration time 144. The Android log did
not gate authentication on the front proximity sensor. This session
does not either.

## Reproduce

On the phone, kernel `7.2.0-nfc-test+`, with `qsee_fingerpr` unloaded:

```sh
scripts/load-qcomtee.sh
```

Copy `focal32.mdt` and `focal32.b00` through `focal32.b07` to
`/lib/firmware/qsee/`. Build the session on the phone. Alpine gcc is
enough. pthread is in musl, so there is no `-lpthread`. `logf` and
`logl` warn against libm. Leave those names.

```sh
make -C session
make -C session test
python3 app/test_fp5_qtee_unlock.py
python3 app/test_fp5_qtee_coach.py
python3 app/test_fp5_qtee_haptic.py
python3 app/test_fp5_qtee_boot.py
./session/fp5-qtee-session unlock
```

`make -C session test` builds and runs the wire, image, and command
tests. Those three do not need the phone or the trustlet.

## Demo

The Phosh pieces only call the session above.

- `app/fp5-qtee-app.py`, launched by `app/fp5-qtee-finger`. Idle until
  Enroll or Match.
- `app/fp5_qtee_unlock.py`. One `unlock` session while the seat is
  locked. `loginctl unlock-session` runs only on a real hit.
- `app/fp5-qtee-feedback.json` for the two vibration events.
- `FP5_QTEE_WARM` and `FP5_QTEE_DARK_ARM` default off. Warm keeps one
  `serve` session and reads `auth`, `cancel`, and `quit` on stdin.
  Dark-arm may write the backlight `bl_power` file and the DSI `dpms`
  file on a hit. Neither flag wakes a suspended CPU.

## License

GPL-2.0-only for the C session. See `LICENSE`.
