# SPDX-License-Identifier: GPL-3.0-or-later
"""One tile in the library grid."""

from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .store import ScoreObject

log = logging.getLogger(__name__)

THUMB_HEIGHT = 300
CARD_WIDTH = 250


class ScoreCard(Gtk.Box):
    """Reused by the GridView factory, so all per-item state is set up in
    bind() and torn down again in unbind()."""

    __gtype_name__ = "LeggioScoreCard"

    def __init__(self, on_context: Optional[Callable] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("score-card")
        self._object: Optional[ScoreObject] = None
        self._changed_handler = 0
        self._on_context = on_context

        overlay = Gtk.Overlay()

        self._stack = Gtk.Stack()
        self._stack.set_size_request(CARD_WIDTH, THUMB_HEIGHT)

        placeholder = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        placeholder.set_pixel_size(48)
        placeholder.add_css_class("card-thumb-placeholder")
        placeholder.set_vexpand(True)
        self._stack.add_named(placeholder, "placeholder")

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_can_shrink(True)
        self._picture.add_css_class("card-thumb")
        self._stack.add_named(self._picture, "thumb")

        overlay.set_child(self._stack)

        self._pages = Gtk.Label()
        self._pages.add_css_class("card-badge")
        self._pages.set_halign(Gtk.Align.END)
        self._pages.set_valign(Gtk.Align.END)
        overlay.add_overlay(self._pages)

        self._missing = Gtk.Label(label="Missing")
        self._missing.add_css_class("card-badge")
        self._missing.add_css_class("missing")
        self._missing.set_halign(Gtk.Align.START)
        self._missing.set_valign(Gtk.Align.START)
        self._missing.set_visible(False)
        overlay.add_overlay(self._missing)

        self.append(overlay)

        self._title = Gtk.Label(xalign=0.0)
        self._title.add_css_class("card-title")
        self._title.set_ellipsize(Pango.EllipsizeMode.END)
        self._title.set_lines(2)
        self._title.set_wrap(True)
        self._title.set_max_width_chars(1)
        self.append(self._title)

        self._composer = Gtk.Label(xalign=0.0)
        self._composer.add_css_class("dim-label")
        self._composer.add_css_class("card-subtitle")
        self._composer.set_ellipsize(Pango.EllipsizeMode.END)
        self._composer.set_max_width_chars(1)
        self.append(self._composer)

        # Right-click on a desktop, long-press on a touchscreen: same menu.
        secondary = Gtk.GestureClick.new()
        secondary.set_button(Gdk.BUTTON_SECONDARY)
        secondary.connect("pressed", self._on_secondary)
        self.add_controller(secondary)

        long_press = Gtk.GestureLongPress.new()
        long_press.set_touch_only(True)
        long_press.connect("pressed", self._on_long_press)
        self.add_controller(long_press)

    # ------------------------------------------------------------- binding

    def bind(self, obj: ScoreObject) -> None:
        self._object = obj
        self._changed_handler = obj.connect("changed", lambda _o: self._refresh())
        self._refresh()

    def unbind(self) -> None:
        if self._object is not None and self._changed_handler:
            self._object.disconnect(self._changed_handler)
        self._changed_handler = 0
        self._object = None

    def _refresh(self) -> None:
        if self._object is None:
            return
        score = self._object.score

        self._title.set_text(score.title)
        self._title.set_tooltip_text(f"{score.title}\n{score.path}")

        subtitle = score.composer or "Unknown composer"
        if score.tags:
            subtitle = f"{subtitle}  ·  {', '.join(score.tags)}"
        self._composer.set_text(subtitle)

        if score.page_count:
            self._pages.set_text(
                "1 page" if score.page_count == 1 else f"{score.page_count} pages"
            )
            self._pages.set_visible(True)
        else:
            self._pages.set_visible(False)

        self._missing.set_visible(score.missing)
        if score.missing:
            self.add_css_class("is-missing")
            self.set_tooltip_text(f"File not found:\n{score.path}")
        else:
            self.remove_css_class("is-missing")
            self.set_tooltip_text(None)

        thumb = self._object.thumb
        if thumb:
            try:
                texture = Gdk.Texture.new_from_filename(thumb)
            except GLib.Error as exc:
                log.debug("bad thumbnail %s: %s", thumb, exc)
                self._stack.set_visible_child_name("placeholder")
            else:
                self._picture.set_paintable(texture)
                self._stack.set_visible_child_name("thumb")
        else:
            self._stack.set_visible_child_name("placeholder")

    # ------------------------------------------------------------- context

    def _on_secondary(self, gesture, n_press, x, y) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_context(x, y)

    def _on_long_press(self, gesture, x, y) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_context(x, y)

    def _show_context(self, x: float, y: float) -> None:
        if self._on_context and self._object is not None:
            self._on_context(self, self._object, x, y)
