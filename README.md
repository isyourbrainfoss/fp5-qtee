# Fairphone 5 — fingerprint over QTEE

Userspace session for the signed `focal32` trustlet on Fairphone 5
(Qualcomm SM7325 / sc7280, postmarketOS). A small Finger app enrolls and
matches through `/dev/tee0`. It does not load `qsee_fingerpr` and it does
not unlock Phosh.

The older in-kernel Register path is
[fp5-fingerprint](https://github.com/isyourbrainfoss/fp5-fingerprint).

Status: **experimental**. Not an official Fairphone or distribution package.

## Where this stands

Enroll can reach samples-remaining 0, and the same trustlet can reopen that
template on the same boot. Match sends authenticate first, then arms chip
wait-touch (work mode 1). It captures only an exact finger-down
(`itype` `0x2`, not an ESD or leftover). A hit is an image report that
succeeds with a real template id. Authenticate-command success, report
failure, fid 0, and the poison ids are misses.

Sync config sends `preferred_device_id` 37777, which is focal32 profile
`0x9391` (the Android config). The detected id 37841 is not in that profile
table, so a template enrolled under 37841 has no feature pointers. Enroll
again on this session before matching. `algorithm_log_level` is 2 so the
trustlet can print the subtemplate distance. A match of the enrolled finger
against a different finger has not been witnessed from this tree.

The signed `focal32` image is not in this repository. See
[Firmware](#firmware) for how to install it.

## Firmware

The session loads `focal32.mdt` and `focal32.b00` … `focal32.b07` from
`/lib/firmware/qsee` (or the directory given as its second argument; the
Finger app passes `/lib/firmware/qsee`). The files are signed proprietary
blobs. They are not in this repository and must never be committed
(`.gitignore` blocks `*.mdt`, `*.bNN` and `*.mbn`).

`scripts/fetch-focal32-firmware.sh` finds them in a Fairphone 5 stock image,
checks them, and copies them in:

```sh
scripts/fetch-focal32-firmware.sh --dry-run FP5-XXXX-factory.zip
sudo scripts/fetch-focal32-firmware.sh FP5-XXXX-factory.zip
sudo scripts/fetch-focal32-firmware.sh --dest /some/dir /path/to/extracted/vendor
```

The source can be an extracted directory, a `.zip`/`.tar*` archive, or a
FAT (modem/NON-HLOS), ext4, EROFS, Android sparse or super image. Images
inside an archive are opened too. On Qualcomm vendor images the trustlets
usually live in `/vendor/firmware_mnt/image` (the modem partition's `image/`
directory) or `/vendor/firmware`. Opening an image needs the matching tool:
mtools or 7z, e2fsprogs (`debugfs`), erofs-utils (`fsck.erofs`), `simg2img`,
`lpunpack`. The script names the one it is missing.

Fairphone publishes the FP5 factory packages on its
[manual install page](https://support.fairphone.com/hc/en-us/articles/18896094650513).
The postmarketOS `firmware-fairphone-fp5` package (built from
[FairBlobs/FP5-firmware](https://github.com/FairBlobs/FP5-firmware)) carries
the modem, DSP, GPU and similar firmware, but not `focal32`.

**Checksums (trust on first use).** Every file is checked against
`scripts/focal32.sha256` before anything is copied. The script refuses to
install when an entry is missing or a hash differs. The committed file has
**no real hashes yet**, only placeholders, so the script refuses until it is
filled in. Fill it in from a copy you trust, either:

- run `sha256sum focal32.mdt focal32.b0[0-7]` in `/lib/firmware/qsee` on a
  phone where focal32 already loads and enrolls, and paste the nine lines;
  or
- run the script once with `--record` against a source you trust. It writes
  the nine hashes into the file (only while it has no real entries) and
  installs. Review and commit the result.

After that, any other source has to match those hashes.

Install with `sudo` when the destination is `/lib/firmware/qsee`. Host test,
with synthetic files only: `scripts/test_fetch_focal32_firmware.sh`.

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
