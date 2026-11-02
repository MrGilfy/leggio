# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the screen awake while a score is open.

Desktops disagree on how an application asks for this, so several methods
are tried in order and the first one that is confirmed to work is kept:

1. Wayland idle-inhibit protocol, through GTK. Used by GNOME, KDE, Sway,
   Hyprland and other wlroots compositors. Only tried when the compositor
   actually advertises the protocol, because otherwise GTK quietly falls
   back to the portal without saying whether that worked.
2. org.freedesktop.ScreenSaver over D-Bus. GNOME, KDE, XFCE, MATE,
   Cinnamon and LXQt all provide it, and a call to it either succeeds or
   fails straight away.
3. xdg-desktop-portal Inhibit. The portal answers each request, so a
   backend that refuses is detected. It comes after ScreenSaver because
   xdg-desktop-portal-gtk, the usual backend outside GNOME and KDE,
   answers "success" before it has forwarded the request anywhere, and
   with nothing to forward to the screen still blanks.

On a native X11 session the X server's own screen saver and DPMS timers
are also suspended, whatever else worked: on a bare window manager they are
the only thing that blanks the screen, and no D-Bus service controls them.

Which method is active is logged at INFO level whenever it changes. If
nothing works, a warning is logged every time a score opens, listing what
was tried and why it failed.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional, Protocol

from gi.repository import Gio, GLib

from . import native

log = logging.getLogger(__name__)

# Gtk.ApplicationInhibitFlags.IDLE and the portal's "idle" flag; the same
# value in both. Spelled out so this module does not need Gtk to import.
INHIBIT_IDLE = 8

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INHIBIT = "org.freedesktop.portal.Inhibit"
PORTAL_REQUEST = "org.freedesktop.portal.Request"

SCREENSAVER_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"

Done = Callable[..., None]


class Method(Protocol):
    """One way of keeping the screen awake.

    start() calls done(True) or done(False, reason), either straight away
    or later from the main loop. stop() releases whatever start() took and
    must be safe to call at any time, including twice.
    """

    label: str
    # Tried even after an earlier method has worked.
    stackable: bool

    def start(self, done: Done) -> None: ...

    def stop(self) -> None: ...


# --------------------------------------------------------------- session

class Session:
    """What kind of display Leggio is running on."""

    def __init__(self, display) -> None:
        type_name = display.__gtype__.name if display is not None else ""
        self.type_name = type_name
        self.display_name: str = display.get_name() if display is not None else ""
        self.wayland = type_name == "GdkWaylandDisplay"
        # An X11 display while a Wayland compositor is running is Xwayland:
        # blanking there is up to the compositor, not the X server.
        self.native_x11 = (
            type_name == "GdkX11Display"
            and not os.environ.get("WAYLAND_DISPLAY")
        )

    def describe(self) -> str:
        if self.wayland:
            return f"Wayland ({self.display_name})"
        if self.native_x11:
            return f"X11 ({self.display_name})"
        if self.type_name == "GdkX11Display":
            return f"Xwayland ({self.display_name})"
        return self.type_name or "no display"


def _session_bus() -> Optional[Gio.DBusConnection]:
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        log.debug("no session bus: %s", exc.message)
        return None


def _name_has_owner(bus: Gio.DBusConnection, name: str) -> bool:
    try:
        result = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 2000, None,
        )
        return bool(result.unpack()[0])
    except GLib.Error:
        return False


# --------------------------------------------------------------- methods

class WaylandProtocol:
    label = "Wayland idle-inhibit protocol"
    stackable = False

    def __init__(self, application, window, reason: str, session: Session):
        self._application = application
        self._window = window
        self._reason = reason
        self._session = session
        self._cookie = 0

    def start(self, done: Done) -> None:
        if not self._session.wayland:
            return done(False, "not a Wayland session")
        globals_ = native.wayland_globals(self._session.display_name)
        if globals_ is None:
            return done(False, "could not list the compositor's protocols")
        if native.WAYLAND_IDLE_INHIBIT not in globals_:
            return done(False, "the compositor does not support it")
        if self._window is None or self._window.get_surface() is None:
            return done(False, "the window is not on screen yet")
        self._cookie = self._application.inhibit(
            self._window, INHIBIT_IDLE, self._reason
        )
        if self._cookie:
            return done(True)
        return done(False, "GTK refused the request")

    def stop(self) -> None:
        if self._cookie:
            self._application.uninhibit(self._cookie)
            self._cookie = 0


class PortalInhibit:
    label = "xdg-desktop-portal Inhibit"
    stackable = False
    TIMEOUT_MS = 5000

    _counter = 0

    def __init__(self, reason: str, bus_getter=_session_bus):
        self._reason = reason
        self._bus_getter = bus_getter
        self._bus: Optional[Gio.DBusConnection] = None
        self._handle: Optional[str] = None
        self._expected: Optional[str] = None
        self._subscription = 0
        self._timeout = 0
        self._done: Optional[Done] = None

    def start(self, done: Done) -> None:
        self._bus = self._bus_getter()
        if self._bus is None:
            return done(False, "no session bus")
        try:
            self._bus.call_sync(
                PORTAL_NAME, PORTAL_PATH, "org.freedesktop.DBus.Properties",
                "Get", GLib.Variant("(ss)", (PORTAL_INHIBIT, "version")),
                None, Gio.DBusCallFlags.NONE, 2000, None,
            )
        except GLib.Error:
            return done(False, "not available")

        PortalInhibit._counter += 1
        token = f"leggio_{os.getpid()}_{PortalInhibit._counter}"
        sender = (self._bus.get_unique_name() or ":").lstrip(":").replace(".", "_")
        self._expected = f"{PORTAL_PATH}/request/{sender}/{token}"
        # Subscribed before the call, so an immediate answer is not missed.
        self._subscription = self._bus.signal_subscribe(
            PORTAL_NAME, PORTAL_REQUEST, "Response", None, None,
            Gio.DBusSignalFlags.NONE, self._on_response,
        )
        self._done = done
        try:
            result = self._bus.call_sync(
                PORTAL_NAME, PORTAL_PATH, PORTAL_INHIBIT, "Inhibit",
                GLib.Variant("(sua{sv})", ("", INHIBIT_IDLE, {
                    "handle_token": GLib.Variant("s", token),
                    "reason": GLib.Variant("s", self._reason),
                })),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
                self.TIMEOUT_MS, None,
            )
        except GLib.Error as exc:
            self._finish(False, exc.message)
            return
        self._handle = result.unpack()[0]
        if self._done is not None:
            self._timeout = GLib.timeout_add(self.TIMEOUT_MS, self._on_timeout)

    def _on_response(self, _conn, _sender, path, _iface, _signal, params) -> None:
        if self._done is None or path not in (self._expected, self._handle):
            return
        code = params.unpack()[0]
        if code == 0:
            self._finish(True)
        else:
            self._finish(False, f"the portal answered with code {code}")

    def _on_timeout(self) -> bool:
        self._timeout = 0
        self._finish(False, f"no answer within {self.TIMEOUT_MS // 1000} s")
        return False

    def _finish(self, ok: bool, reason: str = "") -> None:
        done, self._done = self._done, None
        self._unsubscribe()
        if not ok:
            self.stop()
        if done is not None:
            done(ok, reason)

    def _unsubscribe(self) -> None:
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        if self._subscription and self._bus is not None:
            self._bus.signal_unsubscribe(self._subscription)
            self._subscription = 0

    def stop(self) -> None:
        self._done = None
        self._unsubscribe()
        if self._handle and self._bus is not None:
            # Closing the request object ends the inhibition.
            self._bus.call(
                PORTAL_NAME, self._handle, PORTAL_REQUEST, "Close",
                None, None, Gio.DBusCallFlags.NONE, -1, None, None,
            )
        self._handle = None


class ScreenSaverInhibit:
    label = "org.freedesktop.ScreenSaver"
    stackable = False

    def __init__(self, reason: str, bus_getter=_session_bus):
        self._reason = reason
        self._bus_getter = bus_getter
        self._bus: Optional[Gio.DBusConnection] = None
        self._cookie: Optional[int] = None

    def start(self, done: Done) -> None:
        self._bus = self._bus_getter()
        if self._bus is None:
            return done(False, "no session bus")
        if not _name_has_owner(self._bus, SCREENSAVER_NAME):
            return done(False, "not running")
        try:
            result = self._bus.call_sync(
                SCREENSAVER_NAME, SCREENSAVER_PATH, SCREENSAVER_NAME,
                "Inhibit", GLib.Variant("(ss)", ("Leggio", self._reason)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 3000, None,
            )
        except GLib.Error as exc:
            return done(False, exc.message)
        self._cookie = result.unpack()[0]
        done(True)

    def stop(self) -> None:
        if self._cookie is None or self._bus is None:
            return
        try:
            self._bus.call_sync(
                SCREENSAVER_NAME, SCREENSAVER_PATH, SCREENSAVER_NAME,
                "UnInhibit", GLib.Variant("(u)", (self._cookie,)),
                None, Gio.DBusCallFlags.NONE, 3000, None,
            )
        except GLib.Error as exc:
            log.debug("UnInhibit failed: %s", exc.message)
        self._cookie = None


class X11ServerBlanking:
    label = "X server screen saver"
    stackable = True

    def __init__(self, session: Session):
        self._session = session
        self._saver: Optional[native.X11ScreenSaver] = None

    def start(self, done: Done) -> None:
        if not self._session.native_x11:
            return done(False, "not a native X11 session")
        self._saver = native.X11ScreenSaver(
            self._session.display_name,
            add_timeout=GLib.timeout_add_seconds,
            remove_timeout=GLib.source_remove,
        )
        how = self._saver.suspend()
        if how is None:
            self._saver = None
            return done(False, "could not open the X display")
        self.label = f"X server screen saver ({how})"
        done(True)

    def stop(self) -> None:
        if self._saver is not None:
            self._saver.resume()
            self._saver = None


# ------------------------------------------------------------- inhibitor

MethodFactory = Callable[[object, str], list]


class IdleInhibitor:
    def __init__(self, application, methods: Optional[MethodFactory] = None):
        self._application = application
        self._factory = methods or self._default_methods
        self._methods: list = []
        self._active: list = []
        self._failures: list[str] = []
        self._generation = 0
        self._running = False
        self._last_summary: Optional[str] = None

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def active_labels(self) -> list[str]:
        return [method.label for method in self._active]

    def _default_methods(self, window, reason: str) -> list:
        display = window.get_display() if window is not None else None
        session = Session(display)
        log.debug("idle inhibit: session is %s", session.describe())
        return [
            WaylandProtocol(self._application, window, reason, session),
            ScreenSaverInhibit(reason),
            PortalInhibit(reason),
            X11ServerBlanking(session),
        ]

    def inhibit(self, window, reason: str = "Viewing a score") -> None:
        if self._running:
            return
        self._running = True
        self._generation += 1
        self._methods = self._factory(window, reason)
        self._active = []
        self._failures = []
        self._try(self._generation, 0)

    def uninhibit(self) -> None:
        if not self._running:
            return
        self._running = False
        self._generation += 1
        for method in self._methods:
            method.stop()
        self._methods = []
        self._active = []

    def _try(self, generation: int, index: int) -> None:
        primary_found = any(not m.stackable for m in self._active)
        while index < len(self._methods):
            method = self._methods[index]
            if method.stackable or not primary_found:
                break
            index += 1
        if index >= len(self._methods):
            self._report()
            return

        def done(ok: bool, reason: str = "") -> None:
            if generation != self._generation:
                method.stop()       # answered after the viewer closed
                return
            if ok:
                self._active.append(method)
            else:
                self._failures.append(f"{method.label}: {reason}")
                method.stop()
            self._try(generation, index + 1)

        method.start(done)

    def _report(self) -> None:
        if not self._active:
            log.warning(
                "Nothing is keeping the screen awake, so it may turn off "
                "while this score is open. Tried: %s.",
                "; ".join(self._failures) or "no methods",
            )
            self._last_summary = None
            return
        summary = " + ".join(self.active_labels)
        if summary != self._last_summary:
            log.info("Keeping the screen awake with %s", summary)
            self._last_summary = summary
        else:
            log.debug("Keeping the screen awake with %s", summary)
        if self._failures:
            log.debug("idle inhibit: not used: %s", "; ".join(self._failures))
