# Template learning + warm mode (phone kit)

Kit directory on the phone: `/home/user/fp5-qtee-keep/learn-20261021/`.

| file | what |
| --- | --- |
| `fp5-qtee-session.C` | match-tuning session with Android-style template learning, epoll finger wait, less per-command log work (sha in `C.sha256`) |
| `fp5-qtee-session.A` | the previous live binary, fb4ee59b (main 076720f) |
| `deploy.sh` | checks the storage backup matches, stops the watcher, installs C, adds `warm.conf` (`FP5_QTEE_WARM=1`), starts the watcher |
| `restore.sh` | puts A back, removes `warm.conf`, restores the drop-ins saved in `watcher-config.before/`, starts the watcher |
| `restore.sh --storage <backup>` | also puts `/mnt/persist/misc` back from `<backup>/misc` with the owners in `<backup>/OWNERS.txt`; the current store is saved first to `~/fp5-qtee-keep/backup/<UTC>-before-restore` |
| `fp5-qtee` | finger-manage wrapper (`list`, `files`, `backup`) |
| `history.txt` | one line per deploy/restore |

Storage backup taken before the deploy:
`/home/user/fp5-qtee-keep/backup/20261021T202431Z` (phone) and
`/home/kb/repos/grok_build/fp5/artifacts/fp-backup/20261021T202431Z` (desk).

RPMB caveat: the TEE keeps an anti-rollback counter in RPMB. After a
learning save, the old files can be refused on load (finger list empty).
Then the fingers have to be enrolled again.

What to look for in `~/fp5-qtee-keep/logs/finger-serve-*.txt` after an unlock:

    LEARN <n> update hit=1 rc=0 changed=<0|1> ...
    LEARN <n> save mask=0x60000000 rc=0 ms=... template=1 ...
    TIME <n> caps <ms> scan=1 tee=<ms> cmd=<ms>
    IRQWAIT epoll /dev/focaltech_fp
