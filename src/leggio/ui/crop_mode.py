# SPDX-License-Identifier: GPL-3.0-or-later
"""Crop mode for the viewer: drag the margins, or detect them."""

from __future__ import annotations

import logging
import threading
from typing import Optional

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib  # noqa: E402

from ..crop import detect_crop
from ..geometry import rotate_rect, unrotate_rect
from ..models import CropRect
from ..pdf import PdfError, open_document
from ..render import make_key

log = logging.getLogger(__name__)

CROP_HANDLE_PX = 44       # touch-sized grab area for the crop handles


class CropMixin:
    """Crop editing state and behaviour, mixed into ViewerPage."""

    def _init_crop(self) -> None:
        self.crop_mode = False
        self.crop_draft: Optional[CropRect] = None
        self._crop_rect_px: Optional[tuple[float, float, float, float]] = None
        self._crop_grab: Optional[str] = None

    def _crop_key(self, keyval: int) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.exit_crop_mode(save=False)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.exit_crop_mode(save=True)
            return True
        return False

    # ------------------------------------------------------------ crop mode

    # The draft is edited in display space and converted back on save, so
    # dragging the left handle of a rotated page moves its on-screen left.
    def _to_display(self, rect: CropRect) -> CropRect:
        return rotate_rect(rect, self.rotation)

    def _from_display(self, rect: CropRect) -> CropRect:
        return unrotate_rect(rect, self.rotation)

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
