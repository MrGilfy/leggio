# SPDX-License-Identifier: GPL-3.0-or-later
"""Idle inhibitor: method ordering, fallbacks, logging and the portal."""

from __future__ import annotations

import logging

import pytest

from leggio.inhibit import IdleInhibitor, PortalInhibit, ScreenSaverInhibit, Session


class FakeMethod:
    def __init__(self, label, ok=True, reason="failed", stackable=False,
                 deferred=False):
        self.label = label
        self.ok = ok
        self.reason = reason
        self.stackable = stackable
        self.deferred = deferred
        self.pending = None
        self.started = 0
        self.stopped = 0

    def start(self, done):
        self.started += 1
        if self.deferred:
            self.pending = done
        else:
            done(self.ok, "" if self.ok else self.reason)

    def answer(self):
        done, self.pending = self.pending, None
        done(self.ok, "" if self.ok else self.reason)

    def stop(self):
        self.stopped += 1


def make(methods):
    return IdleInhibitor(application=None, methods=lambda _w, _r: methods)


def test_first_working_method_wins_and_later_ones_are_skipped():
    a = FakeMethod("a", ok=False, reason="nope")
    b = FakeMethod("b")
    c = FakeMethod("c")
    inhibitor = make([a, b, c])
    inhibitor.inhibit(None)
    assert inhibitor.active_labels == ["b"]
    assert c.started == 0
    assert a.stopped == 1


def test_stackable_method_runs_even_after_success():
    primary = FakeMethod("primary")
    x11 = FakeMethod("x11", stackable=True)
    inhibitor = make([primary, x11])
    inhibitor.inhibit(None)
    assert inhibitor.active_labels == ["primary", "x11"]


def test_stackable_method_alone_counts_as_active(caplog):
    caplog.set_level(logging.INFO, logger="leggio.inhibit")
    inhibitor = make([FakeMethod("a", ok=False), FakeMethod("x11", stackable=True)])
    inhibitor.inhibit(None)
    assert inhibitor.active
    assert "Keeping the screen awake with x11" in caplog.text


def test_warns_with_every_reason_when_nothing_works(caplog):
    caplog.set_level(logging.INFO, logger="leggio.inhibit")
    inhibitor = make([
        FakeMethod("wayland", ok=False, reason="not a Wayland session"),
        FakeMethod("portal", ok=False, reason="not available"),
    ])
    inhibitor.inhibit(None)
    assert not inhibitor.active
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wayland: not a Wayland session" in warnings[0].getMessage()
    assert "portal: not available" in warnings[0].getMessage()


def test_warns_again_on_every_open(caplog):
    caplog.set_level(logging.WARNING, logger="leggio.inhibit")
    methods = [FakeMethod("a", ok=False)]
    inhibitor = make(methods)
    for _ in range(3):
        inhibitor.inhibit(None)
        inhibitor.uninhibit()
    assert len(caplog.records) == 3


def test_info_only_when_the_method_changes(caplog):
    caplog.set_level(logging.INFO, logger="leggio.inhibit")
    inhibitor = make([FakeMethod("a")])
    for _ in range(3):
        inhibitor.inhibit(None)
        inhibitor.uninhibit()
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1


def test_uninhibit_stops_every_method():
    a, b = FakeMethod("a"), FakeMethod("b", stackable=True)
    inhibitor = make([a, b])
    inhibitor.inhibit(None)
    inhibitor.uninhibit()
    assert not inhibitor.active
    assert a.stopped == 1 and b.stopped == 1


def test_inhibit_twice_does_not_restart():
    a = FakeMethod("a")
    inhibitor = make([a])
    inhibitor.inhibit(None)
    inhibitor.inhibit(None)
    assert a.started == 1


def test_deferred_answer_then_fallback():
    slow = FakeMethod("portal", ok=False, deferred=True, reason="code 2")
    after = FakeMethod("screensaver")
    inhibitor = make([slow, after])
    inhibitor.inhibit(None)
    assert not inhibitor.active and after.started == 0
    slow.answer()
    assert inhibitor.active_labels == ["screensaver"]


def test_answer_after_close_is_released_not_activated():
    slow = FakeMethod("portal", deferred=True)
    inhibitor = make([slow])
    inhibitor.inhibit(None)
    inhibitor.uninhibit()
    stops = slow.stopped
    slow.answer()
    assert not inhibitor.active
    assert slow.stopped == stops + 1


def test_session_without_display():
    session = Session(None)
    assert not session.wayland and not session.native_x11
    assert session.describe() == "no display"


# ----------------------------------------------------------------- portal

class FakeBus:
    def __init__(self, inhibit_available=True):
        self.inhibit_available = inhibit_available
        self.handler = None
        self.closed = []
        self.unsubscribed = []

    def get_unique_name(self):
        return ":1.42"

    def call_sync(self, _name, _path, iface, method, params, *_rest):
        from gi.repository import GLib

        if method == "Get":
            if not self.inhibit_available:
                raise GLib.Error("No such interface")
            return GLib.Variant("(v)", (GLib.Variant("u", 3),))
        assert iface == "org.freedesktop.portal.Inhibit"
        token = params.unpack()[2]["handle_token"]
        self.handle = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
        return GLib.Variant("(o)", (self.handle,))

    def call(self, _name, path, _iface, method, *_rest):
        if method == "Close":
            self.closed.append(path)

    def signal_subscribe(self, *args):
        self.handler = args[-1]
        return 7

    def signal_unsubscribe(self, sub):
        self.unsubscribed.append(sub)

    def respond(self, code, path=None):
        from gi.repository import GLib

        self.handler(None, None, path or self.handle, None, "Response",
                     GLib.Variant("(ua{sv})", (code, {})))


@pytest.fixture
def results():
    return []


def test_portal_success(results):
    bus = FakeBus()
    portal = PortalInhibit("test", bus_getter=lambda: bus)
    portal.start(lambda ok, reason="": results.append((ok, reason)))
    assert results == []
    bus.respond(0)
    assert results == [(True, "")]
    assert bus.unsubscribed == [7]
    portal.stop()
    assert bus.closed == [bus.handle]


def test_portal_backend_failure_is_reported(results):
    bus = FakeBus()
    portal = PortalInhibit("test", bus_getter=lambda: bus)
    portal.start(lambda ok, reason="": results.append((ok, reason)))
    handle = bus.handle
    bus.respond(2)
    assert results == [(False, "the portal answered with code 2")]
    assert bus.closed == [handle]


def test_portal_ignores_other_requests(results):
    bus = FakeBus()
    portal = PortalInhibit("test", bus_getter=lambda: bus)
    portal.start(lambda ok, reason="": results.append((ok, reason)))
    bus.respond(0, path="/org/freedesktop/portal/desktop/request/1_42/other")
    assert results == []
    portal.stop()


def test_portal_without_inhibit_interface(results):
    bus = FakeBus(inhibit_available=False)
    portal = PortalInhibit("test", bus_getter=lambda: bus)
    portal.start(lambda ok, reason="": results.append((ok, reason)))
    assert results == [(False, "not available")]


# ------------------------------------------------------------ screensaver

class NoScreenSaverBus:
    """What the Flatpak's D-Bus proxy shows: the name has no owner."""

    def call_sync(self, _name, _path, _iface, method, *_rest):
        from gi.repository import GLib

        assert method == "NameHasOwner"
        return GLib.Variant("(b)", (False,))


@pytest.mark.parametrize("sandboxed, reason", [
    (False, "not running"),
    (True, "not available to the Flatpak"),
])
def test_screensaver_hidden_by_the_sandbox(results, sandboxed, reason):
    method = ScreenSaverInhibit("test", bus_getter=NoScreenSaverBus,
                                sandboxed=lambda: sandboxed)
    method.start(lambda ok, why="": results.append((ok, why)))
    assert results == [(False, reason)]


def test_without_screensaver_the_portal_is_used():
    """The order the Flatpak sees on X11: no Wayland protocol, no
    ScreenSaver, so the portal is what keeps the screen awake."""
    wayland = FakeMethod("wayland", ok=False, reason="not a Wayland session")
    screensaver = FakeMethod("screensaver", ok=False,
                             reason="not available to the Flatpak")
    portal = FakeMethod("portal")
    x11 = FakeMethod("x11", stackable=True)
    inhibitor = make([wayland, screensaver, portal, x11])
    inhibitor.inhibit(None)
    assert inhibitor.active_labels == ["portal", "x11"]
