#!/usr/bin/env python3
"""Finger — enroll or match on the power button through the QTEE session.

Nothing starts until Enroll or Match is tapped. Does not unlock Phosh and
does not load qsee_fingerpr.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

for p in (
    os.path.dirname(os.path.abspath(__file__)),
    "/home/user/.local/share/fp5-qtee",
):
    if p not in sys.path:
        sys.path.insert(0, p)

from fp5_qtee_coach import Coach  # noqa: E402
from fp5_qtee_haptic import Haptic, enroll_accept  # noqa: E402

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Gdk  # noqa: E402

APP_ID = "org.fp5.qtee"
APP_TITLE = "Finger"
SESSION_CANDIDATES = (
    Path("/home/user/fp5-qtee-keep/session/fp5-qtee-session"),
    Path("/tmp/fp5-qtee/fp5-qtee-session"),
)
LOAD_SCRIPT = Path("/home/user/fp5-qtee-keep/load-qcomtee.sh")
FIRMWARE = "/lib/firmware/qsee"
LOG_DIR = Path("/home/user/fp5-qtee-keep/logs")
OLD_DRIVER = Path("/sys/module/qsee_fingerpr")

CSS = """
window { background-color: #101418; }
.coach-hero { font-size: 34px; font-weight: 800; }
.coach-sub { font-size: 16px; font-weight: 600; color: #c5d0dc; }
.coach-meta { font-size: 12px; color: #8a97a6; }
.coach-idle { color: #7dd3fc; }
.coach-tap { color: #4ade80; }
.coach-lift { color: #f472b6; }
.coach-warn { color: #fca5a5; }
.coach-done { color: #a3e635; }
.btn-bar { padding: 8px 12px 16px 12px; }
"""


def session_bin() -> Path | None:
    for path in SESSION_CANDIDATES:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


class FingerWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(400, 720)
        self._coach = Coach()
        self._haptic = Haptic()
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = False
        self._closed = False
        self._log_path: Path | None = None
        self._hero_key: tuple[str, str, str] | None = None

        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(outer)
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_TITLE))
        outer.append(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_vexpand(True)
        outer.append(box)

        self._hero = Gtk.Label(label=self._coach.hero)
        self._hero.add_css_class("coach-hero")
        self._hero.add_css_class("coach-idle")
        self._hero.set_wrap(True)
        self._hero.set_justify(Gtk.Justification.CENTER)
        box.append(self._hero)

        self._sub = Gtk.Label(label=self._coach.sub)
        self._sub.add_css_class("coach-sub")
        self._sub.set_wrap(True)
        self._sub.set_justify(Gtk.Justification.CENTER)
        box.append(self._sub)

        self._meta = Gtk.Label(label="Power button. Does not unlock the phone.")
        self._meta.add_css_class("coach-meta")
        self._meta.set_wrap(True)
        self._meta.set_justify(Gtk.Justification.CENTER)
        self._meta.set_selectable(True)
        box.append(self._meta)

        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bar.add_css_class("btn-bar")
        self._enroll = Gtk.Button(label="Enroll")
        self._enroll.add_css_class("suggested-action")
        self._enroll.add_css_class("pill")
        self._enroll.connect("clicked", lambda *_a: self.start("enroll"))
        self._match = Gtk.Button(label="Match")
        self._match.add_css_class("pill")
        self._match.connect("clicked", lambda *_a: self.start("auth"))
        self._stop = Gtk.Button(label="Stop")
        self._stop.add_css_class("destructive-action")
        self._stop.add_css_class("pill")
        self._stop.set_sensitive(False)
        self._stop.connect("clicked", lambda *_a: self.stop())
        bar.append(self._enroll)
        bar.append(self._match)
        bar.append(self._stop)
        outer.append(bar)
        self.connect("close-request", self.on_close)

    def start(self, mode: str) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
            self._cancel = False
        self._coach.begin(mode)
        self._paint()
        self._buttons(running=True)
        threading.Thread(target=self._job, args=(mode,), daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._cancel = True
        self._kill()
        if not self._closed and self._coach.running:
            self._coach.running = False
            self._coach.done = True
            self._coach._set("STOPPED", "Stopped. Tap Enroll or Match to start again.", "warn")
            self._paint()
        self._buttons(running=False)

    def on_close(self, *_a) -> bool:
        self._closed = True
        with self._lock:
            self._cancel = True
        self._kill()
        return False

    def _cancelled(self) -> bool:
        with self._lock:
            return self._cancel

    def _buttons(self, running: bool) -> None:
        self._enroll.set_sensitive(not running)
        self._match.set_sensitive(not running)
        self._stop.set_sensitive(running)

    def _paint(self) -> None:
        if self._closed:
            return
        key = (self._coach.hero, self._coach.sub, self._coach.style)
        if key != self._hero_key:
            self._hero_key = key
            for name in ("coach-idle", "coach-tap", "coach-lift", "coach-warn", "coach-done"):
                self._hero.remove_css_class(name)
            self._hero.add_css_class("coach-" + self._coach.style)
            self._hero.set_text(self._coach.hero)
            self._sub.set_text(self._coach.sub)
        if self._log_path is not None:
            self._meta.set_text(str(self._log_path))

    def _ui(self, fn) -> None:
        GLib.idle_add(fn)

    def _kill(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            return

        def later() -> bool:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            return False

        GLib.timeout_add(2000, later)

    def _job(self, mode: str) -> None:
        rc = 0
        try:
            if not self._cancelled():
                self._preflight()
            if not self._cancelled():
                rc = self._run(mode)
        except Exception as exc:
            if self._cancelled():
                self._ui(lambda: self._finish(rc))
                return
            message = str(exc) or exc.__class__.__name__

            def fail(message: str = message) -> bool:
                with self._lock:
                    self._busy = False
                if self._closed:
                    return False
                self._coach.running = False
                self._coach.done = True
                self._coach._set("FAILED", message, "warn")
                self._paint()
                self._buttons(running=False)
                return False

            self._ui(fail)
            return
        self._ui(lambda rc=rc: self._finish(rc))

    def _preflight(self) -> None:
        if OLD_DRIVER.is_dir():
            raise RuntimeError(
                "The old fingerprint driver is loaded. Reboot, then open Finger again."
            )
        binary = session_bin()
        if binary is None:
            raise RuntimeError("The reader program is not on this phone.")
        if not LOAD_SCRIPT.is_file():
            return
        try:
            proc = subprocess.run(
                ["sudo", "-n", str(LOAD_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("The reader service did not start.") from exc
        if proc.returncode != 0:
            text = (proc.stdout + proc.stderr).strip()
            raise RuntimeError(text or "The reader service did not start.")

    def _run(self, mode: str) -> int:
        binary = session_bin()
        if binary is None:
            raise RuntimeError("The reader program is not on this phone.")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = LOG_DIR / f"finger-{mode}-{stamp}.txt"
        limit = "1500" if mode == "enroll" else "400"
        self._log_path = path
        self._ui(self._paint_only)
        with path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [
                    "sudo",
                    "-n",
                    "timeout",
                    "-k",
                    "15",
                    limit,
                    str(binary),
                    mode,
                    FIRMWARE,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                self._proc = proc
                cancel = self._cancel
            if cancel:
                self._kill()
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                self._ui(lambda line=line: self._deliver(line))
            rc = proc.wait()
        with self._lock:
            if self._proc is proc:
                self._proc = None
        return rc

    def _deliver(self, line: str) -> bool:
        if not self._closed:
            # One click per counted enroll sample. A reject is not one.
            if self._coach.mode == "enroll" and enroll_accept(line):
                self._haptic.play("success")
            self._coach.on_line(line)
            self._paint()
        return False

    def _paint_only(self) -> bool:
        self._paint()
        return False

    def _finish(self, rc: int) -> bool:
        with self._lock:
            self._busy = False
        if self._closed:
            return False
        if self._coach.running and not self._coach.done:
            self._coach.running = False
            self._coach.done = True
            if rc == 124:
                self._coach._set(
                    "NO FINGER",
                    "Timed out waiting for a finger.",
                    "warn",
                )
            elif self._coach.hero == "STARTING":
                self._coach._set("FAILED", "The reader stopped before it was ready.", "warn")
        if self._log_path is not None:
            with self._log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"app exit={rc} ok={int(self._coach.ok)} hero={self._coach.hero}\n"
                )
            latest = LOG_DIR / f"finger-{self._coach.mode}-latest.txt"
            shutil.copyfile(self._log_path, latest)
        self._paint()
        self._buttons(running=False)
        return False


class FingerApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.connect("activate", self.on_activate)

    def on_activate(self, *_a) -> None:
        win = FingerWindow(self)
        win.present()


def main() -> int:
    app = FingerApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
