"""Turn fp5-qtee-session log lines into a finger prompt.

A scored enroll is samples-remaining 0, a template write, and a save that
lasted at least 30 ms. A match is two AUTH HIT results in one session.
Interrupt edges and empty frames never change the prompt on their own.
"""

from __future__ import annotations

import re

_WAIT = re.compile(r"^wait finger sample=(\d+)")
_REM = re.compile(r"^rem=(\d+) sample=(\d+)")
_DONE = re.compile(r"^enroll done rem=(\d+) wrote=(\d+) save_ms=(-?\d+)")
_AUTH_ARM = re.compile(r"^AUTH (\d+) arm ")
_AUTH_HIT = re.compile(r"^AUTH HIT (\d+) ")
_AUTH_FAIL = re.compile(r"^AUTH FAIL (\d+) ")
_AUTH_NO = re.compile(r"^AUTH (\d+) no finger")
_AUTH_EMPTY = re.compile(r"^AUTH (\d+) empty ")
_AUTH_SKIP = re.compile(r"^AUTH (\d+) skip leftover ")
_AUTH_DOWN = re.compile(r"^AUTH (\d+) REAL DOWN ")
_PAIR = re.compile(r"^auth pair (\d+) (\d+)")
_SCORE = re.compile(r"FtVerifySubTemplate\(\) score = (-?\d+)")
_EXIT = re.compile(r"^session_exit:(-?\d+)")
_REJECT = re.compile(r"^PRESS REJECT ")
_INVOKE = re.compile(r"invoke rc=(-?\d+) qret=0x([0-9a-fA-F]+)")
_STUCK = ("STARTING", "PRESS", "HOLD", "LIFT", "SAVING")


class Coach:
    def __init__(self) -> None:
        self.mode = ""
        self.hero = "READY"
        self.sub = (
            "Tap Enroll, then press the power button when it says PRESS. "
            "Same finger each time. This does not unlock the phone."
        )
        self.style = "idle"
        self.detail = ""
        self.rem: int | None = None
        self.sample: int | None = None
        self.running = False
        self.done = False
        self.ok = False
        self.saved = False
        self.matched = False
        self.saw_no_finger = False
        self.exit_code: int | None = None
        self.scores: list[int] = []

    def begin(self, mode: str) -> None:
        self.mode = mode
        self.running = True
        self.done = False
        self.ok = False
        self.saved = False
        self.matched = False
        self.saw_no_finger = False
        self.exit_code = None
        self.scores = []
        self.rem = None
        self.sample = None
        self.detail = ""
        self._set(
            "STARTING",
            "Loading the reader. Keep your finger off the power button.",
            "idle",
        )

    def on_line(self, line: str) -> None:
        line = line.strip()
        if not line or not self.running:
            return
        if line.startswith("loadFromBuffer ") and "success" not in line:
            self.detail = line
            self._set("FAILED", "The fingerprint program did not load.", "warn")
            return
        if line.startswith("loadFromBuffer ") and "success" in line:
            self.detail = line
            self._set(
                "STARTING",
                "Reader loaded. Preparing the sensor. Hands off.",
                "idle",
            )
            return
        if line.startswith("firmware missing: focal32 needs"):
            self.detail = line
            self._set(
                "FAILED",
                "The focal32 firmware is not on this phone. "
                "Install it with scripts/fetch-focal32-firmware.sh.",
                "warn",
            )
            return
        if line.startswith("open /dev/tee0") or line.startswith("ENROLL rejected"):
            self.detail = line
            self._set("FAILED", line, "warn")
            return
        m = _INVOKE.search(line)
        if m and int(m.group(2), 16) != 0:
            self.detail = line
            self._set(
                "FAILED",
                "That press was read, then the reader stopped. Lift your finger.",
                "warn",
            )
            return
        if line.startswith("no finger irq"):
            self.saw_no_finger = True
            self.detail = line
            self._set(
                "NO FINGER",
                "No finger arrived in time. Tap Enroll to try again.",
                "warn",
            )
            return
        m = _WAIT.match(line)
        if m:
            self.sample = int(m.group(1))
            self.detail = line
            left = "" if self.rem is None else f" {self.rem} presses left."
            self._set(
                "PRESS",
                "Press the power button and hold." + left,
                "tap",
            )
            return
        if line.startswith("REAL DOWN "):
            self.detail = line
            self._set("HOLD", "Stay on the power button.", "tap")
            return
        if _REJECT.match(line):
            self.detail = line
            self._set(
                "LIFT",
                "That press was empty. Lift, then press again when it says PRESS.",
                "lift",
            )
            return
        if line.startswith("skip leftover "):
            self.detail = line
            self._set(
                "LIFT",
                "Lift off the power button, then press again when it says PRESS.",
                "lift",
            )
            return
        m = _REM.match(line)
        if m:
            rem = int(m.group(1))
            self.rem = rem
            self.sample = int(m.group(2))
            self.detail = line
            if rem == 0:
                self._set("SAVING", "Last press counted. Saving. Hands off.", "idle")
            elif rem > 20:
                self._set(
                    "LIFT",
                    "Could not read how many presses are left. Lift, then press again.",
                    "warn",
                )
            else:
                self._set(
                    "LIFT",
                    f"Lift your finger. {rem} presses left, then press again.",
                    "lift",
                )
            return
        if line.startswith("rem unread"):
            self.detail = line
            self._set(
                "LIFT",
                "Could not read how many presses are left. Lift, then press again.",
                "warn",
            )
            return
        m = _DONE.match(line)
        if m:
            rem = int(m.group(1))
            wrote = int(m.group(2))
            save_ms = int(m.group(3))
            self.rem = rem if rem <= 20 else self.rem
            self.detail = line
            self.saved = rem == 0 and wrote == 1 and save_ms >= 30
            if self.saw_no_finger and not self.saved:
                self._set(
                    "NO FINGER",
                    "No finger arrived in time. Nothing was saved.",
                    "warn",
                )
            elif self.saved:
                self._set(
                    "SAVED",
                    "Finger saved. Tap Match. It asks for the same finger twice.",
                    "done",
                )
            else:
                self._set(
                    "NOT SAVED",
                    f"Not saved (remaining {rem}, wrote {wrote}, {save_ms} ms).",
                    "warn",
                )
            return
        m = _AUTH_ARM.match(line)
        if m:
            self.detail = line
            n = int(m.group(1))
            self._set(
                "PRESS",
                f"Match {n} of 2. Press the power button and hold for about a second.",
                "tap",
            )
            return
        m = _AUTH_DOWN.match(line)
        if m:
            self.detail = line
            self._set("HOLD", f"Match {m.group(1)} of 2. Stay on the power button.", "tap")
            return
        m = _SCORE.search(line)
        if m:
            self.scores.append(int(m.group(1)))
            self.detail = line
            return
        m = _AUTH_HIT.match(line)
        if m:
            self.detail = line
            n = int(m.group(1))
            if n == 1:
                self._set(
                    "LIFT",
                    "First press matched. Lift, then press again for the second.",
                    "done",
                )
            else:
                self._set("HOLD", "Second press matched. Finishing.", "done")
            return
        m = _AUTH_NO.match(line)
        if m:
            self.saw_no_finger = True
            self.detail = line
            self._set(
                "NO FINGER",
                f"Match {m.group(1)} of 2 saw no finger.",
                "warn",
            )
            return
        m = _AUTH_EMPTY.match(line)
        if m:
            self.detail = line
            self._set(
                "NO MATCH",
                f"Match {m.group(1)} of 2 was an empty press.",
                "warn",
            )
            return
        m = _AUTH_SKIP.match(line)
        if m:
            self.detail = line
            self._set(
                "LIFT",
                "Lift off the power button. That touch did not count.",
                "lift",
            )
            return
        m = _AUTH_FAIL.match(line)
        if m:
            self.detail = line
            self._set(
                "NO MATCH",
                f"Match {m.group(1)} of 2 did not match.{self._score_note()}",
                "warn",
            )
            return
        m = _PAIR.match(line)
        if m:
            a = int(m.group(1))
            b = int(m.group(2))
            self.detail = line
            self.matched = a == 0 and b == 0
            if self.matched:
                self._set("MATCH", "Same finger matched both times.", "done")
            else:
                self._set(
                    "NO MATCH",
                    "The two presses did not both match." + self._score_note(),
                    "warn",
                )
            return
        m = _EXIT.match(line)
        if m:
            self.exit_code = int(m.group(1))
            self.running = False
            self.done = True
            self.detail = line
            self.ok = self.exit_code == 0 and (
                (self.mode == "enroll" and self.saved)
                or (self.mode == "auth" and self.matched)
            )
            if self.ok:
                return
            if self.hero == "FAILED":
                return
            if self.hero in ("SAVED", "MATCH") and self.exit_code != 0:
                self._set(
                    self.hero,
                    self.sub + " The session still reported an error.",
                    "warn",
                )
            elif self.hero in _STUCK:
                self._set(
                    "FAILED",
                    "The reader stopped. Lift your finger.",
                    "warn",
                )
            return

    def _score_note(self) -> str:
        if not self.scores:
            return ""
        nums = ", ".join(str(n) for n in self.scores)
        return f" Distance {nums}. Lower is closer. 0 means no features."

    def _set(self, hero: str, sub: str, style: str) -> None:
        self.hero = hero
        self.sub = sub
        self.style = style
