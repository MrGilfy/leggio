# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the screen awake while a score is open.

Layered on purpose. GTK's own inhibitor is the right answer when something
implements the session-manager or portal interface, but on a bare compositor
session - niri started from a TTY, say - there may be nothing listening, and
gtk_application_inhibit() then returns 0. The org.freedesktop.ScreenSaver
fallback covers most idle daemons. If neither works we log once and carry
on: a viewer that refuses to open because it cannot stop the screen blanking
would be worse than one that blanks.
"""

from __future__ import annotations

import logging
from typing import Optional

from gi.repository import Gio, GLib, Gtk

log = logging.getLogger(__name__)

SCREENSAVER_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"


class IdleInhibitor:
    def __init__(self, application: Gtk.Application):
        self._application = application
        self._cookie = 0
        self._dbus_cookie: Optional[int] = None
        self._proxy: Optional[Gio.DBusProxy] = None
        self._warned = False

    @property
    def active(self) -> bool:
        return bool(self._cookie) or self._dbus_cookie is not None

    def inhibit(self, window: Gtk.Window, reason: str = "Viewing a score") -> None:
        if self.active:
            return

        self._cookie = self._application.inhibit(
            window, Gtk.ApplicationInhibitFlags.IDLE, reason
        )
        if self._cookie:
            log.debug("idle inhibited via GtkApplication (cookie %d)", self._cookie)
            return

        if self._dbus_inhibit(reason):
            return

        if not self._warned:
            self._warned = True
            log.info(
                "no idle inhibitor available; the screen may blank while "
                "a score is open"
            )

    def uninhibit(self) -> None:
        if self._cookie:
            self._application.uninhibit(self._cookie)
            self._cookie = 0
        if self._dbus_cookie is not None and self._proxy is not None:
            try:
                self._proxy.call_sync(
                    "UnInhibit",
                    GLib.Variant("(u)", (self._dbus_cookie,)),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except GLib.Error as exc:
                log.debug("UnInhibit failed: %s", exc.message)
            self._dbus_cookie = None

    def _dbus_inhibit(self, reason: str) -> bool:
        try:
            if self._proxy is None:
                self._proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    SCREENSAVER_NAME,
                    SCREENSAVER_PATH,
                    SCREENSAVER_NAME,
                    None,
                )
            # A proxy is created even with no owner on the bus, so probe
            # before trusting it.
            if self._proxy.get_name_owner() is None:
                return False
            result = self._proxy.call_sync(
                "Inhibit",
                GLib.Variant("(ss)", ("Leggio", reason)),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            self._dbus_cookie = result.unpack()[0]
            log.debug("idle inhibited via %s", SCREENSAVER_NAME)
            return True
        except GLib.Error as exc:
            log.debug("ScreenSaver inhibit unavailable: %s", exc.message)
            return False
