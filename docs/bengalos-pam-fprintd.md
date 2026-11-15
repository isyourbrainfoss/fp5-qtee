# Fingerprint in polkit/run0 prompts on BengalOS (pam_fprintd)

## Why

The Phosh lock screen uses the unlock watcher (`fp5-qtee-unlock`), not
PAM, so it works without any PAM module. Prompts after unlock (pkexec,
run0, GNOME Settings, other polkit dialogs) go through the polkit agent
and the `polkit-1` PAM service. BengalOS 0.26.1114.1 has no
`pam_fprintd.so` and no PAM file that names it, so those prompts only
ever ask for the password, even though `fp5-fprintd` serves
`net.reactivated.Fprint`.

## What the sysext ships

`sysext/build-sysext.sh` puts Debian's PAM module into the image through
`sysext/pam-fprintd.sh`:

| File in the image | From |
|---|---|
| `/usr/lib/aarch64-linux-gnu/security/pam_fprintd.so` | `libpam-fprintd_1.94.5-4+b2_arm64.deb` (Debian forky/sid), sha256 `9497ad30…988de`, fetched from snapshot.debian.org or deb.debian.org and verified |
| `/usr/share/doc/libpam-fprintd/copyright` | same .deb |
| `/usr/share/fp5-qtee/pam/polkit-1.example` | `pam/polkit-1.example` in this repo |

Forky/sid matches the 0.26.1114.1 base (glibc 2.43-7, libpam 1.7.0,
`libsystemd.so.0.45`, polkitd 128-1). The module needs only
`libpam.so.0`, `libsystemd.so.0` and `libc.so.6` (GLIBC_2.38,
LIBSYSTEMD_237, LIBPAM_1.0), all in the base image. The `fprintd`
daemon is **not** shipped: `fp5-fprintd` owns `net.reactivated.Fprint`.
`/usr/share/pam-configs/fprintd` is not shipped either, so
`pam-auth-update` cannot add the module to `common-auth` (that would put
it in front of every login, including the lock-screen PIN).

Nothing is installed into `/etc`. Merging the sysext alone changes no
PAM stack.

`extension-release.<image name>` keeps the fields of the feed's
0.26.1114.1 image (`ID=bengalos`, `VERSION_ID`, `ARCHITECTURE=arm64`,
`SYSEXT_SCOPE=system`, `EXTENSION_RELOAD_MANAGER=1`). Build the image for
the VERSION_ID the phone will run, or it will not merge after a
sysupdate.

## Turning it on (owner step, as root)

Prerequisites:

- The sysext with `pam_fprintd.so` is merged:
  `ls /usr/lib/aarch64-linux-gnu/security/pam_fprintd.so`.
- The unlock watcher lets go of the sensor while the phone is unlocked.
  Use the user drop-in
  `~/.config/systemd/user/fp5-qtee-unlock.service.d/30-fp5-release-when-unlocked.conf`:

      [Service]
      Environment=FP5_QTEE_KEEP_WARM=0

  then `systemctl --user daemon-reload && systemctl --user restart
  fp5-qtee-unlock`, and check right away that the lock screen still
  unlocks with a finger. This is the setup that handed the sensor over on
  the phone (2026-11-01). The newer want-sensor handshake (watcher yields
  while `/run/fp5-fprintd/want-sensor` exists) is the default since
  2026-11-02; if fprintd reports "another fp5-qtee-session is running",
  the drop-in above is the fix. Either way `fp5-fprintd` needs free
  4 MiB contiguous blocks for a new session (`/proc/buddyinfo`,
  order 10). It compacts memory (and drops caches) first and only takes
  the sensor when enough are free; otherwise the prompt falls through to
  the password.

Keep a root shell open in another session while you switch, then:

    d=/var/lib/fp5-backup/$(date +%Y%m%d)-polkit-fprint
    mkdir -p "$d"
    cp -a /usr/lib/pam.d/polkit-1 "$d/usr-lib-pam.d-polkit-1"
    [ -e /etc/pam.d/polkit-1 ] && cp -a /etc/pam.d/polkit-1 "$d/etc-pam.d-polkit-1"
    install -m 0644 /usr/share/fp5-qtee/pam/polkit-1.example /etc/pam.d/polkit-1
    diff <(grep -v '^#' /etc/pam.d/polkit-1 | grep -v pam_fprintd) \
         <(grep -v '^#' /usr/lib/pam.d/polkit-1)    # must print nothing

Test from the user's Phosh session (unlocked): `pkexec true` and
`run0 true`. Each should ask for a finger first; a miss or 5 s without a
touch falls through to the password field.

`/etc/pam.d/polkit-1` fully shadows `/usr/lib/pam.d/polkit-1`. Recheck
it with the `diff` above after every BengalOS update and copy any change
in the stock file into it.

run0 asks polkit for authorization, so it goes through `polkit-1`. Its
session PAM service `systemd-run0` is not changed.

## Rollback

    rm /etc/pam.d/polkit-1

(or restore the copy from the backup directory). No restart is needed;
PAM reads the file on each prompt.
