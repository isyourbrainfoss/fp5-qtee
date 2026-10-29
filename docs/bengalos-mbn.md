# Firmware path: prebuilt focal32.mbn (BengalOS)

**Status:** not tested on the phone yet.

## What changed

The session used to read the split trustlet from `/lib/firmware/qsee`,
which means `focal32.mdt` plus `focal32.b00` … `focal32.bNN`, and then build the
image in memory. BengalOS's `droid-juicer` already squashes the split files
into a single ELF, `/var/lib/firmware/qsee/focal32.mbn`, and `/usr` (and so
`/lib/firmware`) is read-only there.

`session/fp5_fw.c` now picks the firmware directory in this order:

1. `$FP5_QTEE_FW_DIR`, if it is set.
2. The directory given on the command line (`fp5-qtee-session <mode> DIR`).
3. The default, `/var/lib/firmware/qsee`.

In that directory it tries:

1. `focal32.mbn`, used only if it starts with the ELF magic.
2. If that fails, `focal32.mdt` plus `focal32.b00`, `focal32.b01` and so on (up to 16). It stops at the first missing `.bNN`.
3. Only when the default directory is in use: the same two steps in the old `/lib/firmware/qsee`. Nura keeps working without any change.

## Checking that both paths give the same bytes (on the phone)

```
/usr/libexec/fp5-qtee/fp5-qtee-load --assemble /lib/firmware/qsee /tmp/focal32.from-split
sha256sum /var/lib/firmware/qsee/focal32.mbn /tmp/focal32.from-split
cmp /var/lib/firmware/qsee/focal32.mbn /tmp/focal32.from-split && echo SAME
```

`--assemble` uses only the split files and refuses to overwrite an existing
output file. If the bytes differ, do not load either image. Compare the
ELF headers first (`readelf -lW`). droid-juicer may pad segments in a
different way.

## Other BengalOS changes in the same branch

- There are no `uname -r` checks. The session and `load-qcomtee.sh` look at what the
  kernel has (`/dev/tee0`, the qcomtee driver, `/sys/class/misc/focaltech_fp`).
- Sensor: the in-tree `focaltech-fp` misc driver (`/dev/focaltech_fp`) is used when
  `/sys/class/misc/focaltech_fp/device/firmware_name` exists. Otherwise the session
  uses the old `fp_life` class driver.
- No sudo: the `fp5-qtee.socket` system socket, together with `fp5-qtee-broker`, checks
  polkit for each connection and then runs the session as root. The user
  watcher and the Finger app talk to it through `fp5_qtee_transport.py`. Set
  `FP5_QTEE_TRANSPORT=sudo` to get the old behaviour.
- The service sets `FP5_QTEE_MOUNT_PERSIST=0`, because BengalOS mounts persist itself
  (`mnt-persist.mount`, read-only for now). While persist is read-only,
  enroll and remove cannot save templates. That is the owner's call: see
  `fingerprint-prep.md`, persist plan.
