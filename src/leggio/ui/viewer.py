# SPDX-License-Identifier: GPL-3.0-or-later
"""The viewer.

Position is held as (page, offset) where offset is a *fraction* of the
current page's height rather than a pixel scroll. That choice makes
half-page turns exact regardless of zoom or window size, survives a resize
without drift, and is what gets persisted to last_page/last_offset.

Pages are laid out as a vertical strip: the current page at -offset, the
next below it. A half-page turn advances the strip by 0.5, so the screen
shows the bottom half of one page above the top half of the next - the
upcoming system is on screen before you commit to the turn.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import cairo  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..crop import detect_crop
from ..models import FULL_PAGE, Annotation, CropRect, Score
from ..pdf import PdfError, open_document
from ..render import PRIORITY_PREFETCH, make_key
from .controls import (
    HIGHLIGHT_COLOR,
    PEN_COLORS,
    PEN_WIDTHS,
    AnnotateBar,
    ControlBar,
    CropBar,
)
from .symbols import draw_span_symbol, draw_symbol, is_span, needs_text

log = logging.getLogger(__name__)

GAP = 24                  # device px between pages in the strip
ZOOM_MIN, ZOOM_MAX = 0.25, 8.0
ZOOM_STEP = 1.25
TAP_SLOP = 16             # px of movement still counted as a tap
SWIPE_VELOCITY = 300.0    # px/s before a drag counts as a page-turn flick
RESIZE_DEBOUNCE_MS = 150
SAVE_DEBOUNCE_MS = 700
CROP_HANDLE_PX = 44       # touch-sized grab area for the crop handles
ERASER_RADIUS = 22        # eraser reach in device px
# Stamped symbols are sized from the pen width. 14x put a fermata at twice
# the height of the staff it sat above; 5x matches engraved articulation.
SYMBOL_SCALE = 5.0


def _normalise_fit(value: object) -> str:
    """Accept the legacy "height" spelling from older configs and rows."""
    return "width" if value == "width" else "page"


class ViewerPage(Adw.NavigationPage):
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

        self.annotate_mode = False
        self.tool = "pen"                 # pen | highlighter | symbol | eraser
        self.pen_color = PEN_COLORS[0][0]
        self.pen_width = PEN_WIDTHS[1][0]
        self.symbol_glyph = "fermata"
        self._annotations: dict[tuple[int, int], list[Annotation]] = {}
        self._stroke: Optional[dict] = None
        self._eraser_pos: Optional[tuple[int, float, float]] = None
        self._span_draft: Optional[dict] = None
        self._undo_stack: list[tuple[str, object]] = []
        self._page_rects: list[tuple[int, float, float, float, float]] = []

        self.crop_mode = False
        self.crop_draft: Optional[CropRect] = None
        self._crop_rect_px: Optional[tuple[float, float, float, float]] = None
        self._crop_grab: Optional[str] = None

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

        # Capture phase, on the window: a Bluetooth page-turner pedal is
        # just a keyboard, and its keypresses must not be eaten by whatever
        # happens to hold focus (an overlay button, say) before we see them.
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

    def close_viewer(self) -> None:
        self.ctx.navigation.pop()

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

    def _info(self, index: Optional[int] = None):
        score = self.scores[index if index is not None else self.index]
        return self.infos.get(score.path)

    def _page_count(self) -> int:
        info = self._info()
        return info.page_count if info else self.score.page_count

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
        page_w, page_h = info.page_sizes[page]
        box = self.crop if (self.crop and not self.crop_mode) else None
        if box:
            page_w *= box.width
            page_h *= box.height
        if self.rotation in (90, 270):
            page_w, page_h = page_h, page_w
        return (max(page_w, 1.0), max(page_h, 1.0))

    def _scale(self, width: float, height: float) -> float:
        content_w, content_h = self._content_size(self.page)
        if self._spread_active(width, height):
            base = min(height / content_h, ((width - GAP) / 2) / content_w)
        elif self.fit == "width":
            base = width / content_w
        else:
            # Contain, not literally fit-height. On a portrait display a
            # portrait page scaled to the screen height comes out wider
            # than the screen and loses both margins; whichever axis binds
            # first has to win.
            base = min(height / content_h, width / content_w)
        return max(base, 0.01) * self.zoom

    # ----------------------------------------------------------------- draw

    def _draw(self, _area, ctx, width: int, height: int) -> None:
        factor = self.get_scale_factor() or 1
        # Everything below works in device pixels, so a page renders at
        # exactly its on-screen pixel size and the blit is 1:1.
        ctx.scale(1.0 / factor, 1.0 / factor)
        dev_w, dev_h = width * factor, height * factor

        ctx.set_source_rgb(0.08, 0.08, 0.09)
        ctx.paint()

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
            # Mid-gesture or mid-resize: use whatever is already in the
            # cache rather than queueing renders for a geometry that is
            # still changing.
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

        self._page_rects.append((page, x, y, width, height))
        self._draw_annotations(ctx, page, x, y, width, height)

    def _prefetch(self, drawn: list[int], scale: float, spread: bool) -> bool:
        if not drawn:
            return False
        count = self._page_count()
        candidates = []
        if drawn:
            candidates.append(drawn[-1] + 1)
            if spread:
                candidates.append(drawn[-1] + 2)
            candidates.append(drawn[0] - 1)
            if spread:
                candidates.append(drawn[0] - 2)
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
            step = 2 if self._spread_active(width, height) else 1
            self.page += delta * step
            self.offset = 0.0
            self._settle_page()
        self.pan_x = 0.0
        self.area.queue_draw()
        self._sync_controls()
        self._schedule_save()

    def _advance(self, amount: float) -> None:
        self.offset += amount
        while self.offset >= 1.0:
            self.offset -= 1.0
            self.page += 1
        while self.offset < 0.0:
            self.offset += 1.0
            self.page -= 1
        self._settle_page()

    def _settle_page(self) -> None:
        """Clamp within the score, crossing into the next one in a setlist."""
        count = self._page_count()
        while self.page >= count:
            if self.index + 1 < len(self.scores):
                overflow = self.page - count
                self._switch_score(self.index + 1)
                self.page = overflow
                count = self._page_count()
            else:
                self.page = max(0, count - 1)
                self.offset = min(self.offset, self._max_offset())
                return
        while self.page < 0:
            if self.index > 0:
                self._switch_score(self.index - 1)
                count = self._page_count()
                self.page += count
            else:
                self.page = 0
                self.offset = 0.0
                return

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
            spread=self._spread_active(width, height),
            spread_sensitive=True,
        )

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

        if self.annotate_mode:
            if keyval == Gdk.KEY_Escape:
                self.exit_annotate_mode()
                return True
            # Page turns still work, so a pedal keeps advancing the score
            # while a pen is in your other hand.
            if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up, Gdk.KEY_Up):
                self.turn(-1)
                return True
            if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space,
                          Gdk.KEY_Down):
                self.turn(1)
                return True
            for key, tool in ((Gdk.KEY_p, "pen"), (Gdk.KEY_e, "eraser"),
                              (Gdk.KEY_s, "symbol")):
                if keyval == key:
                    self.set_tool(tool)
                    return True
            return False

        if self.crop_mode:
            if keyval == Gdk.KEY_Escape:
                self.exit_crop_mode(save=False)
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self.exit_crop_mode(save=True)
                return True
            return False

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
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self.toggle_overlay()
            return True
        return False

    # -------------------------------------------------------------- gestures

    def _on_click_released(self, gesture, n_press, x, y) -> None:
        if self.crop_mode or self.annotate_mode:
            return
        # Decide on an idle callback so that a swipe or drag for the same
        # event sequence has already had its say.
        GLib.idle_add(self._resolve_tap, x, y)

    def _resolve_tap(self, x: float, y: float) -> bool:
        if self._suppress_tap:
            self._suppress_tap = False
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

    # ------------------------------------------------------ annotation model

    def _annotations_for(self, page: int) -> list[Annotation]:
        key = (self.score.id, page)
        cached = self._annotations.get(key)
        if cached is None:
            cached = self.ctx.db.annotations_for(self.score.id, page)
            self._annotations[key] = cached
        return cached

    def _reload_annotations(self, page: int) -> None:
        self._annotations.pop((self.score.id, page), None)

    def _rot_point(self, u: float, v: float) -> tuple[float, float]:
        """Page space -> display space (before the crop is applied)."""
        rotation = self.rotation
        if rotation == 90:
            return (1 - v, u)
        if rotation == 180:
            return (1 - u, 1 - v)
        if rotation == 270:
            return (v, 1 - u)
        return (u, v)

    def _unrot_point(self, a: float, b: float) -> tuple[float, float]:
        rotation = self.rotation
        if rotation == 90:
            return (b, 1 - a)
        if rotation == 180:
            return (1 - a, 1 - b)
        if rotation == 270:
            return (1 - b, a)
        return (a, b)

    def _page_to_screen(self, u, v, x, y, w, h) -> tuple[float, float]:
        a, b = self._rot_point(u, v)
        box = self._to_display(self.crop or FULL_PAGE)
        return (
            x + (a - box.x0) / max(box.width, 1e-9) * w,
            y + (b - box.y0) / max(box.height, 1e-9) * h,
        )

    def _screen_to_page(self, sx, sy, x, y, w, h) -> tuple[float, float]:
        box = self._to_display(self.crop or FULL_PAGE)
        a = box.x0 + (sx - x) / max(w, 1e-9) * box.width
        b = box.y0 + (sy - y) / max(h, 1e-9) * box.height
        return self._unrot_point(a, b)

    def _page_unit(self, w: float) -> float:
        """Pixels per unit of page width — the scale annotation widths and
        symbol sizes are expressed in."""
        box = self._to_display(self.crop or FULL_PAGE)
        return w / max(box.width, 1e-9)

    def _current_ink(self) -> tuple[str, float]:
        if self.tool == "highlighter":
            return HIGHLIGHT_COLOR, max(self.pen_width * 4.0, 0.012)
        return self.pen_color, self.pen_width

    @staticmethod
    def _rgba(color: str) -> tuple[float, float, float, float]:
        try:
            r = int(color[1:3], 16) / 255
            g = int(color[3:5], 16) / 255
            b = int(color[5:7], 16) / 255
            a = int(color[7:9], 16) / 255 if len(color) >= 9 else 1.0
            return (r, g, b, a)
        except (ValueError, IndexError):
            return (0.85, 0.15, 0.15, 1.0)

    # ---------------------------------------------------- annotation drawing

    def _draw_annotations(self, ctx, page, x, y, w, h) -> None:
        annotations = self._annotations_for(page)
        live = self._stroke if (self._stroke and self._stroke["page"] == page) else None
        eraser = (
            self._eraser_pos
            if (self._eraser_pos and self._eraser_pos[0] == page) else None
        )
        if not annotations and live is None and eraser is None:
            return

        unit = self._page_unit(w)
        ctx.save()
        # Never let ink spill past the page it belongs to - it would land on
        # the neighbouring page in a spread or half-page view.
        ctx.rectangle(x, y, w, h)
        ctx.clip()
        ctx.set_line_cap(cairo.LineCap.ROUND)
        ctx.set_line_join(cairo.LineJoin.ROUND)

        for annotation in annotations:
            self._draw_annotation(ctx, annotation, x, y, w, h, unit)

        if live is not None and live["points"]:
            color, width = self._current_ink()
            self._draw_ink(ctx, live["points"], color, width, x, y, w, h, unit)

        draft = self._span_draft
        if draft is not None and draft["page"] == page:
            ctx.set_source_rgba(*self._rgba(self.pen_color))
            self._draw_symbol_at(
                ctx, draft["glyph"], draft["x"], draft["y"],
                max(self.pen_width * SYMBOL_SCALE * unit, 4.0),
                x, y, w, h, span=draft["span"],
            )

        if eraser is not None:
            _page, eu, ev = eraser
            ex, ey = self._page_to_screen(eu, ev, x, y, w, h)
            ctx.set_source_rgba(1, 1, 1, 0.85)
            ctx.set_line_width(2)
            ctx.arc(ex, ey, ERASER_RADIUS * unit, 0, 2 * math.pi)
            ctx.stroke()

        ctx.restore()

    def _draw_annotation(self, ctx, annotation, x, y, w, h, unit) -> None:
        if annotation.kind == Annotation.SYMBOL:
            data = annotation.data
            ctx.set_source_rgba(*self._rgba(annotation.color))
            self._draw_symbol_at(
                ctx, data.get("glyph", "fermata"),
                data.get("x", 0.5), data.get("y", 0.5),
                max(data.get("size", 0.05) * unit, 4.0),
                x, y, w, h,
                span=data.get("span"), label=data.get("label"),
            )
        else:
            self._draw_ink(ctx, annotation.points, annotation.color,
                           annotation.width, x, y, w, h, unit)

    def _draw_symbol_at(self, ctx, glyph, u, v, size, x, y, w, h,
                        span=None, label=None) -> None:
        """Place a symbol in page space, oriented with the page.

        A rotated score must carry its markings round with it, so the
        context is rotated to match. Spanning symbols are drawn along the
        vector between their two page-space endpoints, which makes them
        follow the rotation for free.
        """
        sx, sy = self._page_to_screen(u, v, x, y, w, h)
        if span is not None and is_span(glyph):
            ex, ey = self._page_to_screen(u + span, v, x, y, w, h)
            length = math.hypot(ex - sx, ey - sy)
            angle = math.atan2(ey - sy, ex - sx)
            ctx.save()
            ctx.translate(sx, sy)
            ctx.rotate(angle)
            draw_span_symbol(ctx, glyph, 0, 0, length, size)
            ctx.restore()
            return
        ctx.save()
        ctx.translate(sx, sy)
        ctx.rotate(math.radians(self.rotation))
        draw_symbol(ctx, glyph, 0, 0, size, label=label)
        ctx.restore()

    def _draw_ink(self, ctx, points, color, width, x, y, w, h, unit) -> None:
        if not points:
            return
        screen = [self._page_to_screen(u, v, x, y, w, h) for u, v in points]
        ctx.set_source_rgba(*self._rgba(color))
        line_width = max(width * unit, 1.0)
        ctx.set_line_width(line_width)

        if len(screen) == 1:
            ctx.arc(screen[0][0], screen[0][1], line_width / 2, 0, 2 * math.pi)
            ctx.fill()
            return

        ctx.move_to(*screen[0])
        if len(screen) == 2:
            ctx.line_to(*screen[1])
        else:
            # Quadratic through the midpoints: cheap smoothing that takes
            # the jitter out of a finger-drawn stroke.
            for i in range(1, len(screen) - 1):
                mx = (screen[i][0] + screen[i + 1][0]) / 2
                my = (screen[i][1] + screen[i + 1][1]) / 2
                self._quad_to(ctx, screen[i][0], screen[i][1], mx, my)
            ctx.line_to(*screen[-1])
        ctx.stroke()

    @staticmethod
    def _quad_to(ctx, qx, qy, ex, ey) -> None:
        x0, y0 = ctx.get_current_point()
        ctx.curve_to(
            x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
            ex + 2 / 3 * (qx - ex), ey + 2 / 3 * (qy - ey),
            ex, ey,
        )

    # ------------------------------------------------------ annotation input

    def _hit_page(self, sx: float, sy: float):
        """Which drawn page a screen point lands on, and its rectangle."""
        for page, x, y, w, h in self._page_rects:
            if x <= sx <= x + w and y <= sy <= y + h:
                return (page, x, y, w, h)
        # Off the page: fall back to the nearest one so a stroke that
        # strays into the margin still lands somewhere sensible.
        best = None
        best_distance = None
        for page, x, y, w, h in self._page_rects:
            dx = max(x - sx, 0, sx - (x + w))
            dy = max(y - sy, 0, sy - (y + h))
            distance = dx * dx + dy * dy
            if best_distance is None or distance < best_distance:
                best, best_distance = (page, x, y, w, h), distance
        return best

    def _annotate_press(self, sx: float, sy: float) -> None:
        target = self._hit_page(sx, sy)
        if target is None:
            return
        page, x, y, w, h = target
        u, v = self._screen_to_page(sx, sy, x, y, w, h)

        if self.tool == "eraser":
            self._eraser_pos = (page, u, v)
            self._erase_at(page, u, v, ERASER_RADIUS / self._page_unit(w) * 1.0)
        elif self.tool == "symbol":
            glyph = self.symbol_glyph
            if is_span(glyph):
                # A slur is as long as its phrase; there is no sensible
                # fixed size, so these are dragged out.
                self._span_draft = {"page": page, "glyph": glyph, "x": u,
                                    "y": v, "span": 0.0, "rect": (x, y, w, h)}
            elif needs_text(glyph):
                self._prompt_symbol_text(page, u, v)
            else:
                self._place_symbol(page, u, v)
        else:
            self._stroke = {"page": page, "points": [(u, v)],
                            "rect": (x, y, w, h)}
        self.area.queue_draw()

    def _annotate_motion(self, sx: float, sy: float) -> None:
        if self.tool == "eraser":
            target = self._hit_page(sx, sy)
            if target is None:
                return
            page, x, y, w, h = target
            u, v = self._screen_to_page(sx, sy, x, y, w, h)
            self._eraser_pos = (page, u, v)
            self._erase_at(page, u, v, ERASER_RADIUS / self._page_unit(w))
            self.area.queue_draw()
            return

        draft = self._span_draft
        if draft is not None:
            x, y, w, h = draft["rect"]
            u, _v = self._screen_to_page(sx, sy, x, y, w, h)
            draft["span"] = max(u - draft["x"], 0.0)
            self.area.queue_draw()
            return

        if self._stroke is None:
            return
        x, y, w, h = self._stroke["rect"]
        u, v = self._screen_to_page(sx, sy, x, y, w, h)
        points = self._stroke["points"]
        # Thin the input: a raw motion stream is far denser than the ink
        # needs, and every point is bytes in the database forever.
        last = points[-1]
        if math.hypot(u - last[0], v - last[1]) * self._page_unit(w) < 1.5:
            return
        points.append((u, v))
        self.area.queue_draw()

    def _annotate_release(self) -> None:
        self._eraser_pos = None

        draft = self._span_draft
        if draft is not None:
            self._span_draft = None
            # A tap with no drag would be an invisible zero-length slur.
            if draft["span"] >= 0.01:
                self._place_symbol(
                    draft["page"], draft["x"], draft["y"],
                    glyph=draft["glyph"], span=round(draft["span"], 5),
                )
            self.area.queue_draw()
            return

        stroke = self._stroke
        self._stroke = None
        if stroke is None or not stroke["points"]:
            self.area.queue_draw()
            return

        color, width = self._current_ink()
        points = [
            [round(min(max(u, 0.0), 1.0), 5), round(min(max(v, 0.0), 1.0), 5)]
            for u, v in stroke["points"]
        ]
        annotation = Annotation(
            id=0, score_id=self.score.id, page=stroke["page"],
            kind=Annotation.INK, color=color, width=width,
            data={"points": points}, created=time.time(),
        )
        annotation.id = self.ctx.db.add_annotation(annotation)
        self._reload_annotations(stroke["page"])
        self._undo_stack.append(("add", [annotation.id]))
        self._sync_annotate_bar()
        self.area.queue_draw()

    def _place_symbol(self, page: int, u: float, v: float,
                      glyph: Optional[str] = None,
                      span: Optional[float] = None,
                      label: Optional[str] = None) -> None:
        data = {
            "glyph": glyph or self.symbol_glyph,
            "x": round(u, 5),
            "y": round(v, 5),
            "size": round(self.pen_width * SYMBOL_SCALE, 5),
        }
        if span is not None:
            data["span"] = span
        if label:
            data["label"] = label
        annotation = Annotation(
            id=0, score_id=self.score.id, page=page, kind=Annotation.SYMBOL,
            color=self.pen_color, width=self.pen_width,
            data=data,
            created=time.time(),
        )
        annotation.id = self.ctx.db.add_annotation(annotation)
        self._reload_annotations(page)
        self._undo_stack.append(("add", [annotation.id]))
        self._sync_annotate_bar()

    def _prompt_symbol_text(self, page: int, u: float, v: float) -> None:
        glyph = self.symbol_glyph
        metronome = glyph == "metronome"
        dialog = Adw.AlertDialog(
            heading="Metronome mark" if metronome else "Chord symbol",
            body="Beats per minute" if metronome else "For example Cmaj7 or G/B",
        )
        entry = Gtk.Entry()
        entry.set_text("120" if metronome else "")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("place", "Place")
        dialog.set_response_appearance("place", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("place")
        dialog.connect("response", self._on_symbol_text, entry, page, u, v, glyph)
        dialog.present(self)

    def _on_symbol_text(self, _dialog, response, entry, page, u, v, glyph) -> None:
        if response != "place":
            return
        text = entry.get_text().strip()
        if not text:
            return
        self._place_symbol(page, u, v, glyph=glyph, label=text)
        self.area.queue_draw()

    @staticmethod
    def _segment_distance(px, py, x0, y0, x1, y1) -> float:
        dx, dy = x1 - x0, y1 - y0
        if dx == 0.0 and dy == 0.0:
            return math.hypot(px - x0, py - y0)
        t = ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))

    def _erase_at(self, page: int, u: float, v: float, radius: float) -> None:
        """Whole-stroke erase.

        Deleting the entire stroke you touched is far more predictable at
        arm's length than nibbling pixels out of it, and it keeps every
        annotation a single undoable object.
        """
        hits = []
        for annotation in self._annotations_for(page):
            if annotation.kind == Annotation.SYMBOL:
                data = annotation.data
                reach = max(radius, data.get("size", 0.05) * 0.6)
                sx, sy = data.get("x", 0), data.get("y", 0)
                span = data.get("span")
                if span is not None:
                    # Anywhere along a slur or hairpin should erase it, not
                    # just its left-hand end.
                    hit = self._segment_distance(u, v, sx, sy, sx + span, sy)
                else:
                    hit = math.hypot(u - sx, v - sy)
                if hit <= reach:
                    hits.append(annotation)
                continue
            points = annotation.points
            reach = radius + annotation.width
            if len(points) == 1:
                if math.hypot(u - points[0][0], v - points[0][1]) <= reach:
                    hits.append(annotation)
                continue
            # Deliberately ragged: each point paired with its successor.
            for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
                if self._segment_distance(u, v, x0, y0, x1, y1) <= reach:
                    hits.append(annotation)
                    break

        if not hits:
            return
        self.ctx.db.delete_annotations([a.id for a in hits])
        self._reload_annotations(page)
        self._undo_stack.append(("remove", hits))
        self._sync_annotate_bar()

    # ------------------------------------------------------- annotation mode

    def enter_annotate_mode(self) -> None:
        if self._info() is None or self.crop_mode:
            return
        self.annotate_mode = True
        self.control_bar.set_visible(False)
        self.annotate_bar.set_visible(True)
        self.overlay_revealer.set_reveal_child(True)
        if self._overlay_source:
            GLib.source_remove(self._overlay_source)
            self._overlay_source = 0
        self._sync_annotate_bar()
        self.area.queue_draw()

    def exit_annotate_mode(self) -> None:
        self.annotate_mode = False
        self._stroke = None
        self._eraser_pos = None
        self._span_draft = None
        self._undo_stack.clear()
        self.annotate_bar.set_visible(False)
        self.control_bar.set_visible(True)
        self.show_overlay()
        self.area.queue_draw()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._sync_annotate_bar()

    def set_pen_color(self, color: str) -> None:
        self.pen_color = color

    def set_pen_width(self, width: float) -> None:
        self.pen_width = width

    def annotation_undo(self) -> None:
        if not self._undo_stack:
            return
        action, payload = self._undo_stack.pop()
        pages: set[int] = set()
        if action == "add":
            self.ctx.db.delete_annotations(payload)
            for page, *_rest in self._page_rects:
                pages.add(page)
            pages.add(self.page)
        else:
            for annotation in payload:
                annotation.id = self.ctx.db.add_annotation(annotation)
                pages.add(annotation.page)
        for page in pages | {self.page}:
            self._reload_annotations(page)
        self._annotations.clear()
        self._sync_annotate_bar()
        self.area.queue_draw()

    def annotation_clear_page(self) -> None:
        removed = self.ctx.db.clear_annotations(self.score.id, self.page)
        if removed:
            self._undo_stack.append(("remove", removed))
        self._reload_annotations(self.page)
        self._sync_annotate_bar()
        self.area.queue_draw()

    def _sync_annotate_bar(self) -> None:
        self.annotate_bar.sync(
            tool=self.tool,
            color=self.pen_color,
            width=self.pen_width,
            can_undo=bool(self._undo_stack),
        )

    # ------------------------------------------------------------ crop mode

    def _to_display(self, rect: CropRect) -> CropRect:
        """Page-space crop -> the orientation actually on screen.

        A crop is stored unrotated, because that is the space Poppler
        renders in, but the editor draws and drags in the rotated view. The
        draft is therefore held in display space for the duration of the
        edit and converted back on save - otherwise dragging the left
        handle on a page rotated 90° would move its top edge.
        """
        rotation = self.rotation
        if rotation == 90:
            return CropRect(1 - rect.y1, rect.x0, 1 - rect.y0, rect.x1)
        if rotation == 180:
            return CropRect(1 - rect.x1, 1 - rect.y1, 1 - rect.x0, 1 - rect.y0)
        if rotation == 270:
            return CropRect(rect.y0, 1 - rect.x1, rect.y1, 1 - rect.x0)
        return rect

    def _from_display(self, rect: CropRect) -> CropRect:
        rotation = self.rotation
        if rotation == 90:
            return CropRect(rect.y0, 1 - rect.x1, rect.y1, 1 - rect.x0)
        if rotation == 180:
            return CropRect(1 - rect.x1, 1 - rect.y1, 1 - rect.x0, 1 - rect.y0)
        if rotation == 270:
            return CropRect(1 - rect.y1, rect.x0, 1 - rect.y0, rect.x1)
        return rect

    def enter_crop_mode(self) -> None:
        if self._info() is None:
            return
        if self.annotate_mode:
            self.exit_annotate_mode()
        self.crop_mode = True
        self.crop_draft = self._to_display(
            self.crop or CropRect(0.0, 0.0, 1.0, 1.0)
        )
        self.control_bar.set_visible(False)
        self.crop_bar.set_visible(True)
        self.overlay_revealer.set_reveal_child(True)
        if self._overlay_source:
            GLib.source_remove(self._overlay_source)
            self._overlay_source = 0
        self.area.queue_draw()

    def exit_crop_mode(self, save: bool) -> None:
        if save and self.crop_draft is not None:
            draft = self._from_display(self.crop_draft.clamped()).clamped()
            if draft.is_sane():
                self.score.crop = draft
                # auto=False: an explicit choice must never be replaced by a
                # later automatic detection.
                self.ctx.db.set_crop(self.score.id, draft, auto=False)
                self.ctx.render.invalidate()
        self.crop_mode = False
        self.crop_draft = None
        self._crop_grab = None
        self.control_bar.set_visible(True)
        self.crop_bar.set_visible(False)
        self.zoom = 1.0
        self.pan_x = 0.0
        self.show_overlay()
        self.area.queue_draw()

    def crop_reset(self) -> None:
        self.crop_draft = CropRect(0.0, 0.0, 1.0, 1.0)
        self.area.queue_draw()

    def crop_autodetect(self) -> None:
        path = self.score.path
        count = self._page_count()

        def work():
            try:
                document = open_document(path)
                detected = detect_crop(document, max(1, count))
            except PdfError as exc:
                log.warning("auto-crop failed: %s", exc)
                detected = None
            GLib.idle_add(self._on_autodetected, detected)

        threading.Thread(target=work, name="leggio-autocrop", daemon=True).start()

    def _on_autodetected(self, detected: Optional[CropRect]) -> bool:
        if detected is None:
            self.ctx.toast("Could not detect margins on this score")
        elif self.crop_mode:
            self.crop_draft = self._to_display(detected)
            self.area.queue_draw()
        return False

    def _draw_crop_mode(self, ctx, dev_w: float, dev_h: float) -> None:
        """The full uncropped page, dimmed outside the draft rectangle."""
        info = self._info()
        page_w, page_h = info.page_sizes[min(self.page, len(info.page_sizes) - 1)]
        if self.rotation in (90, 270):
            page_w, page_h = page_h, page_w

        margin = 60
        scale = min(
            (dev_w - margin * 2) / page_w, (dev_h - margin * 2) / page_h
        )
        width_px, height_px = page_w * scale, page_h * scale
        x = (dev_w - width_px) / 2
        y = (dev_h - height_px) / 2
        self._crop_rect_px = (x, y, width_px, height_px)

        key = make_key(
            self.score.path, self.page, round(width_px), round(height_px),
            self.rotation, None,
        )
        surface = self.ctx.render.request(key, self._on_rendered)
        ctx.save()
        if surface is not None:
            ctx.translate(x, y)
            ctx.set_source_surface(surface, 0, 0)
            ctx.paint()
        else:
            ctx.set_source_rgb(1, 1, 1)
            ctx.rectangle(x, y, width_px, height_px)
            ctx.fill()
        ctx.restore()

        draft = (self.crop_draft or CropRect(0.0, 0.0, 1.0, 1.0)).clamped()
        rx = x + draft.x0 * width_px
        ry = y + draft.y0 * height_px
        rw = draft.width * width_px
        rh = draft.height * height_px

        # Dim everything the crop would discard.
        ctx.set_source_rgba(0, 0, 0, 0.55)
        ctx.rectangle(0, 0, dev_w, dev_h)
        ctx.rectangle(rx, ry, rw, rh)
        ctx.set_fill_rule(cairo.FillRule.EVEN_ODD)
        ctx.fill()
        ctx.set_fill_rule(cairo.FillRule.WINDING)

        ctx.set_source_rgb(0.3, 0.7, 1.0)
        ctx.set_line_width(3)
        ctx.rectangle(rx, ry, rw, rh)
        ctx.stroke()

        size = 26
        for hx, hy in (
            (rx, ry + rh / 2), (rx + rw, ry + rh / 2),
            (rx + rw / 2, ry), (rx + rw / 2, ry + rh),
        ):
            ctx.rectangle(hx - size / 2, hy - size / 2, size, size)
        ctx.fill()

    def _crop_hit_test(self, x: float, y: float) -> Optional[str]:
        if self._crop_rect_px is None or self.crop_draft is None:
            return None
        factor = self.get_scale_factor() or 1
        px, py = x * factor, y * factor
        bx, by, bw, bh = self._crop_rect_px
        draft = self.crop_draft
        left = bx + draft.x0 * bw
        right = bx + draft.x1 * bw
        top = by + draft.y0 * bh
        bottom = by + draft.y1 * bh

        near = CROP_HANDLE_PX
        if abs(px - left) < near:
            return "left"
        if abs(px - right) < near:
            return "right"
        if abs(py - top) < near:
            return "top"
        if abs(py - bottom) < near:
            return "bottom"
        return None

    def _crop_drag(self, gesture, offset_x: float, offset_y: float) -> None:
        if self._crop_grab is None or self._crop_rect_px is None:
            return
        ok, start_x, start_y = gesture.get_start_point()
        if not ok:
            return
        factor = self.get_scale_factor() or 1
        bx, by, bw, bh = self._crop_rect_px
        px = (start_x + offset_x) * factor
        py = (start_y + offset_y) * factor
        fx = min(max((px - bx) / bw, 0.0), 1.0)
        fy = min(max((py - by) / bh, 0.0), 1.0)

        draft = self.crop_draft or CropRect(0.0, 0.0, 1.0, 1.0)
        minimum = 0.05
        if self._crop_grab == "left":
            draft = CropRect(min(fx, draft.x1 - minimum), draft.y0, draft.x1, draft.y1)
        elif self._crop_grab == "right":
            draft = CropRect(draft.x0, draft.y0, max(fx, draft.x0 + minimum), draft.y1)
        elif self._crop_grab == "top":
            draft = CropRect(draft.x0, min(fy, draft.y1 - minimum), draft.x1, draft.y1)
        elif self._crop_grab == "bottom":
            draft = CropRect(draft.x0, draft.y0, draft.x1, max(fy, draft.y0 + minimum))
        self.crop_draft = draft.clamped()
        self.area.queue_draw()
