# SPDX-License-Identifier: GPL-3.0-or-later
"""Annotation mode for the viewer: ink, highlighter, symbols, eraser."""

from __future__ import annotations

import math
import time
from typing import Optional

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .. import geometry
from ..models import Annotation
from .controls import HIGHLIGHT_COLOR, PEN_COLORS, PEN_WIDTHS
from .symbols import draw_span_symbol, draw_symbol, is_span, needs_text

ERASER_RADIUS = 22        # eraser reach in device px
# Stamped symbols are sized from the pen width. 14x put a fermata at twice
# the height of the staff it sat above; 5x matches engraved articulation.
SYMBOL_SCALE = 5.0


class AnnotationMixin:
    """Annotation state and behaviour, mixed into ViewerPage."""

    def _init_annotation(self) -> None:
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

    def _annotate_key(self, keyval: int) -> bool:
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

    def _page_to_screen(self, u, v, x, y, w, h) -> tuple[float, float]:
        return geometry.page_to_screen(u, v, (x, y, w, h), self.rotation, self.crop)

    def _screen_to_page(self, sx, sy, x, y, w, h) -> tuple[float, float]:
        return geometry.screen_to_page(sx, sy, (x, y, w, h), self.rotation, self.crop)

    def _page_unit(self, w: float) -> float:
        return geometry.page_unit(w, self.rotation, self.crop)

    def _current_ink(self) -> tuple[str, float]:
        if self.tool == "highlighter":
            return HIGHLIGHT_COLOR, max(self.pen_width * 4.0, 0.012)
        return self.pen_color, self.pen_width

    def _rgba(self, color: str) -> tuple[float, float, float, float]:
        try:
            r = int(color[1:3], 16) / 255
            g = int(color[3:5], 16) / 255
            b = int(color[5:7], 16) / 255
            a = int(color[7:9], 16) / 255 if len(color) >= 9 else 1.0
        except (ValueError, IndexError):
            return (0.85, 0.15, 0.15, 1.0)
        # Night mode: dark ink would vanish on the inverted page, so it
        # inverts with it; coloured ink keeps its colour.
        if self.night and 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.25:
            r, g, b = 1 - r, 1 - g, 1 - b
        return (r, g, b, a)

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
