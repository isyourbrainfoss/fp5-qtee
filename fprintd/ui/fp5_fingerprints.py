#!/usr/bin/env python3
"""Fingerprints: list, add, test, rename and delete fingers.

A small GTK4 / libadwaita app for Phosh. It only talks to
net.reactivated.Fprint (our fp5-fprintd, or any real fprintd) and, for
renaming, to org.fp5.Fprint.Manage. It never opens the sensor or the
fingerprint storage itself.

  * Add:    Claim, EnrollStart(finger), EnrollStatus signals, EnrollStop, Release.
  * Test:   Claim, VerifyStart("any"), VerifyStatus, VerifyStop, Release.
  * Delete: DeleteEnrolledFinger(name)  (fp5-fprintd backs up storage first).
  * Rename: RelabelFinger(old, new)     (names only; swaps if the new name is used).
  * Lockout: org.fp5.Fprint.Manage properties locked-out / lockout-remaining /
    lockout-permanent + LockoutChanged; shown as a banner with a countdown.
    A stock fprintd has none of these and the banner just stays hidden.

While the phone is locked the unlock watcher owns the sensor, so enroll,
test and delete answer "AlreadyInUse"; the app says so. Opening the app
means the phone is unlocked.
"""
from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

APP_ID = "org.fp5.Fingerprints"
BUS = "net.reactivated.Fprint"
MGR_PATH = "/net/reactivated/Fprint/Manager"
IFACE_MGR = "net.reactivated.Fprint.Manager"
IFACE_DEV = "net.reactivated.Fprint.Device"
IFACE_MANAGE = "org.fp5.Fprint.Manage"
ERR = "net.reactivated.Fprint.Error."

FINGERS = (
    "right-thumb", "right-index-finger", "right-middle-finger",
    "right-ring-finger", "right-little-finger",
    "left-thumb", "left-index-finger", "left-middle-finger",
    "left-ring-finger", "left-little-finger",
)
PRETTY = {
    "left-thumb": "Left thumb", "left-index-finger": "Left index finger",
    "left-middle-finger": "Left middle finger", "left-ring-finger": "Left ring finger",
    "left-little-finger": "Left little finger", "right-thumb": "Right thumb",
    "right-index-finger": "Right index finger", "right-middle-finger": "Right middle finger",
    "right-ring-finger": "Right ring finger", "right-little-finger": "Right little finger",
}
ENROLL_TEXT = {
    "enroll-stage-passed": "Good. Lift, then touch again, a little differently.",
    "enroll-retry-scan": "Not clear. Touch again.",
    "enroll-swipe-too-short": "Touch for a little longer.",
    "enroll-finger-not-centered": "Centre your finger on the power button.",
    "enroll-remove-and-retry": "Lift your finger and try again.",
    "enroll-data-full": "No room for another finger. Delete one first.",
    "enroll-duplicate": "This finger is already enrolled.",
    "enroll-completed": "Done. This finger now unlocks the phone.",
    "enroll-failed": "That did not work. Nothing was saved.",
    "enroll-disconnected": "The fingerprint reader went away.",
    "enroll-unknown-error": "Something went wrong. Nothing was saved.",
}
VERIFY_TEXT = {
    "verify-match": "Match. This finger is recognised.",
    "verify-no-match": "No match.",
    "verify-retry-scan": "Not clear. Touch again.",
    "verify-swipe-too-short": "Touch for a little longer.",
    "verify-finger-not-centered": "Centre your finger on the power button.",
    "verify-remove-and-retry": "Lift your finger and try again.",
    "verify-disconnected": "The fingerprint reader went away.",
    "verify-unknown-error": "Something went wrong.",
}
ERROR_TEXT = {
    "AlreadyInUse": "The reader is busy (the phone locks it while it is locked). Unlock the phone and try again.",
    "PermissionDenied": "Not allowed. Authenticate when asked.",
    "NoEnrolledPrints": "No such finger is enrolled.",
    "FingerAlreadyEnrolled": "That finger is already enrolled. Delete it first.",
    "PrintsNotDeleted": "The finger could not be deleted. Nothing was changed.",
    "InvalidFingername": "Not a valid finger name.",
}


def lockout_text(locked: bool, permanent: bool, remaining: int) -> str:
    """Banner text for the watcher's lockout, or "" when there is none."""
    if not locked:
        return ""
    if permanent:
        return "Too many attempts. Unlock with your PIN once to use your finger again."
    if remaining > 0:
        return f"Too many attempts. Fingerprint unlock is paused for {remaining} s."
    return "Too many attempts. Fingerprint unlock is paused."


def pretty(name: str) -> str:
    return PRETTY.get(name, name)


def friendly_error(err: GLib.Error) -> str:
    remote = Gio.DBusError.get_remote_error(err) or ""
    if remote.startswith(ERR):
        key = remote[len(ERR):]
        return ERROR_TEXT.get(key, err.message)
    if "ServiceUnknown" in remote or "NameHasNoOwner" in remote:
        return "The fingerprint service is not running."
    return err.message


class Client(GObject.Object):
    """Thin async wrapper over the fprintd Device object."""

    __gsignals__ = {
        "enroll-status": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        "verify-status": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        # locked-out, lockout-permanent, lockout-remaining (s), lockout-reason
        "lockout": (GObject.SignalFlags.RUN_FIRST, None, (bool, bool, int, str)),
    }

    def __init__(self) -> None:
        super().__init__()
        bus_type = Gio.BusType.SESSION if os.environ.get("FP5_FINGERPRINTS_BUS") == "session" else Gio.BusType.SYSTEM
        self.conn = Gio.bus_get_sync(bus_type, None)
        self.path: str | None = None
        self.stages = 16
        self._subs: list[int] = []

    def call(self, iface: str, method: str, params, cb, path: str | None = None,
             sig: str | None = None, timeout: int = 120_000) -> None:
        def done(conn, res):
            try:
                out = conn.call_finish(res)
            except GLib.Error as e:
                cb(None, e)
                return
            cb(out, None)
        self.conn.call(BUS, path or self.path, iface, method, params,
                       GLib.VariantType(sig) if sig else None,
                       Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, timeout, None, done)

    def connect_device(self, cb) -> None:
        """Find the default device and read num-enroll-stages."""
        def got_dev(out, err):
            if err:
                cb(err)
                return
            self.path = out.unpack()[0]
            for sig, name in (("EnrollStatus", "enroll-status"), ("VerifyStatus", "verify-status")):
                self._subs.append(self.conn.signal_subscribe(
                    BUS, IFACE_DEV, sig, self.path, None, Gio.DBusSignalFlags.NONE,
                    lambda c, s, p, i, sg, params, n=name: self.emit(n, *params.unpack())))

            # Lockout (fp5-fprintd extension; absent on a stock fprintd).
            self._subs.append(self.conn.signal_subscribe(
                BUS, IFACE_MANAGE, "LockoutChanged", self.path, None, Gio.DBusSignalFlags.NONE,
                lambda *_a: self.fetch_lockout()))
            self._subs.append(self.conn.signal_subscribe(
                BUS, "org.freedesktop.DBus.Properties", "PropertiesChanged", self.path,
                IFACE_MANAGE, Gio.DBusSignalFlags.NONE, lambda *_a: self.fetch_lockout()))

            def got_props(o, e):
                if not e:
                    self.stages = int(o.unpack()[0].get("num-enroll-stages", self.stages))
                cb(None)
                self.fetch_lockout()
            self.call("org.freedesktop.DBus.Properties", "GetAll",
                      GLib.Variant("(s)", (IFACE_DEV,)), got_props, sig="(a{sv})")
        self.call(IFACE_MGR, "GetDefaultDevice", None, got_dev, path=MGR_PATH, sig="(o)")

    def fetch_lockout(self) -> None:
        def got(out, err):
            p = {} if err else out.unpack()[0]
            self.emit("lockout", bool(p.get("locked-out", False)),
                      bool(p.get("lockout-permanent", False)),
                      int(p.get("lockout-remaining", 0)), str(p.get("lockout-reason", "")))
        self.call("org.freedesktop.DBus.Properties", "GetAll",
                  GLib.Variant("(s)", (IFACE_MANAGE,)), got, sig="(a{sv})", timeout=10_000)

    # -- fprintd calls; cb(result, error)
    def list_fingers(self, cb) -> None:
        def got(out, err):
            if err:
                if (Gio.DBusError.get_remote_error(err) or "").endswith("NoEnrolledPrints"):
                    cb([], None)
                else:
                    cb(None, err)
                return
            cb(list(out.unpack()[0]), None)
        self.call(IFACE_DEV, "ListEnrolledFingers", GLib.Variant("(s)", ("",)), got, sig="(as)")

    def claim(self, cb) -> None:
        self.call(IFACE_DEV, "Claim", GLib.Variant("(s)", ("",)), cb)

    def release(self, cb=lambda *_: None) -> None:
        self.call(IFACE_DEV, "Release", None, cb)

    def enroll_start(self, finger, cb) -> None:
        self.call(IFACE_DEV, "EnrollStart", GLib.Variant("(s)", (finger,)), cb)

    def enroll_stop(self, cb=lambda *_: None) -> None:
        self.call(IFACE_DEV, "EnrollStop", None, cb)

    def verify_start(self, cb) -> None:
        self.call(IFACE_DEV, "VerifyStart", GLib.Variant("(s)", ("any",)), cb)

    def verify_stop(self, cb=lambda *_: None) -> None:
        self.call(IFACE_DEV, "VerifyStop", None, cb)

    def delete(self, finger, cb) -> None:
        self.call(IFACE_DEV, "DeleteEnrolledFinger", GLib.Variant("(s)", (finger,)), cb, timeout=180_000)

    def relabel(self, old, new, cb) -> None:
        self.call(IFACE_MANAGE, "RelabelFinger", GLib.Variant("(ss)", (old, new)), cb, sig="(s)")


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, client: Client) -> None:
        super().__init__(application=app, title="Fingerprints", default_width=420, default_height=720)
        self.client = client
        self.fingers: list[str] = []
        self.op: str | None = None       # "enroll" | "verify" while an operation runs
        self.op_label: Gtk.Label | None = None
        self.op_button: Gtk.Button | None = None
        self.op_bar: Gtk.ProgressBar | None = None
        self.enroll_finger = ""
        self.enroll_passed = 0
        self.locked_out = False
        self.lock_permanent = False
        self.lock_remaining = 0
        self._lock_timer = 0
        self.overlay = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.overlay.set_child(self.nav)
        self.set_content(self.overlay)
        self.nav.add(self._main_page())
        client.connect("enroll-status", self._on_enroll_status)
        client.connect("verify-status", self._on_verify_status)
        client.connect("lockout", self._on_lockout)
        client.connect_device(self._on_connected)

    # ------------------------------------------------------------ main page
    def _main_page(self) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.lock_banner = Adw.Banner(revealed=False)
        view.add_top_bar(self.lock_banner)
        page = Adw.PreferencesPage()
        self.status = Adw.StatusPage(icon_name="auth-fingerprint-symbolic", title="Fingerprints",
                                     description="Connecting\u2026")
        self.status.add_css_class("compact")
        banner = Adw.PreferencesGroup()
        banner.add(self.status)
        page.add(banner)
        self.group = Adw.PreferencesGroup(title="Fingers that unlock this phone",
                                          description="Touch the power button with an enrolled finger to unlock.")
        page.add(self.group)
        actions = Adw.PreferencesGroup()
        self.add_row = Adw.ActionRow(title="Add a finger", subtitle="Takes about 16 touches", activatable=True)
        self.add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        self.add_row.connect("activated", lambda *_: self._pick_finger())
        self.test_row = Adw.ActionRow(title="Test", subtitle="Touch to see if your finger is recognised",
                                      activatable=True)
        self.test_row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
        self.test_row.connect("activated", lambda *_: self._start_test())
        actions.add(self.add_row)
        actions.add(self.test_row)
        page.add(actions)
        view.set_content(page)
        self._rows: list[Gtk.Widget] = []
        return Adw.NavigationPage(title="Fingerprints", child=view, tag="main")

    def _on_connected(self, err) -> None:
        if err:
            self.status.set_description(friendly_error(err))
            self.add_row.set_sensitive(False)
            self.test_row.set_sensitive(False)
            return
        self.refresh()

    def refresh(self) -> None:
        self.client.list_fingers(self._on_list)

    # -------------------------------------------------------------- lockout
    def _on_lockout(self, _c, locked: bool, permanent: bool, remaining: int, _reason: str) -> None:
        self.locked_out, self.lock_permanent, self.lock_remaining = locked, permanent, remaining
        self._show_lockout()
        if self._lock_timer:
            GLib.source_remove(self._lock_timer)
            self._lock_timer = 0
        if locked and not permanent and remaining > 0:
            self._lock_timer = GLib.timeout_add_seconds(1, self._lock_tick)

    def _lock_tick(self) -> bool:
        self.lock_remaining = max(0, self.lock_remaining - 1)
        if self.lock_remaining == 0:
            self._lock_timer = 0
            self.client.fetch_lockout()   # the daemon has the truth
            return False
        self._show_lockout()
        return True

    def _show_lockout(self) -> None:
        text = lockout_text(self.locked_out, self.lock_permanent, self.lock_remaining)
        if text:
            self.lock_banner.set_title(text)
        self.lock_banner.set_revealed(bool(text))

    def _on_list(self, fingers, err) -> None:
        for r in self._rows:
            self.group.remove(r)
        self._rows = []
        if err:
            self.status.set_description(friendly_error(err))
            return
        self.fingers = sorted(fingers, key=lambda n: FINGERS.index(n) if n in FINGERS else 99)
        n = len(self.fingers)
        self.status.set_description("No fingers enrolled. Unlock uses your PIN." if n == 0 else
                                    f"{n} finger{'s' if n != 1 else ''} enrolled")
        self.add_row.set_sensitive(n < len(FINGERS))
        self.test_row.set_sensitive(n > 0)
        for name in self.fingers:
            row = Adw.ActionRow(title=pretty(name))
            rename = Gtk.MenuButton(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Rename: which finger is this?")
            rename.add_css_class("flat")
            rename.set_popover(self._rename_popover(name))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Delete this finger")
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, nm=name: self._confirm_delete(nm))
            row.add_suffix(rename)
            row.add_suffix(delete)
            self.group.add(row)
            self._rows.append(row)

    # --------------------------------------------------------------- rename
    def _rename_popover(self, old: str) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lab = Gtk.Label(label="This is my\u2026", xalign=0, margin_start=8, margin_top=6, margin_bottom=4)
        lab.add_css_class("heading")
        box.append(lab)
        for name in FINGERS:
            if name == old:
                continue
            text = pretty(name) + ("  (swap)" if name in self.fingers else "")
            b = Gtk.Button(label=text, halign=Gtk.Align.FILL)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, o=old, nn=name, p=pop: (p.popdown(), self._do_rename(o, nn)))
            box.append(b)
        sc = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=420,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(box)
        pop.set_child(sc)
        return pop

    def _do_rename(self, old: str, new: str) -> None:
        def done(out, err):
            if err:
                self._toast(friendly_error(err))
            else:
                self._toast(f"{pretty(old)} is now {pretty(new)}" if out.unpack()[0] != "swapped"
                            else f"{pretty(old)} and {pretty(new)} swapped")
            self.refresh()
        self.client.relabel(old, new, done)

    # --------------------------------------------------------------- delete
    def _confirm_delete(self, name: str) -> None:
        last = len(self.fingers) == 1
        dlg = Adw.AlertDialog(
            heading=f"Delete {pretty(name).lower()}?",
            body=("This is your last finger. The phone will be unlocked with the PIN only."
                  if last else "This finger will no longer unlock the phone. Storage is backed up first."))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "delete" and self._do_delete(name))
        dlg.present(self)

    def _do_delete(self, name: str) -> None:
        def after_claim(_o, err):
            if err:
                self._toast(friendly_error(err))
                return
            def done(_o2, err2):
                self.client.release()
                self._toast(friendly_error(err2) if err2 else f"{pretty(name)} deleted")
                self.refresh()
            self.client.delete(name, done)
        self.client.claim(after_claim)

    # ---------------------------------------------------------------- enroll
    def _pick_finger(self) -> None:
        free = [n for n in FINGERS if n not in self.fingers]
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Which finger?",
                                 description="Use a finger you can hold on the power button.")
        for name in free:
            r = Adw.ActionRow(title=pretty(name), activatable=True)
            r.connect("activated", lambda _r, nm=name: self._start_enroll(nm))
            g.add(r)
        page.add(g)
        view.set_content(page)
        self.nav.push(Adw.NavigationPage(title="Add a finger", child=view, tag="pick"))

    def _progress_page(self, title: str, text: str, with_bar: bool) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_back_button=False)
        view.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                      margin_start=24, margin_end=24)
        icon = Gtk.Image(icon_name="auth-fingerprint-symbolic", pixel_size=96)
        self.op_label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
        self.op_label.add_css_class("title-3")
        box.append(icon)
        box.append(self.op_label)
        self.op_bar = Gtk.ProgressBar() if with_bar else None
        if self.op_bar:
            box.append(self.op_bar)
        self.op_button = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        self.op_button.add_css_class("pill")
        self.op_button.connect("clicked", lambda *_: self._op_button())
        box.append(self.op_button)
        view.set_content(box)
        return Adw.NavigationPage(title=title, child=view, tag="op", can_pop=False)

    def _start_enroll(self, finger: str) -> None:
        self.enroll_finger = finger
        self.enroll_passed = 0
        self.op = "enroll"

        def after_claim(_o, err):
            if err:
                self._toast(friendly_error(err))
                return
            def after_start(_o2, err2):
                if err2:
                    self.client.release()
                    self._toast(friendly_error(err2))
                    return
                self.nav.push(self._progress_page(
                    f"Add {pretty(finger).lower()}",
                    f"Touch the power button with your {pretty(finger).lower()}.", True))
            self.client.enroll_start(finger, after_start)
        self.client.claim(after_claim)

    def _on_enroll_status(self, _c, result: str, done: bool) -> None:
        if getattr(self, "op", None) != "enroll" or self.op_label is None:
            return
        if result == "enroll-stage-passed":
            self.enroll_passed += 1
            if self.op_bar:
                self.op_bar.set_fraction(min(1.0, self.enroll_passed / max(1, self.client.stages)))
        self.op_label.set_label(ENROLL_TEXT.get(result, result))
        if done:
            self._finish_op(ok=result == "enroll-completed")

    # ------------------------------------------------------------------ test
    def _start_test(self) -> None:
        self.op = "verify"

        def after_claim(_o, err):
            if err:
                self._toast(friendly_error(err))
                return
            def after_start(_o2, err2):
                if err2:
                    self.client.release()
                    self._toast(friendly_error(err2))
                    return
                self.nav.push(self._progress_page("Test", "Touch the power button.", False))
            self.client.verify_start(after_start)
        self.client.claim(after_claim)

    def _on_verify_status(self, _c, result: str, done: bool) -> None:
        if getattr(self, "op", None) != "verify" or self.op_label is None:
            return
        text = VERIFY_TEXT.get(result, result)
        if result == "verify-disconnected" and self.locked_out:
            # fp5-fprintd with FP5_FPRINTD_HONOR_LOCKOUT=1 ends a verify like this
            text = lockout_text(True, self.lock_permanent, self.lock_remaining)
        self.op_label.set_label(text)
        if done:
            self._finish_op(ok=result == "verify-match")

    # ----------------------------------------------------- shared op end
    def _stop_and_release(self, kind: str | None, then=lambda: None) -> None:
        stop = self.client.enroll_stop if kind == "enroll" else self.client.verify_stop
        stop(lambda *_: self.client.release(lambda *_: then()))

    def _op_button(self) -> None:
        """Cancel while an operation runs, Done afterwards."""
        if self.op is not None:
            kind, self.op = self.op, None
            self._stop_and_release(kind, lambda: (self.nav.pop(), self.refresh()))
        else:
            self.nav.pop()
            self.refresh()

    def _finish_op(self, ok: bool) -> None:
        kind, self.op = self.op, None
        self.op_button.set_label("Done")
        if ok:
            self.op_button.add_css_class("suggested-action")
        self._stop_and_release(kind)
        self.refresh()

    def _toast(self, text: str) -> None:
        self.overlay.add_toast(Adw.Toast(title=text))


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:
        win = self.props.active_window
        if win is None:
            try:
                client = Client()
            except GLib.Error as e:
                print(f"no D-Bus: {e.message}", file=sys.stderr)
                return
            win = Window(self, client)
        win.present()


def main() -> int:
    return App().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
