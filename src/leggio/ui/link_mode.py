# SPDX-License-Identifier: GPL-3.0-or-later
"""Jump links in the viewer: placing them, drawing them, following them.

Placing happens in annotation mode with the Link tool: tap the source spot,
go to the target (page buttons, arrows or pedal), tap the target spot.
While playing, each link is a small marker; tapping it, or pressing J for
the first one on screen in reading order, jumps to the target. A Back
button then returns to where the jump started, until it is used or two
page turns have passed. The arithmetic lives in leggio.links.
"""

from __future__ import annotations

import math
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import links
from ..models import Annotation
from .symbols import draw_jump_arrow

MARKER_RADIUS = 13       # logical px: small enough to stay out of the music
HIT_RADIUS = 22          # logical px: a 44 px target for a finger
FLASH_SECONDS = 1.5
MIN_LINK_DISTANCE = 0.02  # page units; a tap on the source itself is not a target


class LinkModeMixin:
    """Jump-link state and behaviour, mixed into ViewerPage."""

    def _init_link_mode(self) -> None:
        # (score id, page, u, v) of a source tapped but not yet linked.
        self._link_source: Optional[tuple[int, int, float, float]] = None
        self._link_spots: list[links.Spot] = []
        self._score_link_rows: dict[int, list[Annotation]] = {}
        # (score id, page, tx, ty, start) of the target ring after a jump.
        self._flash: Optional[tuple[int, int, float, float, float]] = None
        self._flash_source = 0
        self.back_state = links.BackState()

    def _build_link_widgets(self) -> list[Gtk.Widget]:
        """The placement prompt and the Back button, as overlay children."""
        self.link_prompt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.link_prompt.add_css_class("viewer-overlay")
        label = Gtk.Label(label="Go to the jump target and tap it")
        label.add_css_class("viewer-title")
        self.link_prompt.append(label)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("viewer-button")
        cancel.connect("clicked", lambda _b: self.cancel_link())
        self.link_prompt.append(cancel)
        self.link_prompt.set_halign(Gtk.Align.CENTER)
        self.link_prompt.set_valign(Gtk.Align.END)
        self.link_prompt.set_margin_bottom(24)
        self.link_prompt.set_visible(False)

        self.back_button = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.append(Gtk.Image.new_from_icon_name("leggio-back-symbolic"))
        content.append(Gtk.Label(label="Back"))
        self.back_button.set_child(content)
        self.back_button.set_tooltip_text("Back to where the jump started (B)")
        self.back_button.add_css_class("viewer-overlay")
        self.back_button.add_css_class("jump-back")
        self.back_button.set_halign(Gtk.Align.END)
        self.back_button.set_valign(Gtk.Align.END)
        self.back_button.set_margin_end(24)
        self.back_button.set_margin_bottom(24)
        self.back_button.set_visible(False)
        self.back_button.connect("clicked", lambda _b: self.jump_back())
        return [self.link_prompt, self.back_button]

    # ------------------------------------------------------------ the model

    def _links_on(self, page: int) -> list[links.Link]:
        found = []
        for annotation in self._annotations_for(page):
            link = links.Link.from_annotation(annotation)
            if link is not None:
                found.append(link)
        return found

    def _score_links(self) -> list[Annotation]:
        score_id = self.score.id
        rows = self._score_link_rows.get(score_id)
        if rows is None:
            rows = self.ctx.db.links_for(score_id)
            self._score_link_rows[score_id] = rows
        return rows

    def _forget_score_links(self) -> None:
        self._score_link_rows.clear()

    def _links_targeting(self, page: int) -> list[tuple[Annotation, links.Link]]:
        out = []
        for row in self._score_links():
            link = links.Link.from_annotation(row)
            if link is not None and link.target_page == page:
                out.append((row, link))
        return out

    # -------------------------------------------------------------- drawing

    def _draw_links(self, ctx, page: int, x: float, y: float,
                    w: float, h: float, order: int) -> None:
        factor = self.get_scale_factor() or 1
        marker = MARKER_RADIUS * factor
        score_id = self.score.id

        ctx.save()
        ctx.rectangle(x, y, w, h)
        ctx.clip()

        for link in self._links_on(page):
            sx, sy = self._page_to_screen(link.x, link.y, x, y, w, h)
            self._draw_marker(ctx, sx, sy, marker, link.forward)
            if x <= sx <= x + w and y <= sy <= y + h:
                self._link_spots.append((link, sx, sy, order))

        if self.annotate_mode:
            for _row, link in self._links_targeting(page):
                tx, ty = self._page_to_screen(link.tx, link.ty, x, y, w, h)
                self._draw_target_ring(ctx, tx, ty, marker, factor)
            source = self._link_source
            if source is not None and source[0] == score_id and source[1] == page:
                sx, sy = self._page_to_screen(source[2], source[3], x, y, w, h)
                self._draw_marker(ctx, sx, sy, marker, True, pending=True)

        flash = self._flash
        if flash is not None and flash[0] == score_id and flash[1] == page:
            progress = (time.monotonic() - flash[4]) / FLASH_SECONDS
            if 0.0 <= progress < 1.0:
                fx, fy = self._page_to_screen(flash[2], flash[3], x, y, w, h)
                ctx.set_source_rgba(0.21, 0.52, 0.89, 0.9 * (1.0 - progress))
                ctx.set_line_width(3 * factor)
                ctx.arc(fx, fy, marker + 26 * factor * progress, 0, 2 * math.pi)
                ctx.stroke()

        ctx.restore()

    @staticmethod
    def _draw_marker(ctx, sx: float, sy: float, radius: float,
                     forward: bool, pending: bool = False) -> None:
        ctx.save()
        ctx.set_source_rgba(0.21, 0.52, 0.89, 0.45 if pending else 0.72)
        ctx.arc(sx, sy, radius, 0, 2 * math.pi)
        ctx.fill()
        if pending:
            ctx.set_source_rgba(0.21, 0.52, 0.89, 1.0)
            ctx.set_line_width(max(radius * 0.15, 1.5))
            ctx.set_dash([radius * 0.4, radius * 0.3])
            ctx.arc(sx, sy, radius * 1.35, 0, 2 * math.pi)
            ctx.stroke()
            ctx.set_dash([])
        ctx.set_source_rgba(1, 1, 1, 0.95)
        draw_jump_arrow(ctx, sx, sy, radius * 1.5, forward)
        ctx.restore()

    @staticmethod
    def _draw_target_ring(ctx, tx: float, ty: float, radius: float,
                          factor: float) -> None:
        ctx.save()
        ctx.set_source_rgba(0.21, 0.52, 0.89, 0.85)
        ctx.set_line_width(2 * factor)
        ctx.set_dash([4 * factor, 3 * factor])
        ctx.arc(tx, ty, radius, 0, 2 * math.pi)
        ctx.stroke()
        ctx.set_dash([])
        ctx.arc(tx, ty, 2.5 * factor, 0, 2 * math.pi)
        ctx.fill()
        ctx.restore()

    def _tick_flash(self) -> bool:
        flash = self._flash
        self.area.queue_draw()
        if flash is None or time.monotonic() - flash[4] >= FLASH_SECONDS:
            self._flash = None
            self._flash_source = 0
            return False
        return True

    def _stop_flash(self) -> None:
        if self._flash_source:
            GLib.source_remove(self._flash_source)
            self._flash_source = 0
        self._flash = None

    # ------------------------------------------------------------- placing

    def _link_press(self, page: int, u: float, v: float) -> None:
        source = self._link_source
        if source is None or source[0] != self.score.id:
            # Starting, or the source belonged to another score in a setlist.
            self._link_source = (self.score.id, page, u, v)
            self.link_prompt.set_visible(True)
            return
        _score_id, source_page, su, sv = source
        if source_page == page and math.hypot(u - su, v - sv) < MIN_LINK_DISTANCE:
            return
        annotation = links.make_link(
            self.score.id, source_page, su, sv, page, u, v, created=time.time()
        )
        annotation.id = self.ctx.db.add_annotation(annotation)
        self._reload_annotations(source_page)
        self._undo_stack.append(("add", [annotation.id]))
        self.cancel_link()
        self._sync_annotate_bar()

    def cancel_link(self) -> None:
        self._link_source = None
        if hasattr(self, "link_prompt"):
            self.link_prompt.set_visible(False)
        self.area.queue_draw()

    @property
    def placing_link(self) -> bool:
        return self._link_source is not None

    # ----------------------------------------------------------- following

    def link_tap(self, x: float, y: float) -> bool:
        """Follow the link under a tap at logical coordinates, if any."""
        factor = self.get_scale_factor() or 1
        link = links.marker_at(
            self._link_spots, x * factor, y * factor, HIT_RADIUS * factor
        )
        if link is None:
            return False
        self.follow_link(link)
        return True

    def follow_first_link(self) -> bool:
        _width, height = self._device_size()
        link = links.first_in_reading_order(self._link_spots, height)
        if link is None:
            return False
        self.follow_link(link)
        return True

    def _landing_view(self) -> str:
        width, height = self._device_size()
        if self._spread_active(width, height):
            return "spread"
        if self._half_active(width, height) or self.fit == "width" or self.zoom > 1.01:
            return "top"
        return "page"

    def follow_link(self, link: links.Link) -> None:
        width, height = self._device_size()
        view = self._landing_view()
        scale = self._scale(width, height)
        _cw, content_h = self._content_size(link.target_page)
        page_h = content_h * scale
        margin = links.TOP_MARGIN * height / page_h if page_h > 0 else 0.0

        before = (self.index, self.page, self.offset)
        page, offset = links.landing(
            link, view, rotation=self.rotation, crop=self.crop,
            current_page=self.page, margin=margin,
        )
        count = self._page_count()
        if count:
            page = min(page, count - 1)
        if page == count - 1 and page_h > height:
            # Never scroll the last page's bottom edge up off the screen.
            offset = min(offset, 1.0 - height / page_h)

        self.page, self.offset = page, max(offset, 0.0)
        self.pan_x = 0.0
        self.back_state.jumped(before)

        self._stop_flash()
        self._flash = (self.score.id, link.target_page, link.tx, link.ty,
                       time.monotonic())
        self._flash_source = GLib.timeout_add(33, self._tick_flash)
        self._after_jump()

    def jump_back(self) -> None:
        position = self.back_state.take()
        if position is None:
            self._sync_back()
            return
        index, page, offset = position
        if index != self.index and 0 <= index < len(self.scores):
            self._switch_score(index)
        self.page, self.offset = page, offset
        self.pan_x = 0.0
        self._stop_flash()
        self._after_jump()

    def _after_jump(self) -> None:
        # The control bar would cover a target that lands at the top.
        self._hide_overlay()
        self.area.queue_draw()
        self._sync_controls()
        self._schedule_save()

    def _sync_back(self) -> None:
        if not hasattr(self, "back_button"):
            return
        self.back_button.set_visible(
            self.back_state.available
            and not self.annotate_mode
            and not self.crop_mode
        )

