# SPDX-License-Identifier: GPL-3.0-or-later
"""The viewer: one score, or a setlist, as a vertical strip of pages.

Position is (page, offset), offset being a fraction of the current page's
height, so half-page turns stay exact at any zoom or window size; it is what
last_page/last_offset store. A half-page turn adds 0.5, showing the bottom
of one page above the top of the next.

Annotation and crop editing live in annotate_mode.py and crop_mode.py,
jump links in link_mode.py, the page overview and bookmarks in overview.py.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import cairo  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .. import geometry, paging
from ..models import CropRect, Score
from ..render import PRIORITY_PREFETCH, make_key
from .annotate_mode import AnnotateModeMixin
from .controls import AnnotateBar, ControlBar, CropBar
from .crop_mode import CropModeMixin
from .link_mode import LinkModeMixin
from .overview import OverviewMixin

log = logging.getLogger(__name__)

GAP = 24                  # device px between pages in the strip
ZOOM_MIN, ZOOM_MAX = 0.25, 8.0
ZOOM_STEP = 1.25
TAP_SLOP = 16             # px of movement still counted as a tap
SWIPE_VELOCITY = 300.0    # px/s before a drag counts as a page-turn flick
RESIZE_DEBOUNCE_MS = 150
SAVE_DEBOUNCE_MS = 700


def _normalise_fit(value: object) -> str:
    """Accept the legacy "height" spelling from older configs and rows."""
    return "width" if value == "width" else "page"


class ViewerPage(AnnotateModeMixin, CropModeMixin, LinkModeMixin,
                 OverviewMixin, Adw.NavigationPage):
    """One score, or a setlist paged through end to end."""

    __gtype_name__ = "LeggioViewerPage"

    def __init__(self, ctx, scores: list[Score], label: str):
        super().__init__(title=label)
        self.ctx = ctx
        self.scores = scores
        self.index = 0

        self.page = 0
        self.offset = 0.0
        self.zoom = 1.0
        self.pan_x = 0.0

        config = ctx.config
        self.fit = _normalise_fit(config.get("viewer", "fit_mode", "page"))
        self.half = bool(config.get("viewer", "half_page", False))
        self.spread_mode = config.get("viewer", "spread", "auto")
        self.tap_zones = bool(config.get("viewer", "tap_zones", True))
        self.swipe_enabled = bool(config.get("viewer", "swipe", True))
        self.overlay_timeout = float(config.get("viewer", "overlay_timeout", 4.0))

        self.infos: dict[str, object] = {}
        self._suppress_tap = False
        self._interacting = False
        self._resize_pending = False
        self._resize_source = 0
        self._save_source = 0
        self._overlay_source = 0
        self._last_size = (0, 0)
        self._last_page_h = 0.0
        self._zoom_start = 1.0
        self._pan_last_x = 0.0
        self._pan_last_y = 0.0
        self._prefetch_signature = None
        # Pages shown by the last draw, for actions on "what is on screen".
        self._drawn_pages: list[int] = []

        self._init_annotate_mode()
        self._init_crop_mode()
        self._init_link_mode()

        self._key_controller: Optional[Gtk.EventControllerKey] = None

        self._build_ui()
        self._restore_position()
        self._ensure_info(self.index)

        self.connect("shown", self._on_shown)
        self.connect("hidden", self._on_hidden)

    # ------------------------------------------------------------------ ui

    def _build_ui(self) -> None:
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self._draw)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_focusable(True)
        self.area.connect("resize", self._on_resize)

        self._install_gestures()

        self.control_bar = ControlBar(self)
        self.crop_bar = CropBar(self)
        self.crop_bar.set_visible(False)
        self.annotate_bar = AnnotateBar(self)
        self.annotate_bar.set_visible(False)

        bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bars.append(self.control_bar)
        bars.append(self.crop_bar)
        bars.append(self.annotate_bar)
        bars.set_valign(Gtk.Align.START)
        bars.set_halign(Gtk.Align.FILL)
        bars.set_margin_top(12)
        bars.set_margin_start(12)
        bars.set_margin_end(12)

        self.overlay_revealer = Gtk.Revealer()
        self.overlay_revealer.set_child(bars)
        self.overlay_revealer.set_transition_type(
            Gtk.RevealerTransitionType.CROSSFADE
        )
        self.overlay_revealer.set_valign(Gtk.Align.START)
        self.overlay_revealer.set_reveal_child(False)

        self.status = Gtk.Label(label="Loading…")
        self.status.add_css_class("viewer-status")
        self.status.set_can_target(False)
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_valign(Gtk.Align.CENTER)

        overlay = Gtk.Overlay()
        overlay.set_child(self.area)
        overlay.add_overlay(self.status)
        overlay.add_overlay(self.overlay_revealer)
        for widget in self._build_link_widgets():
            overlay.add_overlay(widget)
        overlay.add_overlay(self._build_overview())

        self.set_child(overlay)

    def _install_gestures(self) -> None:
        click = Gtk.GestureClick.new()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_click_released)
        self.area.add_controller(click)

        swipe = Gtk.GestureSwipe.new()
        swipe.connect("swipe", self._on_swipe)
        self.area.add_controller(swipe)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.area.add_controller(drag)

        zoom = Gtk.GestureZoom.new()
        zoom.connect("begin", self._on_zoom_begin)
        zoom.connect("scale-changed", self._on_zoom_changed)
        zoom.connect("end", self._on_zoom_end)
        self.area.add_controller(zoom)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES
        )
        scroll.connect("scroll", self._on_scroll)
        self.area.add_controller(scroll)

    # ------------------------------------------------------------- lifecycle

    def _on_shown(self, *_args) -> None:
        window = self.get_root()
        if window is None:
            return
        window.fullscreen()
        self.ctx.inhibitor.inhibit(window, "Viewing sheet music")

        # Capture phase on the window: a page-turner pedal is a keyboard, and
        # a focused overlay button must not eat its keys.
        controller = Gtk.EventControllerKey.new()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        window.add_controller(controller)
        self._key_controller = controller

        self.area.grab_focus()
        self._sync_controls()

    def _on_hidden(self, *_args) -> None:
        window = self.get_root()
        self._flush_save()
        if self._key_controller is not None and window is not None:
            window.remove_controller(self._key_controller)
        self._key_controller = None
        self.ctx.inhibitor.uninhibit()
        if window is not None:
            window.unfullscreen()
        for source in (self._resize_source, self._overlay_source):
            if source:
                GLib.source_remove(source)
        self._resize_source = self._overlay_source = 0
        self._stop_flash()
        self.close_overview()

    def close_viewer(self) -> None:
        self.ctx.navigation.pop()

    def export_pdf(self) -> None:
        """Export this score, or the whole setlist, as an annotated PDF."""
        setlist = len(self.scores) > 1
        title = self.get_title() if setlist else self.score.title
        self.ctx.exporter.start(self, list(self.scores), title, setlist=setlist)

    # ----------------------------------------------------------- score state

    @property
    def score(self) -> Score:
        return self.scores[self.index]

    @property
    def rotation(self) -> int:
        return self.score.rotation % 360

    @property
    def crop(self) -> Optional[CropRect]:
        return self.score.crop

    @property
    def night(self) -> bool:
        return self.score.night_mode

    def _info(self, index: Optional[int] = None):
        score = self.scores[index if index is not None else self.index]
        return self.infos.get(score.path)

    def _page_count(self, index: Optional[int] = None) -> int:
        info = self._info(index)
        if info:
            return info.page_count
        return self.scores[index if index is not None else self.index].page_count

    def _ensure_info(self, index: int) -> None:
        if not (0 <= index < len(self.scores)):
            return
        score = self.scores[index]
        if score.path in self.infos:
            return
        self.ctx.render.request_info(
            score.path, lambda info, p=score.path: self._on_info(p, info)
        )

    def _on_info(self, path: str, info) -> None:
        if info is None:
            self.status.set_text("Could not open this file")
            self.status.set_visible(True)
            return
        self.infos[path] = info
        if path == self.score.path:
            self.status.set_visible(False)
            self._clamp_page()
            self._sync_controls()
        self.area.queue_draw()

    def _restore_position(self) -> None:
        score = self.score
        self.page = max(0, score.last_page)
        self.offset = min(max(score.last_offset, 0.0), 0.999)
        if score.fit_mode:
            self.fit = _normalise_fit(score.fit_mode)
        if score.spread_override in ("auto", "single", "double"):
            self.spread_mode = score.spread_override

    def _clamp_page(self) -> None:
        count = self._page_count()
        if count and self.page >= count:
            self.page = count - 1
            self.offset = 0.0
        if self.page < 0:
            self.page = 0

    # -------------------------------------------------------------- geometry

    def _spread_active(self, width: float, height: float) -> bool:
        if self.spread_mode == "double":
            return True
        if self.spread_mode == "single":
            return False
        return width > height

    def _half_active(self, width: float, height: float) -> bool:
        # A spread already shows two pages; halving it as well is nonsense.
        return self.half and not self._spread_active(width, height)

    def _content_size(self, page: int) -> tuple[float, float]:
        """Page size in points after cropping and rotation."""
        info = self._info()
        if info is None or not (0 <= page < len(info.page_sizes)):
            return (595.0, 842.0)
        box = self.crop if (self.crop and not self.crop_mode) else None
        return geometry.content_size(info.page_sizes[page], box, self.rotation)

    def _scale(self, width: float, height: float) -> float:
        content_w, content_h = self._content_size(self.page)
        if self._spread_active(width, height):
            base = min(height / content_h, ((width - GAP) / 2) / content_w)
        elif self.fit == "width":
            base = width / content_w
        else:
            # Contain, not fit-height: on a portrait screen fit-height would
            # push a portrait page past both side edges.
            base = min(height / content_h, width / content_w)
        return max(base, 0.01) * self.zoom

    # ----------------------------------------------------------------- draw

    def _draw(self, _area, ctx, width: int, height: int) -> None:
        factor = self.get_scale_factor() or 1
        # Device pixels throughout, so pages render at on-screen size and blit 1:1.
        ctx.scale(1.0 / factor, 1.0 / factor)
        dev_w, dev_h = width * factor, height * factor

        ctx.set_source_rgb(0.08, 0.08, 0.09)
        ctx.paint()
        # Reset first, so an early return never leaves another score's pages.
        self._drawn_pages = []
        self._link_spots = []

        if self._info() is None:
            return

        if self.crop_mode:
            self._draw_crop_mode(ctx, dev_w, dev_h)
            return

        scale = self._scale(dev_w, dev_h)
        spread = self._spread_active(dev_w, dev_h)
        count = self._page_count()
        # Rebuilt every frame; annotation input hit-tests against it.
        self._page_rects = []

        content_w, content_h = self._content_size(self.page)
        page_h = content_h * scale
        self._last_page_h = page_h

        y = -self.offset * page_h
        page = self.page
        drawn: list[int] = []

        while page < count and y < dev_h:
            content_w, content_h = self._content_size(page)
            width_px, height_px = content_w * scale, content_h * scale

            if spread:
                total = width_px * 2 + GAP
                left = (dev_w - total) / 2 + self.pan_x
                self._draw_page(ctx, page, left, y, width_px, height_px)
                drawn.append(page)
                if page + 1 < count:
                    self._draw_page(
                        ctx, page + 1, left + width_px + GAP, y,
                        width_px, height_px,
                    )
                    drawn.append(page + 1)
                page += 2
            else:
                left = (dev_w - width_px) / 2 + self.pan_x
                self._draw_page(ctx, page, left, y, width_px, height_px)
                drawn.append(page)
                page += 1

            y += height_px + GAP

        self._drawn_pages = drawn

        signature = (self.score.path, tuple(drawn), round(scale, 4), spread)
        if (not self._interacting and not self._resize_pending
                and signature != self._prefetch_signature):
            self._prefetch_signature = signature
            GLib.idle_add(
                self._prefetch, drawn, scale, spread, priority=GLib.PRIORITY_LOW
            )

    def _draw_page(self, ctx, page: int, x: float, y: float,
                   width: float, height: float) -> None:
        key = make_key(
            self.score.path, page, round(width), round(height),
            self.rotation, self.crop,
        )

        if self._interacting or self._resize_pending:
            # Mid-gesture or resize: cache only, no renders for a geometry
            # that is still changing.
            surface = self.ctx.render.cache.get(key)
        else:
            surface = self.ctx.render.request(key, self._on_rendered)

        ctx.save()
        if surface is not None:
            ctx.translate(x, y)
            ctx.set_source_surface(surface, 0, 0)
            ctx.paint()
        else:
            fallback = self.ctx.render.cache.find_similar(
                self.score.path, page, self.rotation, key.crop
            )
            if fallback is not None:
                # A briefly soft page beats a blank one.
                ctx.translate(x, y)
                ctx.scale(
                    width / fallback.get_width(), height / fallback.get_height()
                )
                ctx.set_source_surface(fallback, 0, 0)
                ctx.get_source().set_filter(cairo.Filter.GOOD)
                ctx.paint()
            else:
                ctx.set_source_rgb(1, 1, 1)
                ctx.rectangle(x, y, width, height)
                ctx.fill()
        ctx.restore()
        self._apply_night(ctx, x, y, width, height)

        self._page_rects.append((page, x, y, width, height))
        self._draw_annotations(ctx, page, x, y, width, height)
        self._draw_links(ctx, page, x, y, width, height, len(self._page_rects) - 1)

    def _apply_night(self, ctx, x: float, y: float,
                     width: float, height: float) -> None:
        """Night mode: invert the page just painted into this rectangle."""
        if not self.night:
            return
        ctx.save()
        ctx.set_operator(cairo.Operator.DIFFERENCE)
        ctx.set_source_rgb(1, 1, 1)
        ctx.rectangle(x, y, width, height)
        ctx.fill()
        ctx.restore()

    def _prefetch(self, drawn: list[int], scale: float, spread: bool) -> bool:
        if not drawn:
            return False
        count = self._page_count()
        candidates = [drawn[-1] + 1, drawn[0] - 1]
        if spread:
            candidates += [drawn[-1] + 2, drawn[0] - 2]
        for page in candidates:
            if not (0 <= page < count):
                continue
            content_w, content_h = self._content_size(page)
            key = make_key(
                self.score.path, page,
                round(content_w * scale), round(content_h * scale),
                self.rotation, self.crop,
            )
            self.ctx.render.request(
                key, self._on_rendered, priority=PRIORITY_PREFETCH
            )
        return False

    def _on_rendered(self, _key, _surface) -> bool:
        self.area.queue_draw()
        return False

    # --------------------------------------------------------------- resize

    def _on_resize(self, _area, width: int, height: int) -> None:
        if (width, height) == self._last_size:
            return
        self._last_size = (width, height)
        self._resize_pending = True
        # Drop queued renders for the geometry we just left behind.
        self.ctx.render.invalidate()
        if self._resize_source:
            GLib.source_remove(self._resize_source)
        self._resize_source = GLib.timeout_add(
            RESIZE_DEBOUNCE_MS, self._on_resize_settled
        )
        self._sync_controls()

    def _on_resize_settled(self) -> bool:
        self._resize_source = 0
        self._resize_pending = False
        self.area.queue_draw()
        return False

    # ------------------------------------------------------------ page turns

    def turn(self, delta: int) -> None:
        if self.crop_mode:
            return
        width, height = self._device_size()
        if self._half_active(width, height):
            self._advance(delta * 0.5)
        else:
            self._step(delta)
            self.offset = 0.0
        self.pan_x = 0.0
        self.back_state.turned()
        self.area.queue_draw()
        self._sync_controls()
        self._schedule_save()

    def _step(self, delta: int) -> bool:
        """Move one screen forward or back, crossing into the next or the
        previous score of a setlist. False at either end, where there is
        nowhere to go."""
        width, height = self._device_size()
        step = 2 if self._spread_active(width, height) else 1
        counts = [self._page_count(i) for i in range(len(self.scores))]
        index, page = paging.turn(self.index, self.page, delta, step, counts)
        if (index, page) == (self.index, self.page):
            return False
        if index != self.index:
            self._switch_score(index)
        self.page = page
        return True

    def _advance(self, amount: float) -> None:
        offset = self.offset + amount
        while offset >= 1.0:
            index = self.index
            if not self._step(1):
                offset = self._max_offset()
                break
            # A new score always opens at the top of its first page.
            offset = offset - 1.0 if index == self.index else 0.0
        while offset < 0.0:
            index = self.index
            if not self._step(-1):
                offset = 0.0
                break
            offset = offset + 1.0 if index == self.index else 0.0
        self.offset = offset

    def _max_offset(self) -> float:
        """How far the last page may scroll before its bottom edge lifts off
        the bottom of the screen."""
        _width, height = self._device_size()
        if self._last_page_h <= 0 or self._last_page_h <= height:
            return 0.0
        return max(0.0, 1.0 - height / self._last_page_h)

    def _switch_score(self, index: int) -> None:
        self._flush_save()
        self.index = index
        self.offset = 0.0
        self.pan_x = 0.0
        self.zoom = 1.0
        self._ensure_info(index)
        self._ensure_info(index + 1)
        self.ctx.db.update_score(self.score.id, last_opened=time.time())

    def _device_size(self) -> tuple[float, float]:
        factor = self.get_scale_factor() or 1
        return (
            self.area.get_width() * factor,
            self.area.get_height() * factor,
        )

    # --------------------------------------------------------------- toggles

    def set_fit_width(self, active: bool) -> None:
        if self.control_bar.updating:
            return
        self.fit = "width" if active else "page"
        self.zoom = 1.0
        self.pan_x = 0.0
        self.ctx.db.update_score(self.score.id, fit_mode=self.fit)
        self.score.fit_mode = self.fit
        self.area.queue_draw()

    def set_half_page(self, active: bool) -> None:
        if self.control_bar.updating:
            return
        self.half = active
        self.ctx.config.set("viewer", "half_page", active)
        self.ctx.config.save()
        if not active:
            self.offset = 0.0
        self.area.queue_draw()

    def set_night_mode(self, active: bool) -> None:
        if self.control_bar.updating:
            return
        self.score.night_mode = active
        self.ctx.db.update_score(self.score.id, night_mode=active)
        self.area.queue_draw()

    def set_spread(self, active: bool) -> None:
        if self.control_bar.updating:
            return
        self.spread_mode = "double" if active else "single"
        self.ctx.db.update_score(
            self.score.id, spread_override=self.spread_mode
        )
        self.score.spread_override = self.spread_mode
        self.offset = 0.0
        self.area.queue_draw()
        self._sync_controls()

    def rotate(self) -> None:
        rotation = (self.rotation + 90) % 360
        self.score.rotation = rotation
        self.ctx.db.update_score(self.score.id, rotation=rotation)
        self.zoom = 1.0
        self.pan_x = 0.0
        self.ctx.render.invalidate()
        self.area.queue_draw()

    def zoom_in(self) -> None:
        self._set_zoom(self.zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        self._set_zoom(self.zoom / ZOOM_STEP)

    def zoom_reset(self) -> None:
        self.pan_x = 0.0
        self._set_zoom(1.0)

    def _set_zoom(self, value: float) -> None:
        self.zoom = min(max(value, ZOOM_MIN), ZOOM_MAX)
        self.area.queue_draw()

    # --------------------------------------------------------------- overlay

    def toggle_overlay(self) -> None:
        if self.overlay_revealer.get_reveal_child():
            self._hide_overlay()
        else:
            self.show_overlay()

    def show_overlay(self) -> None:
        self._sync_controls()
        self.overlay_revealer.set_reveal_child(True)
        if self._overlay_source:
            GLib.source_remove(self._overlay_source)
        self._overlay_source = 0
        if self.overlay_timeout > 0 and not self.crop_mode:
            self._overlay_source = GLib.timeout_add(
                int(self.overlay_timeout * 1000), self._on_overlay_timeout
            )

    def _on_overlay_timeout(self) -> bool:
        self._overlay_source = 0
        if not self.crop_mode:
            self.overlay_revealer.set_reveal_child(False)
        return False

    def _hide_overlay(self) -> None:
        if self._overlay_source:
            GLib.source_remove(self._overlay_source)
            self._overlay_source = 0
        self.overlay_revealer.set_reveal_child(False)

    def _sync_controls(self) -> None:
        width, height = self._device_size()
        count = self._page_count()
        if len(self.scores) > 1:
            title = f"{self.index + 1}/{len(self.scores)}  ·  {self.score.title}"
        else:
            title = self.score.title
        self.control_bar.sync(
            title=title,
            page_text=f"{self.page + 1} / {count}" if count else "—",
            fit_width=self.fit == "width",
            half=self.half,
            night=self.night,
            spread=self._spread_active(width, height),
        )
        self._sync_back()

    # ------------------------------------------------------------ persistence

    def _schedule_save(self) -> None:
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(SAVE_DEBOUNCE_MS, self._flush_save)

    def _flush_save(self) -> bool:
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = 0
        try:
            self.ctx.db.update_score(
                self.score.id,
                last_page=self.page,
                last_offset=round(self.offset, 4),
                last_opened=time.time(),
            )
            self.score.last_page = self.page
            self.score.last_offset = self.offset
        except Exception:
            log.exception("could not save reading position")
        return False

    # -------------------------------------------------------------- keyboard

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        window = self.get_root()
        if self.ctx.navigation.get_visible_page() is not self:
            return False
        focus = window.get_focus()
        if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
            return False
        if state & Gdk.ModifierType.CONTROL_MASK:
            # Ctrl+Z is the one chord the viewer wants for itself.
            if self.annotate_mode and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
                self.annotation_undo()
                return True
            return False
        if state & Gdk.ModifierType.ALT_MASK:
            return False

        if self.overview_open:
            # Arrows and Enter belong to the page grid while it is open.
            if keyval in (Gdk.KEY_Escape, Gdk.KEY_g, Gdk.KEY_G):
                self.close_overview()
                return True
            if keyval in (Gdk.KEY_k, Gdk.KEY_K):
                self.add_bookmark_here()
                return True
            return False

        if self.annotate_mode:
            return self._annotate_key(keyval)

        if self.crop_mode:
            return self._crop_key(keyval)

        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up, Gdk.KEY_BackSpace,
                      Gdk.KEY_Up):
            self.turn(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space,
                      Gdk.KEY_Down, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.turn(1)
            return True
        if keyval == Gdk.KEY_Escape:
            if self.overlay_revealer.get_reveal_child():
                self._hide_overlay()
            else:
                self.close_viewer()
            return True
        if keyval == Gdk.KEY_Home:
            self.page, self.offset = 0, 0.0
            self.area.queue_draw()
            self._sync_controls()
            return True
        if keyval == Gdk.KEY_End:
            self.page = max(0, self._page_count() - 1)
            self.offset = 0.0
            self.area.queue_draw()
            self._sync_controls()
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.rotate()
            return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.enter_crop_mode()
            return True
        if keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.enter_annotate_mode()
            return True
        if keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self.set_half_page(not self.half)
            self._sync_controls()
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.set_fit_width(self.fit != "width")
            self._sync_controls()
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.zoom_in()
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.zoom_out()
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.zoom_reset()
            return True
        if keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self.set_night_mode(not self.night)
            self._sync_controls()
            return True
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self.toggle_overlay()
            return True
        if keyval in (Gdk.KEY_j, Gdk.KEY_J):
            self.follow_first_link()
            return True
        if keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self.jump_back()
            return True
        if keyval in (Gdk.KEY_g, Gdk.KEY_G):
            self.open_overview()
            return True
        if keyval in (Gdk.KEY_e, Gdk.KEY_E):
            self.export_pdf()
            return True
        if keyval in (Gdk.KEY_k, Gdk.KEY_K):
            self.add_bookmark_here()
            return True
        return False

    # -------------------------------------------------------------- gestures

    def _on_click_released(self, _gesture, _n_press, x, y) -> None:
        if self.crop_mode or self.annotate_mode:
            return
        # Decide on an idle callback so that a swipe or drag for the same
        # event sequence has already had its say.
        GLib.idle_add(self._resolve_tap, x, y)

    def _resolve_tap(self, x: float, y: float) -> bool:
        if self._suppress_tap:
            self._suppress_tap = False
            return False
        # A link marker wins over the tap zones, but only inside the marker.
        if self.link_tap(x, y):
            return False
        if not self.tap_zones:
            self.toggle_overlay()
            return False
        width = self.area.get_width()
        if width <= 0:
            return False
        if x < width / 3:
            self.turn(-1)
        elif x > width * 2 / 3:
            self.turn(1)
        else:
            self.toggle_overlay()
        return False

    def _on_swipe(self, _gesture, velocity_x, velocity_y) -> None:
        if (not self.swipe_enabled or self.crop_mode or self.annotate_mode
                or self.zoom > 1.01):
            return
        if abs(velocity_x) < SWIPE_VELOCITY or abs(velocity_x) < abs(velocity_y):
            return
        self._suppress_tap = True
        # Swipe left = move forward, matching the direction paper would go.
        self.turn(1 if velocity_x < 0 else -1)

    def _on_drag_begin(self, _gesture, x, y) -> None:
        if self.crop_mode:
            self._crop_grab = self._crop_hit_test(x, y)
            return
        if self.annotate_mode:
            factor = self.get_scale_factor() or 1
            self._annotate_press(x * factor, y * factor)
            return
        self._interacting = self.zoom > 1.01

    def _on_drag_update(self, gesture, offset_x, offset_y) -> None:
        if self.crop_mode:
            self._crop_drag(gesture, offset_x, offset_y)
            return
        if self.annotate_mode:
            ok, start_x, start_y = gesture.get_start_point()
            if ok:
                factor = self.get_scale_factor() or 1
                self._annotate_motion(
                    (start_x + offset_x) * factor, (start_y + offset_y) * factor
                )
            return
        if abs(offset_x) > TAP_SLOP or abs(offset_y) > TAP_SLOP:
            self._suppress_tap = True
        if self.zoom <= 1.01:
            return
        factor = self.get_scale_factor() or 1
        self.pan_x += offset_x * factor - self._pan_last_x
        self._pan_last_x = offset_x * factor
        if self._last_page_h > 0:
            delta = offset_y * factor - self._pan_last_y
            self.offset -= delta / self._last_page_h
            self._pan_last_y = offset_y * factor
        self.area.queue_draw()

    def _on_drag_end(self, _gesture, _offset_x, _offset_y) -> None:
        self._pan_last_x = 0.0
        self._pan_last_y = 0.0
        if self.crop_mode:
            self._crop_grab = None
            return
        if self.annotate_mode:
            self._annotate_release()
            return
        self._interacting = False
        self._advance(0.0)  # renormalise page/offset after a vertical pan
        self.area.queue_draw()
        self._schedule_save()

    def _on_zoom_begin(self, _gesture, _sequence) -> None:
        self._zoom_start = self.zoom
        self._interacting = True

    def _on_zoom_changed(self, _gesture, scale: float) -> None:
        self._suppress_tap = True
        self.zoom = min(max(self._zoom_start * scale, ZOOM_MIN), ZOOM_MAX)
        self.area.queue_draw()

    def _on_zoom_end(self, _gesture, _sequence) -> None:
        self._interacting = False
        self.area.queue_draw()

    def _on_scroll(self, _controller, _dx, dy) -> bool:
        if self.crop_mode or self.annotate_mode:
            return False
        if self._last_page_h > 0 and self.zoom > 1.01:
            self.offset += (dy * 60) / self._last_page_h
            self._advance(0.0)
        else:
            self.turn(1 if dy > 0 else -1)
        self.area.queue_draw()
        return True
