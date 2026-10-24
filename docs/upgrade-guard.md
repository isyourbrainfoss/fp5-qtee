# Upgrade guard: keep `apk upgrade` from silently undoing the FP5 fixes

## Problem

Several FP5 fixes replace or add files that a Nura edge `apk upgrade` can change:

* `resuspend` swaps `/usr/libexec/gsd-power` for a patched build. That file belongs to
  `gnome-settings-daemon`, so any upgrade or `apk fix` of that package puts the
  stock binary back. The phone then stops re-suspending after an unattended wake
  (2026-10-23 bug). This already happened once.
* `fprintd` installs `/usr/share/polkit-1/actions/net.reactivated.fprint.device.policy`, a
  path a real `fprintd` package would own.
* The custom kernel `7.2.9` (v2, `boot_b` 2645ea09) shares its release, and so
  `/lib/modules/7.2.9`, with the distro `linux-postmarketos-qcom-sc7280` 7.2.9 package.
  A package rebuild (`7.2.9-r1`) replaces the modules under the running custom kernel.
  The v4 kernel (`7.2.9-fp5`, see grok_build `artifacts/kernel-7.2.9-fp5-v4-usb-20261024`)
  removes that clash. Out-of-tree modules in `extra/` are kept by hand.
* OpenSSH was replaced mid-transaction on 2026-10-08 and refused logins until a reboot.

## What is here

`scripts/upgrade-guard/`:

| File | Installed as | Role |
|---|---|---|
| `fp5-upgrade-guard` | `/usr/local/sbin/` | `snapshot`, `check`, `fix`, `pin`, `unpin` |
| `zz-fp5-upgrade-guard.sh` | `/etc/apk/commit_hooks.d/` | After every apk transaction (`post-commit`), runs `fix` and logs to `/var/log/fp5-upgrade-guard.log`. It prints FAIL lines on the apk terminal and never fails the transaction. apk-tools 2 and 3 both run these hooks. `--no-scripts` / `--no-commit-hooks` skip them. |
| `fp5-upgrade-guard.service` | `/etc/systemd/system/` | `check` once per boot (journal) |
| `install.sh`, `uninstall.sh` | | install / remove (no reboot) |
| `test-upgrade-guard.sh` | | desk test in a fake root (20 checks) |

### `snapshot`

Copies each guarded file that exists into `/var/lib/fp5-upgrade-guard/store/` and records its
sha256 and mode in `manifest`. It also stores the baseline (`baseline`): kernel release,
distro kernel package version, `gnome-settings-daemon` version, the patched gsd-power sha, and
the `modules.dep` sha. The list is `DEFAULT_PATHS` in the script (cell-backoff, q6voiced drop-in, call
path agent, fprintd daemon / D-Bus / polkit, GNOME Settings schema shim, backlight units,
paging-wake unit) plus every `/lib/modules/$(uname -r)/extra/*.ko*`, plus anything in
`/var/lib/fp5-upgrade-guard/extra-paths`.

**Take it only while everything works.** The snapshot is what `fix` restores.

### `check` / `fix`

| Detects | `check` | `fix` |
|---|---|---|
| stock gsd-power, same `gnome-settings-daemon` version as the patch | FAIL | re-installs the saved patched binary (atomic rename), SIGKILLs gsd-power so systemd respawns it |
| stock gsd-power, **new** `gnome-settings-daemon` version | FAIL | **refuses** (a 51.0 binary on 52.x libs is unsafe). Rebuild with `resuspend/build-gsd-power.sh` for the new version, then `snapshot` again. |
| guarded file missing or changed (names the apk owner if a package now owns it) | FAIL | restores the saved copy with its mode, `daemon-reload` |
| `/lib/modules/$(uname -r)` missing | FAIL | — |
| running a release that a distro package owns (`7.2.9`) | WARN | — |
| distro kernel package version changed since snapshot while running that release | FAIL (modules may now be the distro build) | — |
| out-of-tree module in `extra/` missing or changed | FAIL | restores it |
| modules failing BTF validation this boot | FAIL | — |
| `deviceinfo_flash_kernel_on_update` not `false` | FAIL | — |
| `gnome-settings-daemon-mobile` installed | FAIL | — |
| `sshd -t` fails | FAIL | — |

Exit: 0 ok, 1 warnings, 2 failures.

### `pin` (optional, stronger)

`fp5-upgrade-guard pin` writes `gnome-settings-daemon=<installed version>` into `/etc/apk/world`.
apk then keeps that version. An upgrade that needs a newer one (for example a Phosh bump)
**stops with an unsatisfiable-dependency error** rather than replacing the binary. That stop is
the signal to rebuild the patch. `unpin` restores the plain name. The hook (re-apply
after the fact) is the default. Pinning trades silent breakage for blocked upgrades.

A local apk with a higher `pkgrel` was considered and not used. It would have to carry
all of `gnome-settings-daemon` (not just gsd-power), it would hide every real upstream
update behind a manual rebuild, and the distro's next `-rN` would win again anyway.

## Install (on the phone, later; nothing has been installed)

    scp -r scripts/upgrade-guard fp5-pmos:fp5-qtee-keep/upgrade-guard
    ssh fp5-pmos 'cd fp5-qtee-keep/upgrade-guard && sh install.sh --snapshot'

Remove with `sh uninstall.sh` (`--purge` also drops the saved copies).

## Not covered

* Files outside the list. Add them to `extra-paths`, then run `snapshot`.
* A new `gnome-settings-daemon` needs a new patched build (manual).
* Upstreaming: the gsd-power fix belongs in GNOME. Lab rule: no AI-authored MR. A human
  re-authors it.
* Tested on desk with bash as `/bin/sh` in a fake root. Not yet run under busybox ash or on the phone.
