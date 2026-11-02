# SPDX-License-Identifier: GPL-3.0-or-later
"""Small ctypes helpers for things GTK does not expose.

Two questions the idle inhibitor needs answered, neither of which has a
GTK or GObject-introspection API:

* Which Wayland protocols does the compositor offer? GTK uses the
  idle-inhibit protocol when it is there and silently falls back to the
  portal when it is not, so this is the only way to know which one GTK used.
* How to stop the X server's own screen saver and DPMS blanking, which is
  active on X11 sessions with no screen saver daemon (a bare window manager
  started from startx, for example) and is not reachable over D-Bus.

Everything here is best effort: a missing library or a failed call returns
None or False rather than raising.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

WAYLAND_IDLE_INHIBIT = "zwp_idle_inhibit_manager_v1"

# ------------------------------------------------------------------ Wayland

_WL_DISPLAY_GET_REGISTRY = 1

_GlobalFn = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_char_p, ctypes.c_uint32,
)
_GlobalRemoveFn = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
)


class _RegistryListener(ctypes.Structure):
    _fields_ = [("global_", _GlobalFn), ("global_remove", _GlobalRemoveFn)]


_wayland_cache: dict[str, Optional[frozenset[str]]] = {}


def wayland_globals(display_name: str) -> Optional[frozenset[str]]:
    """Interface names the Wayland compositor advertises, or None.

    Opens a second, short-lived connection to the same compositor GDK is
    using and lists its globals. The result is cached per display.
    """
    if display_name in _wayland_cache:
        return _wayland_cache[display_name]
    result = _query_wayland_globals(display_name)
    _wayland_cache[display_name] = result
    return result


def _query_wayland_globals(display_name: str) -> Optional[frozenset[str]]:
    try:
        lib = ctypes.CDLL("libwayland-client.so.0")
    except OSError as exc:
        log.debug("libwayland-client not loadable: %s", exc)
        return None

    lib.wl_display_connect.restype = ctypes.c_void_p
    lib.wl_display_connect.argtypes = [ctypes.c_char_p]
    lib.wl_display_disconnect.argtypes = [ctypes.c_void_p]
    lib.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
    lib.wl_display_roundtrip.restype = ctypes.c_int
    lib.wl_proxy_add_listener.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    lib.wl_proxy_add_listener.restype = ctypes.c_int
    lib.wl_proxy_destroy.argtypes = [ctypes.c_void_p]
    # Variadic, so no argtypes: every argument is passed with its C type.
    lib.wl_proxy_marshal_constructor.restype = ctypes.c_void_p

    display = lib.wl_display_connect(display_name.encode() or None)
    if not display:
        log.debug("could not connect to Wayland display %r", display_name)
        return None

    names: set[str] = set()

    def on_global(_data, _registry, _name, interface, _version):
        if interface:
            names.add(interface.decode("ascii", "replace"))

    listener = _RegistryListener(
        _GlobalFn(on_global), _GlobalRemoveFn(lambda *_args: None)
    )
    try:
        interface = ctypes.c_char.in_dll(lib, "wl_registry_interface")
        registry = lib.wl_proxy_marshal_constructor(
            ctypes.c_void_p(display),
            ctypes.c_uint32(_WL_DISPLAY_GET_REGISTRY),
            ctypes.byref(interface),
            ctypes.c_void_p(None),
        )
        if not registry:
            return None
        lib.wl_proxy_add_listener(registry, ctypes.byref(listener), None)
        ok = lib.wl_display_roundtrip(display) >= 0
        lib.wl_proxy_destroy(registry)
        return frozenset(names) if ok else None
    except (AttributeError, ValueError, OSError) as exc:
        log.debug("listing Wayland globals failed: %s", exc)
        return None
    finally:
        lib.wl_display_disconnect(display)


# ---------------------------------------------------------------------- X11

class X11ScreenSaver:
    """Keeps the X server's built-in screen saver and DPMS from blanking.

    Uses XScreenSaverSuspend from libXss, which suspends both for as long
    as this connection stays open. Without libXss it falls back to calling
    XResetScreenSaver every 30 seconds, which is what the screen saver
    timer counts from.
    """

    HEARTBEAT_SECONDS = 30

    def __init__(self, display_name: Optional[str],
                 add_timeout: Callable[[int, Callable[[], bool]], int],
                 remove_timeout: Callable[[int], None]):
        self._display_name = display_name
        self._add_timeout = add_timeout
        self._remove_timeout = remove_timeout
        self._x11 = None
        self._xss = None
        self._dpy = None
        self._heartbeat = 0
        self.method: Optional[str] = None

    def suspend(self) -> Optional[str]:
        """Start suppressing blanking; return how, or None on failure."""
        if self.method is not None:
            return self.method
        try:
            self._x11 = ctypes.CDLL("libX11.so.6")
        except OSError as exc:
            log.debug("libX11 not loadable: %s", exc)
            return None
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XFlush.argtypes = [ctypes.c_void_p]
        self._x11.XResetScreenSaver.argtypes = [ctypes.c_void_p]

        name = self._display_name.encode() if self._display_name else None
        self._dpy = self._x11.XOpenDisplay(name)
        if not self._dpy:
            log.debug("could not open X display %r", self._display_name)
            return None

        if self._suspend_extension(True):
            self.method = "XScreenSaverSuspend"
        else:
            self._x11.XResetScreenSaver(self._dpy)
            self._x11.XFlush(self._dpy)
            self._heartbeat = self._add_timeout(
                self.HEARTBEAT_SECONDS, self._tick
            )
            self.method = "XResetScreenSaver every 30 s"
        return self.method

    def resume(self) -> None:
        if self._dpy is None:
            return
        if self._heartbeat:
            self._remove_timeout(self._heartbeat)
            self._heartbeat = 0
        if self.method == "XScreenSaverSuspend":
            self._suspend_extension(False)
        self._x11.XCloseDisplay(self._dpy)
        self._dpy = None
        self.method = None

    def _tick(self) -> bool:
        if self._dpy is None:
            return False
        self._x11.XResetScreenSaver(self._dpy)
        self._x11.XFlush(self._dpy)
        return True

    def _suspend_extension(self, suspend: bool) -> bool:
        try:
            if self._xss is None:
                self._xss = ctypes.CDLL("libXss.so.1")
                self._xss.XScreenSaverQueryExtension.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                ]
                self._xss.XScreenSaverQueryExtension.restype = ctypes.c_int
                self._xss.XScreenSaverSuspend.argtypes = [
                    ctypes.c_void_p, ctypes.c_int,
                ]
            event, error = ctypes.c_int(), ctypes.c_int()
            if not self._xss.XScreenSaverQueryExtension(
                self._dpy, ctypes.byref(event), ctypes.byref(error)
            ):
                return False
            self._xss.XScreenSaverSuspend(self._dpy, 1 if suspend else 0)
            self._x11.XFlush(self._dpy)
            return True
        except (OSError, AttributeError) as exc:
            log.debug("XScreenSaver extension unavailable: %s", exc)
            return False
