#!/usr/bin/env python3
"""net.reactivated.Fprint for the Fairphone 5, backed by fp5-qtee-session.

A small stand-in for fprintd: same D-Bus names, objects, methods and
signals that pam_fprintd, polkit agents and GNOME Settings use. There is
no libfprint underneath. Each operation runs fp5-qtee-session against the
QTEE trustlet, the same binary the unlock watcher uses.

One owner of the sensor at a time:
  - While any graphical session is locked, the unlock watcher owns the
    sensor (it keeps a warm serve session). We do not open the trustlet
    then; Verify/Enroll/Delete fail fast with an error, so PAM falls back
    to the password at once.
  - While unlocked the watcher quits its session. We take
    /tmp/fp5-qtee-phone.lock and start our own session only if no other
    fp5-qtee-session runs, and quit it when the client releases.
  - If the seat locks while we hold the sensor, we stop and release it.

Finger names: the trustlet knows only numeric fids. fingers.json maps
fid -> {user, finger}. Fids the trustlet lists that the map lacks get the
next free name from DEFAULT_ORDER and are written back.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import pwd
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BUS_NAME = "net.reactivated.Fprint"
MGR_PATH = "/net/reactivated/Fprint/Manager"
DEV_PATH = "/net/reactivated/Fprint/Device/0"
IFACE_MGR = "net.reactivated.Fprint.Manager"
IFACE_DEV = "net.reactivated.Fprint.Device"
IFACE_MANAGE = "org.fp5.Fprint.Manage"  # our extension: details + relabel
ERR = "net.reactivated.Fprint.Error."

SESSION_CANDIDATES = (
    Path(os.environ.get("FP5_FPRINTD_SESSION", "/home/user/fp5-qtee-keep/session/fp5-qtee-session")),
    Path("/usr/local/lib/fp5-qtee/fp5-qtee-session"),
    Path("/usr/libexec/fp5-qtee/fp5-qtee-session"),  # BengalOS sysext
)
FIRMWARE = os.environ.get("FP5_FPRINTD_FW", "/lib/firmware/qsee")
LOAD_SCRIPT = Path(os.environ.get("FP5_FPRINTD_LOAD", "/home/user/fp5-qtee-keep/load-qcomtee.sh"))
STATE_DIR = Path(os.environ.get("FP5_FPRINTD_STATE", "/var/lib/fp5-qtee"))
MAP_PATH = STATE_DIR / "fingers.json"
BACKUP_DIR = STATE_DIR / "backup"
LOG_DIR = STATE_DIR / "logs"
STORE = Path(os.environ.get("FP5_FPRINTD_STORE", "/mnt/persist/misc"))
PHONE_LOCK = os.environ.get("FP5_FPRINTD_LOCK", "/tmp/fp5-qtee-phone.lock")
DEFAULT_USER = os.environ.get("FP5_FPRINTD_USER", "user")
# Known fids on this phone (enrolled 2026-10). Names are a guess until the
# user renames them in fingers.json; the trustlet stores no names.
SEED = {1558157930: "right-index-finger", 1979259351: "right-thumb"}
if os.environ.get("FP5_FPRINTD_SEED", "1") == "0":  # other phones: no guessed fids
    SEED = {}
FINGERS = (
    "left-thumb", "left-index-finger", "left-middle-finger",
    "left-ring-finger", "left-little-finger", "right-thumb",
    "right-index-finger", "right-middle-finger", "right-ring-finger",
    "right-little-finger",
)
DEFAULT_ORDER = (
    "right-index-finger", "right-thumb", "left-index-finger", "left-thumb",
    "right-middle-finger", "left-middle-finger", "right-ring-finger",
    "left-ring-finger", "right-little-finger", "left-little-finger",
)
ENROLL_STAGES = int(os.environ.get("FP5_FPRINTD_ENROLL_STAGES", "16"))
# A claimed but idle session is closed after this, so a client that went
# away does not keep the sensor from the lock screen.
IDLE_RELEASE_S = 60.0
READY_TIMEOUT_S = 20.0
ALLOW_LAST = os.environ.get("FP5_FPRINTD_ALLOW_LAST", "0") == "1"

_HIT = re.compile(r"^AUTH HIT (\d+) .* fid=(\d+) ")
_NOMATCH = re.compile(r"^AUTH FAIL \d+ itype=0x2 ")
_PARTIAL = re.compile(r"^AUTH \d+ empty ")
_ARM = re.compile(r"^AUTH \d+ arm ")
_DOWN = re.compile(r"^AUTH \d+ REAL DOWN ")
_RESULT = re.compile(r"^SERVE result (-?\d+)")
_FINGER = re.compile(r"^FINGER index=\d+ gid=\d+ fid=(\d+) ")
_REM = re.compile(r"^rem=(\d+) sample=")
_ENROLL_FID = re.compile(r"^ENROLL fid=(\d+) rc=(-?\d+)")
_ENROLL_DONE = re.compile(r"^enroll done rem=(\d+) wrote=(\d+)")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- pure logic

def verify_event(line: str) -> tuple[str, int | None] | None:
    """Map one session line to a fprintd verify result.

    ("match", fid), ("no-match", None), ("retry", None) for a partial or
    too-light press, ("armed", None), ("down", None), ("result", rc), or
    None for lines that do not matter.
    """
    text = line.strip()
    m = _HIT.match(text)
    if m:
        return ("match", int(m.group(2)))
    if _NOMATCH.match(text):
        return ("no-match", None)
    if _PARTIAL.match(text):
        return ("retry", None)
    if _ARM.match(text):
        return ("armed", None)
    if _DOWN.match(text):
        return ("down", None)
    m = _RESULT.match(text)
    if m:
        return ("result", int(m.group(1)))
    return None


def enroll_event(line: str) -> tuple[str, int | None] | None:
    """Map one `enroll` line: ("fid", n), ("rem", n), ("retry", None),
    ("done", rem_ok) or ("rejected", None)."""
    text = line.strip()
    m = _ENROLL_FID.match(text)
    if m:
        return ("fid", int(m.group(1)))
    m = _REM.match(text)
    if m:
        return ("rem", int(m.group(1)))
    if text.startswith("PRESS REJECT") or text.startswith("CAP null") or text.startswith("no finger irq"):
        return ("retry", None)
    if text.startswith("ENROLL rejected"):
        return ("rejected", None)
    m = _ENROLL_DONE.match(text)
    if m:
        ok = int(m.group(1)) == 0 and int(m.group(2)) != 0
        return ("done", 1 if ok else 0)
    return None


def listed_fids(text: str) -> list[int]:
    out = []
    for line in text.splitlines():
        m = _FINGER.match(line.strip())
        if m:
            fid = int(m.group(1))
            if fid not in out:
                out.append(fid)
    return out


class FingerMap:
    """fid <-> (user, finger name), persisted as JSON."""

    def __init__(self, path: Path = MAP_PATH) -> None:
        self.path = path
        self.rows: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            rows = data.get("fingers", []) if isinstance(data, dict) else []
            self.rows = [r for r in rows if isinstance(r, dict) and "fid" in r]
        except (OSError, ValueError):
            self.rows = [
                {"fid": fid, "user": DEFAULT_USER, "finger": name, "source": "seed"}
                for fid, name in SEED.items()
            ]
            self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "fingers": self.rows}, indent=1) + "\n")
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"fingers.json not saved: {e}")

    def names(self, user: str) -> list[str]:
        return [r["finger"] for r in self.rows if r.get("user") == user]

    def fid(self, user: str, finger: str) -> int | None:
        for r in self.rows:
            if r.get("user") == user and r.get("finger") == finger:
                return int(r["fid"])
        return None

    def fids(self, user: str) -> list[int]:
        return [int(r["fid"]) for r in self.rows if r.get("user") == user]

    def owner(self, fid: int) -> str | None:
        for r in self.rows:
            if int(r["fid"]) == fid:
                return r.get("user")
        return None

    def add(self, fid: int, user: str, finger: str, source: str) -> None:
        self.rows = [r for r in self.rows if int(r["fid"]) != fid
                     and not (r.get("user") == user and r.get("finger") == finger)]
        self.rows.append({"fid": fid, "user": user, "finger": finger, "source": source})
        self.save()

    def drop(self, fid: int) -> None:
        self.rows = [r for r in self.rows if int(r["fid"]) != fid]
        self.save()

    def details(self, user: str) -> list[tuple[str, int, str]]:
        """(finger name, fid, source) for a user, in map order."""
        return [(r["finger"], int(r["fid"]), str(r.get("source", "")))
                for r in self.rows if r.get("user") == user]

    def relabel(self, user: str, old: str, new: str) -> str:
        """Give the template named `old` the name `new`. Names only; the
        trustlet is not touched. If `new` is already taken by another of the
        user's templates the two names are swapped. Returns "renamed",
        "swapped" or "same". Raises KeyError (no such finger) or ValueError
        (not a standard finger name)."""
        if new not in FINGERS or old not in FINGERS:
            raise ValueError(new if new not in FINGERS else old)
        mine = {r["finger"]: r for r in self.rows if r.get("user") == user}
        if old not in mine:
            raise KeyError(old)
        if old == new:
            return "same"
        other = mine.get(new)
        mine[old]["finger"] = new
        mine[old]["source"] = "relabel"
        if other is not None:
            other["finger"] = old
            other["source"] = "relabel"
        self.save()
        return "swapped" if other is not None else "renamed"

    def sync(self, fids: list[int], user: str = DEFAULT_USER) -> bool:
        """Make the map match the trustlet list. True if it changed."""
        changed = False
        keep = [r for r in self.rows if int(r["fid"]) in fids]
        if len(keep) != len(self.rows):
            changed = True
            self.rows = keep
        known = {int(r["fid"]) for r in self.rows}
        for fid in fids:
            if fid in known:
                continue
            used = set(self.names(user))
            name = next((n for n in DEFAULT_ORDER if n not in used), None)
            if name is None:
                continue
            self.rows.append({"fid": fid, "user": user, "finger": name, "source": "list"})
            changed = True
        if changed:
            self.save()
        return changed


# ------------------------------------------------------------- sensor access

def session_bin() -> Path | None:
    for p in SESSION_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def other_session_pids(ours: int | None = None) -> list[int]:
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            comm = (d / "comm").read_text().strip()
        except OSError:
            continue
        if comm.startswith("fp5-qtee-sessio") and int(d.name) != ours:
            pids.append(int(d.name))
    return pids


def seat_locked() -> bool | None:
    """True if any graphical logind session is locked. None if unknown."""
    forced = os.environ.get("FP5_FPRINTD_FAKE_LOCKED")
    if forced is not None:
        return forced == "1" or Path(forced).is_file()
    try:
        out = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    any_known = False
    for line in out.splitlines():
        sid = line.split()[0] if line.split() else ""
        if not sid:
            continue
        try:
            show = subprocess.run(
                ["loginctl", "show-session", sid, "-p", "Type", "-p", "LockedHint"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        props = dict(l.split("=", 1) for l in show.splitlines() if "=" in l)
        if props.get("Type") not in ("wayland", "x11"):
            continue
        any_known = True
        if props.get("LockedHint") == "yes":
            return True
    return False


class Busy(Exception):
    pass


class SensorLock:
    """flock on the shared phone lock plus the 'no other session' rule."""

    def __init__(self) -> None:
        self.fd: int | None = None

    def take(self) -> None:
        # Unknown lock state counts as locked: never take the sensor from
        # the watcher on a guess.
        if seat_locked() is not False:
            raise Busy("the screen is locked; the unlock watcher owns the sensor")
        if other_session_pids():
            raise Busy("another fp5-qtee-session is running")
        fd = os.open(PHONE_LOCK, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise Busy(f"{PHONE_LOCK} is held")
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            self.fd = None


def prepare() -> None:
    if not Path("/dev/tee0").exists() and LOAD_SCRIPT.is_file():
        subprocess.run([str(LOAD_SCRIPT)], timeout=60, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Session:
    """fp5-qtee-session in a child process; lines go to a queue."""

    def __init__(self, args: list[str], tag: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.logf = (LOG_DIR / f"{tag}-{stamp}.txt").open("w")
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self.logf.write(line)
                self.logf.flush()
            except (OSError, ValueError):
                pass
            self.lines.put(line)
        self.lines.put(None)

    def send(self, cmd: str) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError, AssertionError):
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self, grace: float = 10.0) -> None:
        self.send("quit")
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            # SIGTERM is deferred by the session until a learn save ends.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            self.logf.close()
        except OSError:
            pass


def run_once(mode_args: list[str], tag: str, timeout: float = 120) -> str:
    """Run a one-shot session mode (list, remove) and return its output."""
    binary = session_bin()
    if binary is None:
        raise Busy("no fp5-qtee-session binary")
    prepare()
    s = Session([str(binary)] + mode_args, tag)
    out = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            line = s.lines.get(timeout=1)
        except queue.Empty:
            continue
        if line is None:
            break
        out.append(line)
    s.close(grace=5)
    return "".join(out)


def backup_map(path: Path = MAP_PATH) -> Path | None:
    """Copy fingers.json to BACKUP_DIR before a rename. None if absent."""
    if not path.is_file():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"fingers-{_dt.datetime.now(_dt.timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    shutil.copy2(path, dst)
    return dst


def backup_store() -> Path:
    """Copy fingerprint storage with a sha256 manifest. Raises on mismatch."""
    base = BACKUP_DIR / _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst, n = base, 1
    while dst.exists():
        n += 1
        dst = base.with_name(f"{base.name}-{n}")
    shutil.copytree(STORE, dst / "misc", symlinks=True)
    lines = []
    for p in sorted((dst / "misc").rglob("*")):
        if p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            src = STORE / p.relative_to(dst / "misc")
            if hashlib.sha256(src.read_bytes()).hexdigest() != h:
                raise OSError(f"backup mismatch for {src}")
            lines.append(f"{h}  {p.relative_to(dst / 'misc')}")
    (dst / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
    log(f"backup {dst} files={len(lines)}")
    return dst


# --------------------------------------------------------------- D-Bus layer

def main() -> int:  # pragma: no cover - needs a system bus
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    test = os.environ.get("FP5_FPRINTD_TEST", "0") == "1"
    bus = dbus.SessionBus() if test else dbus.SystemBus()
    fmap = FingerMap()

    def dbus_err(name: str, msg: str) -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(msg, name=ERR + name)

    def caller_uid(sender: str) -> int:
        return int(bus.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                     "org.freedesktop.DBus", "GetConnectionUnixUser",
                                     "s", [sender]))

    def polkit_ok(sender: str, action: str, interactive: bool = False) -> bool:
        if caller_uid(sender) == 0 or test:
            return True
        try:
            auth = bus.get_object("org.freedesktop.PolicyKit1",
                                  "/org/freedesktop/PolicyKit1/Authority")
            subject = ("system-bus-name", {"name": dbus.String(sender, variant_level=1)})
            res = auth.CheckAuthorization(
                subject, action, dbus.Dictionary({}, signature="ss"),
                dbus.UInt32(1 if interactive else 0), "",
                dbus_interface="org.freedesktop.PolicyKit1.Authority", timeout=120)
            return bool(res[0])
        except dbus.exceptions.DBusException as e:
            log(f"polkit {action}: {e.get_dbus_name()}")
            return False

    class Device(dbus.service.Object):
        def __init__(self) -> None:
            super().__init__(bus, DEV_PATH)
            self.claimer: str | None = None
            self.user: str | None = None
            self.lock = SensorLock()
            self.sess: Session | None = None
            self.op: str | None = None  # "verify" | "enroll"
            self.op_finger = "any"
            self.armed_sent = False
            self.idle_since = time.monotonic()
            self.enroll_rem: int | None = None
            self.enroll_fid: int | None = None
            self.watch = None
            GLib.timeout_add(100, self._tick)
            GLib.timeout_add_seconds(2, self._lock_check)

        # ---------------------------------------------------------- helpers
        def _need_claim(self, sender: str) -> None:
            if self.claimer is None:
                raise dbus_err("ClaimDevice", "Device was not claimed before use")
            if sender != self.claimer:
                raise dbus_err("AlreadyInUse", "Device already in use by another user")

        def _user_for(self, username: str, sender: str) -> str:
            uid = caller_uid(sender)
            if not username:
                return pwd.getpwuid(uid).pw_name
            if uid != 0 and pwd.getpwuid(uid).pw_name != username:
                if not polkit_ok(sender, "net.reactivated.fprint.device.setusername", True):
                    raise dbus_err("PermissionDenied", "Not allowed to act for another user")
            return username

        def _open_serve(self) -> None:
            if self.sess is not None and self.sess.alive():
                return
            binary = session_bin()
            if binary is None:
                raise dbus_err("Internal", "no fp5-qtee-session binary")
            try:
                self.lock.take()
            except Busy as e:
                raise dbus_err("AlreadyInUse", str(e))
            try:
                prepare()
                self.sess = Session([str(binary), "serve", FIRMWARE], "fprintd-serve")
            except OSError as e:
                self.lock.release()
                raise dbus_err("Internal", f"session start: {e}")
            self.armed_sent = False
            self.idle_since = time.monotonic()
            log("serve session started")

        def _close_sess(self) -> None:
            s, self.sess = self.sess, None
            self.op = None
            if s is not None:
                s.close()
                log("session closed")
            self.lock.release()

        def _sender_gone(self, new_owner: str) -> None:
            if not new_owner and self.claimer is not None:
                log("claimer left the bus; releasing")
                self._release()

        def _release(self) -> None:
            self._close_sess()
            self.claimer = None
            self.user = None
            if self.watch is not None:
                self.watch.cancel()
                self.watch = None

        # ------------------------------------------------------- the loop
        def _tick(self) -> bool:
            s = self.sess
            if s is None:
                return True
            while True:
                try:
                    line = s.lines.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    log("session ended")
                    if self.op == "verify":
                        self.VerifyStatus("verify-disconnected", True)
                    elif self.op == "enroll":
                        self._enroll_finish_from_exit()
                    self.sess = None
                    self.op = None
                    self.lock.release()
                    return True
                if self.op == "verify":
                    self._verify_line(line)
                elif self.op == "enroll":
                    self._enroll_line(line)
                elif line.startswith("SERVE idle"):
                    self.idle_since = time.monotonic()
            if self.op is None and s is self.sess and \
                    time.monotonic() - self.idle_since > IDLE_RELEASE_S:
                log("idle; closing session")
                self._close_sess()
            return True

        def _lock_check(self) -> bool:
            if self.sess is not None and seat_locked() is not False:
                log("seat locked; handing the sensor back to the watcher")
                if self.op == "verify":
                    self.VerifyStatus("verify-disconnected", True)
                elif self.op == "enroll":
                    self.EnrollStatus("enroll-disconnected", True)
                self._close_sess()
            return True

        def _verify_line(self, line: str) -> None:
            ev = verify_event(line)
            if ev is None:
                if line.startswith("SERVE idle") and self.op == "verify" and not self.armed_sent:
                    self.sess.send("auth")
                    self.armed_sent = True
                return
            kind, val = ev
            if kind == "match":
                fid = int(val or 0)
                owner = fmap.owner(fid)
                if owner is not None and owner != self.user:
                    log(f"hit fid={fid} belongs to {owner}, not {self.user}")
                    self.op = None
                    self.VerifyStatus("verify-no-match", True)
                    return
                if self.op_finger != "any" and fmap.fid(self.user or "", self.op_finger) not in (None, fid):
                    self.op = None
                    self.VerifyStatus("verify-no-match", True)
                    return
                log(f"verify match fid={fid}")
                self.op = None
                self.VerifyStatus("verify-match", True)
            elif kind == "no-match":
                self.op = None
                self.VerifyStatus("verify-no-match", True)
            elif kind == "retry":
                self.VerifyStatus("verify-retry-scan", False)
            elif kind == "result":
                # Attempt over without a decision (no finger, cancelled).
                self.armed_sent = False
            # After "result" the session prints "SERVE idle"; arm again
            # while the verify is still running.

        def _enroll_line(self, line: str) -> None:
            ev = enroll_event(line)
            if ev is None:
                return
            kind, val = ev
            if kind == "fid":
                self.enroll_fid = val
            elif kind == "rem":
                if self.enroll_rem is None or (val is not None and val < self.enroll_rem):
                    self.EnrollStatus("enroll-stage-passed", False)
                self.enroll_rem = val
            elif kind == "retry":
                self.EnrollStatus("enroll-retry-scan", False)
            elif kind == "rejected":
                self.EnrollStatus("enroll-failed", True)
                self.op = None
            elif kind == "done":
                self._enroll_done(bool(val))

        def _enroll_done(self, ok: bool) -> None:
            self.op = None
            if ok and self.enroll_fid:
                fmap.add(self.enroll_fid, self.user or DEFAULT_USER, self.op_finger, "enroll")
                log(f"enrolled fid={self.enroll_fid} as {self.op_finger}")
                self.EnrollStatus("enroll-completed", True)
            else:
                self.EnrollStatus("enroll-failed", True)

        def _enroll_finish_from_exit(self) -> None:
            if self.op == "enroll":
                self._enroll_done(False)

        # ------------------------------------------------- D-Bus methods
        @dbus.service.method(IFACE_DEV, in_signature="s", out_signature="as",
                             sender_keyword="sender")
        def ListEnrolledFingers(self, username, sender=None):
            user = self._user_for(str(username), sender)
            names = fmap.names(user)
            if not names:
                raise dbus_err("NoEnrolledPrints", "Failed to discover prints")
            return dbus.Array(names, signature="s")

        @dbus.service.method(IFACE_DEV, in_signature="s", out_signature="",
                             sender_keyword="sender")
        def Claim(self, username, sender=None):
            if self.claimer is not None:
                raise dbus_err("AlreadyInUse", "Device was already claimed")
            user = self._user_for(str(username), sender)
            if caller_uid(sender) != 0 and not polkit_ok(sender, "net.reactivated.fprint.device.verify"):
                raise dbus_err("PermissionDenied", "Not authorized: net.reactivated.fprint.device.verify")
            self.claimer = sender
            self.user = user
            self.watch = bus.watch_name_owner(sender, self._sender_gone)
            log(f"claimed by {sender} for {user}")

        @dbus.service.method(IFACE_DEV, in_signature="", out_signature="",
                             sender_keyword="sender")
        def Release(self, sender=None):
            self._need_claim(sender)
            self._release()

        @dbus.service.method(IFACE_DEV, in_signature="s", out_signature="",
                             sender_keyword="sender")
        def VerifyStart(self, finger_name, sender=None):
            self._need_claim(sender)
            if self.op is not None:
                raise dbus_err("AlreadyInUse", "Action already in progress")
            names = fmap.names(self.user or "")
            if not names:
                raise dbus_err("NoEnrolledPrints", "No fingerprints enrolled")
            finger = str(finger_name) or "any"
            if finger != "any" and finger not in names:
                raise dbus_err("NoEnrolledPrints", f"{finger} not enrolled")
            self._open_serve()
            self.op = "verify"
            self.op_finger = finger
            self.armed_sent = False
            self.VerifyFingerSelected(finger)
            # If the session is already idle, arm now; else _tick arms on
            # its first "SERVE idle".
            if self.sess is not None:
                self.sess.send("auth")
                self.armed_sent = True

        @dbus.service.method(IFACE_DEV, in_signature="", out_signature="",
                             sender_keyword="sender")
        def VerifyStop(self, sender=None):
            self._need_claim(sender)
            if self.op == "verify":
                self.op = None
                if self.sess is not None:
                    self.sess.send("cancel")
            self.idle_since = time.monotonic()

        @dbus.service.method(IFACE_DEV, in_signature="s", out_signature="",
                             sender_keyword="sender")
        def EnrollStart(self, finger_name, sender=None):
            self._need_claim(sender)
            if not polkit_ok(sender, "net.reactivated.fprint.device.enroll", True):
                raise dbus_err("PermissionDenied", "Not authorized: net.reactivated.fprint.device.enroll")
            finger = str(finger_name)
            if finger not in FINGERS:
                raise dbus_err("InvalidFingername", "Invalid finger name")
            if fmap.fid(self.user or "", finger) is not None:
                raise dbus_err("FingerAlreadyEnrolled", f"{finger} is already enrolled; delete it first")
            if self.op is not None:
                raise dbus_err("AlreadyInUse", "Action already in progress")
            self._close_sess()
            binary = session_bin()
            if binary is None:
                raise dbus_err("Internal", "no fp5-qtee-session binary")
            try:
                self.lock.take()
            except Busy as e:
                raise dbus_err("AlreadyInUse", str(e))
            try:
                backup_store()
                prepare()
                self.sess = Session([str(binary), "enroll", FIRMWARE], "fprintd-enroll")
            except OSError as e:
                self.lock.release()
                raise dbus_err("Internal", f"enroll start: {e}")
            self.op = "enroll"
            self.op_finger = finger
            self.enroll_rem = None
            self.enroll_fid = None

        @dbus.service.method(IFACE_DEV, in_signature="", out_signature="",
                             sender_keyword="sender")
        def EnrollStop(self, sender=None):
            self._need_claim(sender)
            if self.op == "enroll" or (self.sess is not None):
                # The enroll mode has no cancel command; end the process.
                # Nothing is saved until the last sample (rem=0).
                self.op = None
                self._close_sess()

        def _delete(self, sender, fids, done_ok, done_err, allow_last=False):
            if not polkit_ok(sender, "net.reactivated.fprint.device.enroll", True):
                done_err(dbus_err("PermissionDenied", "Not authorized: net.reactivated.fprint.device.enroll"))
                return
            if self.op is not None:
                done_err(dbus_err("AlreadyInUse", "Action already in progress"))
                return
            self._close_sess()
            try:
                self.lock.take()
            except Busy as e:
                done_err(dbus_err("AlreadyInUse", str(e)))
                return

            def work():
                err = None
                try:
                    backup_store()
                    for fid in fids:
                        args = ["remove", FIRMWARE, str(fid), "--yes-really"]
                        if allow_last:
                            args.append("--allow-last")
                        out = run_once(args, "fprintd-remove")
                        listed = listed_fids(out.split("LIST_AFTER", 1)[-1]) if "LIST_AFTER" in out else None
                        if listed is not None and fid not in listed:
                            fmap.drop(fid)
                            log(f"removed fid={fid}")
                        else:
                            err = dbus_err("PrintsNotDeleted", f"fid {fid} not removed (see {LOG_DIR})")
                            break
                except (OSError, Busy) as e:
                    err = dbus_err("PrintsNotDeleted", str(e))
                finally:
                    self.lock.release()
                GLib.idle_add(lambda: (done_err(err) if err else done_ok()) and False)

            threading.Thread(target=work, daemon=True).start()

        @dbus.service.method(IFACE_DEV, in_signature="s", out_signature="",
                             sender_keyword="sender", async_callbacks=("ok", "err"))
        def DeleteEnrolledFinger(self, finger_name, sender=None, ok=None, err=None):
            try:
                self._need_claim(sender)
            except dbus.exceptions.DBusException as e:
                err(e)
                return
            fid = fmap.fid(self.user or "", str(finger_name))
            if fid is None:
                err(dbus_err("NoEnrolledPrints", f"{finger_name} not enrolled"))
                return
            last = len(fmap.rows) <= 1
            if last and not ALLOW_LAST:
                err(dbus_err("PrintsNotDeleted", "refusing to delete the last finger on this phone"))
                return
            self._delete(sender, [fid], ok, err, allow_last=last)

        @dbus.service.method(IFACE_DEV, in_signature="", out_signature="",
                             sender_keyword="sender", async_callbacks=("ok", "err"))
        def DeleteEnrolledFingers2(self, sender=None, ok=None, err=None):
            try:
                self._need_claim(sender)
            except dbus.exceptions.DBusException as e:
                err(e)
                return
            fids = fmap.fids(self.user or "")
            if not fids:
                err(dbus_err("NoEnrolledPrints", "No fingerprints enrolled"))
                return
            if len(fids) >= len(fmap.rows) and not ALLOW_LAST:
                err(dbus_err("PrintsNotDeleted", "refusing to delete every finger on this phone (set FP5_FPRINTD_ALLOW_LAST=1)"))
                return
            self._delete(sender, fids, ok, err, allow_last=ALLOW_LAST)

        @dbus.service.method(IFACE_DEV, in_signature="s", out_signature="",
                             sender_keyword="sender", async_callbacks=("ok", "err"))
        def DeleteEnrolledFingers(self, username, sender=None, ok=None, err=None):
            # Deprecated fprintd call: no claim needed.
            try:
                user = self._user_for(str(username), sender)
            except dbus.exceptions.DBusException as e:
                err(e)
                return
            fids = fmap.fids(user)
            if not fids:
                err(dbus_err("NoEnrolledPrints", "No fingerprints enrolled"))
                return
            if len(fids) >= len(fmap.rows) and not ALLOW_LAST:
                err(dbus_err("PrintsNotDeleted", "refusing to delete every finger on this phone"))
                return
            self._delete(sender, fids, ok, err, allow_last=ALLOW_LAST)

        # --------------------------------------- org.fp5.Fprint.Manage
        # Not part of fprintd. Used by the Fingerprints app only; stock
        # clients (GNOME Settings, pam_fprintd) never call it.
        @dbus.service.method(IFACE_MANAGE, in_signature="s", out_signature="a(sxs)",
                             sender_keyword="sender")
        def ListDetailed(self, username, sender=None):
            user = self._user_for(str(username), sender)
            return dbus.Array([(n, dbus.Int64(fid), src) for n, fid, src in fmap.details(user)],
                              signature="(sxs)")

        @dbus.service.method(IFACE_MANAGE, in_signature="ss", out_signature="s",
                             sender_keyword="sender")
        def RelabelFinger(self, old_name, new_name, sender=None):
            """Rename which finger a stored template is called (names only)."""
            user = self._user_for("", sender)
            if not polkit_ok(sender, "net.reactivated.fprint.device.enroll", True):
                raise dbus_err("PermissionDenied", "Not authorized: net.reactivated.fprint.device.enroll")
            try:
                backup_map(fmap.path)
                res = fmap.relabel(user, str(old_name), str(new_name))
            except ValueError as e:
                raise dbus_err("InvalidFingername", f"Invalid finger name: {e}")
            except KeyError:
                raise dbus_err("NoEnrolledPrints", "No such enrolled finger")
            log(f"relabel {old_name} -> {new_name}: {res}")
            return res

        # ---------------------------------------------------- signals
        @dbus.service.signal(IFACE_DEV, signature="sb")
        def VerifyStatus(self, result, done):
            log(f"VerifyStatus {result} {done}")

        @dbus.service.signal(IFACE_DEV, signature="s")
        def VerifyFingerSelected(self, finger_name):
            pass

        @dbus.service.signal(IFACE_DEV, signature="sb")
        def EnrollStatus(self, result, done):
            log(f"EnrollStatus {result} {done}")

        @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface, prop):
            props = self.GetAll(interface)
            if prop not in props:
                raise dbus.exceptions.DBusException(
                    prop, name="org.freedesktop.DBus.Error.UnknownProperty")
            return props[prop]

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface not in (IFACE_DEV, ""):
                return dbus.Dictionary({}, signature="sv")
            return dbus.Dictionary({
                "name": dbus.String("Fairphone 5 FocalTech FT9362 (QTEE)"),
                "num-enroll-stages": dbus.Int32(ENROLL_STAGES),
                "scan-type": dbus.String("press"),
                "finger-present": dbus.Boolean(False),
                "finger-needed": dbus.Boolean(self.op is not None),
            }, signature="sv")

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv", out_signature="")
        def Set(self, interface, prop, value):
            raise dbus.exceptions.DBusException(
                prop, name="org.freedesktop.DBus.Error.PropertyReadOnly")

        @dbus.service.method(dbus.INTROSPECTABLE_IFACE, in_signature="", out_signature="s",
                             path_keyword="object_path", connection_keyword="connection")
        def Introspect(self, object_path, connection):
            return dbus.service.Object.Introspect(self, object_path, connection)

    class Manager(dbus.service.Object):
        def __init__(self) -> None:
            super().__init__(bus, MGR_PATH)

        @dbus.service.method(IFACE_MGR, in_signature="", out_signature="ao")
        def GetDevices(self):
            return dbus.Array([dbus.ObjectPath(DEV_PATH)], signature="o")

        @dbus.service.method(IFACE_MGR, in_signature="", out_signature="o")
        def GetDefaultDevice(self):
            return dbus.ObjectPath(DEV_PATH)

    name = dbus.service.BusName(BUS_NAME, bus, allow_replacement=False,
                                replace_existing=True, do_not_queue=True)
    dev = Device()
    mgr = Manager()

    def initial_sync() -> bool:
        # Refresh names from the trustlet once, only if the sensor is free.
        def work():
            lock = SensorLock()
            try:
                lock.take()
            except Busy as e:
                log(f"startup list skipped: {e}")
                return
            try:
                out = run_once(["list", FIRMWARE], "fprintd-list")
                fids = listed_fids(out)
                if fids:
                    GLib.idle_add(lambda: fmap.sync(fids) and False)
                    log(f"trustlet lists {fids}")
            except Busy as e:
                log(f"list: {e}")
            finally:
                lock.release()
        if os.environ.get("FP5_FPRINTD_LIST_AT_START", "0") == "1":
            threading.Thread(target=work, daemon=True).start()
        return False

    GLib.idle_add(initial_sync)
    log(f"fp5-fprintd (qtee) on {bus.get_unique_name()} as {name.get_name()}; map {fmap.rows}")
    loop = GLib.MainLoop()

    def stop(*_a):
        dev._release()
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, stop)
    loop.run()
    _ = mgr
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
