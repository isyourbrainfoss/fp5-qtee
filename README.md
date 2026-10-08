# Fairphone 5 — fingerprint over QTEE

Userspace session for the signed `focal32` trustlet on Fairphone 5
(Qualcomm SM7325 / sc7280, postmarketOS). A small Finger app enrolls and
matches through `/dev/tee0`. It does not load `qsee_fingerpr` and it does
not unlock Phosh.

The older in-kernel Register path is
[fp5-fingerprint](https://github.com/isyourbrainfoss/fp5-fingerprint).

Status: **experimental**. Not an official Fairphone or distribution package.

## Where this stands

On 2026-10-08 the Finger app enrolled a finger (samples-remaining 0, template
written, hero SAVED) and then matched it. The enrolled finger returned
template id 1657488200 with a real finger-down (`itype` `0x2`) and
`auth success`. Other presses on the same boot returned fid 0,
`authentication failed`, and hero NO MATCH. Phosh unlock is not wired up.
Reboot persistence of that template was not checked.

Log scans, the time-listener reply, and the group-path buffer stop at the
buffer they were given. A group path that does not fit is refused before it
is sent. The phone is still running the 2026-10-08 session binary.

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
session/   fp5-qtee-session, wire helpers, and their host tests
app/       Phosh "Finger" app (idle until Enroll or Match)
scripts/   load-qcomtee.sh for the test phone
```

## Kernel

`/dev/tee0` comes from an out-of-tree `qcomtee` module. The `7.2.0-nfc-test+`
kernel used here has `CONFIG_QCOMTEE` unset and `CONFIG_QCOM_QSEECOM=y`.
The module on the test phone also exposes `/dev/qsee_log` (avgv and interrupt
type). That module binary and its patch against mainline `drivers/tee/qcomtee`
are not in this tree.

`scripts/load-qcomtee.sh` refuses to load when `qsee_fingerpr` is already
loaded, and it is a no-op when `qcomtee` is already loaded. Do not `rmmod` a
live `qsee_fingerpr`. One listener registration per boot. Do not program or
erase the RPMB key.

## Build

On the phone, Alpine gcc is enough. pthread is in musl, so there is no
`-lpthread`. `logf` and `logl` warn against libm; leave those names.

```sh
make -C session
python3 app/test_fp5_qtee_coach.py
make -C session test
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

## License

GPL-2.0-only for the C session. See `LICENSE`.
