# Screen-on race after a dark fingerprint hit (black/flickering screen)

Seen on BengalOS 0.26.1116.1, 18 Nov 2026 at 10:43:14 and 14:40:25 CET.

The FP5 sensor is in the power key. One physical press was both a dark
fingerprint hit and a KEY_POWER. The watcher ran `loginctl unlock-session`
and then `org.gnome.ScreenSaver.SetActive(false)` within 20-40 ms of the key
press that Phosh also handles (screen toggle). Phoc then switched DSI-1 on
and off about twice a second (21 modesets at 10:43, 96 at 14:40-14:41,
each cycle with two ScreenSaver ActiveChanged reactions in
gsd-usb-protection) until a later power key press at 10:43:23 / 14:41:07.
All other 70+ dark-hit screen-on requests that day (key press >= 0.14 s
away, or no key press) gave 0-2 modesets. The later
`Atomic commit failed: Device or resource busy` lines (14:42:40-14:43:31)
were while the screen was on and in use; that message shows up every day
and is not the cause.

Fix (WarmLoop.screen_on_after_hit): after a dark unlock, poll the panel for
FP5_QTEE_SCREEN_ON_SETTLE_MS (default 350 ms). If the power key turned it
on, do not call SetActive(false). Never ask more than once in 2 s.
Cost: a touch-only dark unlock (no click) turns the screen on up to
350 ms later. The haptic still fires on the match, before this.

Install (as root, from this dir): `./install.sh`. It checks SHA256SUMS and
that the installed copy is the expected base (BASE.sha256, sha cd821291 =
origin/main afa5687), keeps a .orig copy, replaces only
/var/lib/fp5-qtee-loginpin/fp5_qtee_unlock.py and restarts only the fp5
user unit fp5-qtee-unlock.service. Undo: `./rollback.sh`.
