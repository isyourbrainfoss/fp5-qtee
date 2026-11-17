# Direct fingerprint haptic (BengalOS login-pin watcher)

Builds on deploy/haptic-on-match (4408e9e). fe63060 reads feedbackd's
profile (global, org-fp5-qtee app profile, max-haptic-strength) and opens
the aw86927 evdev node at arm and finger-down, then starts the first pulse
in the watcher's main thread on AUTH HIT (after the logind LockedHint
check), before unlock-session and screen-on. Silent plays nothing; a
missing or stale (>15 s) prepare falls back to fbcli. Also used for the
miss / too-fast patterns. FP5_QTEE_HAPTIC_DIRECT=0 disables it.

Files are the branch's app/fp5_qtee_unlock.py applied to the installed
login-pin copy (differs by one comment line) and app/fp5_qtee_haptic.py
(88364f0 pulse lengths = fp5-strong theme). The haptic module did not
exist in /var/lib/fp5-qtee-loginpin before; rollback removes it again.
