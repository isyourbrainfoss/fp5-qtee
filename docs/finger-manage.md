# Listing and removing fingers

## Commands

```
fp5-qtee list                  # enrolled fingers, asked of the trustlet
fp5-qtee files                 # /mnt/persist/misc files, no trustlet
fp5-qtee backup                # copy /mnt/persist/misc to ~/fp5-qtee-keep/backup/<UTC>
fp5-qtee remove <fid> --yes-really [--allow-last]
```

The session binary takes the same modes directly:

```
sudo fp5-qtee-session list   /lib/firmware/qsee
sudo fp5-qtee-session remove /lib/firmware/qsee <fid> --yes-really [--allow-last]
```

`list` and `remove` need the trustlet to themselves. The wrapper takes
`/tmp/fp5-qtee-phone.lock`, stops `fp5-qtee-unlock.service`, waits until
no other session process is left, runs the session (120 s timeout), and
starts the service again.

## Opcodes

| Op | id | plen | Source |
|---|---|---|---|
| ENUMERATE | `0x2005` | 4 | `ff_trustlet_enumerate` loads `{0x2005, 4}` from the HAL table at `0x97a08` in `fingerprint.default.so` (VT2Y.C.108); Marcus Ramberg's `focaltechqsee` uses the same id |
| REMOVE | `0x2006` | 8 | `ff_trustlet_remove`, table entry `0x97a10`; same in Marcus's driver |
| SET_ACTIVE_GROUP | `0x2007` | path | already sent as SET_GROUP |
| `0x1028` | — | — | not in the HAL table. focal32's switch covers `0x1004..0x1023` and `0x2000..0x2008`; anything else gets the default `-203` |

So the `ENUM rc=-203` seen on every unlock is not a missing SET_GROUP
or auth id. `0x1028` was never the enumerate command. It is harmless
and was left on the unlock path unchanged.

ENUMERATE (from the HAL disassembly):

- request payload +0: capacity (the HAL passes its caller's array size;
  this session sends 5)
- status word: the number of fingers. `1` is not an error.
- payload +0: the same count; payload +4: `count` entries of
  `{u32 gid, u32 fid}` (stride 8). The HAL copies `count * 8` bytes from
  +4 into the framework's `fingerprint_finger_id_t` array and returns
  `-12` when the count is above the capacity. Marcus reads the fid at
  `+8 + 8*i`, the same word.

`fp5_parse_list()` accepts an answer only when the status is `>= 0`, the
payload count equals the status, the count fits the capacity, and no
fid is 0. A trustlet that ignored the command leaves the capacity word
(5) in place, and that is rejected.

REMOVE: payload `{u32 gid, u32 fid}`. Status 0 is success. With the fid
alone in the first word focal32 answers `-200` (Marcus). The removal is
in RAM until SAVE (`0x1014`, flag `0x40000000`, the enroll save) writes
the gallery.

There is no name or date in the trustlet. A finger is an id and a group.
The slot printed by `list` is `fid % 10`, the RAM slot the enroll path
uses (see `group_path()`).

## Storage

The templates are not plain `ff_template_*.bin` files on disk. GPFILE
requests land in `/mnt/persist/misc/<SFS dir>/` (for example
`Wc9lu7HhpDADVWYMRw88zZxgXUmlrsVIzIrCf6-GmUA_Alt/`) under names the
TEE's secure file system chose. One file is not one finger. `list`
prints the objects the gallery load read (`OBJECT read=...`).
`fp5-qtee files` shows the tree with sizes and hashes without opening
the trustlet.

## list is read-only

`list` sets `persist_ro` before the trustlet is opened. The session then
refuses (EROFS, logged as `RO refuse`) every GPFILE write or unlink and
every RPMB write (`0x103`, multi-frame write, single authenticated
write). Reads are served. It sends the same setup as the unlock path,
then SET_GROUP (the gallery load), then ENUMERATE, then FREE_SPI at exit.
It does not send `0x1028`, PRE_ENROLL, ENROLL, REMOVE, or SAVE.

## remove (implemented, unit-tested, not run on the phone)

Order in `remove_mode()`:

1. `main` checks the arguments before anything is opened: one positive
   decimal id, `--yes-really`, nothing else except `--allow-last`
   (`fp5_remove_args`).
2. SET_GROUP, ENUMERATE. The id must be in that list
   (`fp5_remove_check`); the gid comes from the list. The only finger
   left is refused without `--allow-last`.
3. CANCEL (clears a stale enroll), REMOVE `{gid, fid}`. Non-zero status
   stops here, before SAVE.
4. SAVE `0x40000000`, then ENUMERATE again. The id must be gone.

The wrapper also refuses unless the newest `~/fp5-qtee-keep/backup/*`
matches `/mnt/persist/misc` byte for byte (`fp5-qtee backup` makes
one).

Backup before the first real remove:

- `fp5-qtee backup` (copy of `/mnt/persist/misc`, with
  `MANIFEST.sha256`). Copy that directory off the phone too.
- The RPMB side cannot be copied back: SFS keeps a write counter / anti
  rollback state in RPMB. A restored `/mnt/persist/misc` from before the
  remove may be refused if the trustlet's RPMB state moved on during
  SAVE. So check after the remove, with the backup kept, that the other
  finger still matches before deleting the backup.
  `~/repos/fp5-fp-backup-20261010T211513Z/RESTORE.sh` shows how an
  earlier copy was put back.
- The session never performs GPFILE unlinks (`fp5_gp_unlink_performs()`
  is 0). If REMOVE asks to unlink an object, it is logged as
  `GPFILE UNLINK` and left on disk.

Open questions for the first run: whether REMOVE alone already writes
(the HAL calls SAVE separately, so probably not), and whether the
trustlet needs AUTHENTICATOR_ID (`0x2003`) or a HAT first (the HAL's
remove takes only gid and fid, so probably not).

## fprintd / GNOME Settings

Today nothing on this phone exposes the fingers over
`net.reactivated.Fprint`:

- This repo has no fprintd daemon. The unlock watcher and the Finger app
  call the session directly.
- Marcus's libfprint `focaltechqsee` driver does implement `list`
  (ENUMERATE) and `delete` (REMOVE + SAVE `0x60000000`) as a
  device-storage driver, so fprintd on his NixOS stack does list and
  delete. It needs his QSEECOM TEE driver, the misc-device discovery
  change in libfprint, ffsupplicant and his RPMB listener path, and it
  saves to his group path. Pointed at this phone's gallery it would look
  up a different object (docs/ultimate.md item 4).
- `fp5-fingerprint/fprintd/fp5-fprintd.py` (older kernel path) answers
  the D-Bus API from a JSON file; its delete never reached the trustlet.

Plan, smallest first:

1. A system service that owns `net.reactivated.Fprint` (as Catcrafts'
   `fingerprintd` does on FP6) and runs `fp5-qtee-session` for
   `ListEnrolledFingers` (list), `DeleteEnrolledFinger(s)` (remove), and
   later `EnrollStart` / `VerifyStart` (the existing enroll and serve
   modes). It takes the same lock and pauses the unlock service the way
   the wrapper does.
2. fprintd's API is by finger name, the trustlet's by id. Keep a map in
   `/var/lib/fp5-qtee/fingers.json` (`{user, finger_name, fid, enrolled}`),
   written when enroll gets its fid. Ids that ENUMERATE returns and the
   map lacks are shown as "unnamed" and can be named once. That is also
   where a name and date for `list` would live; the trustlet has none.
3. polkit: `net.reactivated.fprint.device.enroll` for delete, as
   fprintd does.
4. Longer term, a libfprint driver on top of the session (Marcus's
   driver with this session's transport and group path) would give
   fprintd itself instead of a lookalike service.

Not done here: it is more than a small change, and delete through it
must wait for the first supervised remove.
