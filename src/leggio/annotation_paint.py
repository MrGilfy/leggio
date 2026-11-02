# SPDX-License-Identifier: GPL-3.0-or-later
"""Drawing annotations with cairo, shared by the viewer and PDF export.

Annotations live in page space; `to_screen(u, v)` maps a page-space point to
the target surface, which is the screen in the viewer and an output page in
an export. `unit` is target units per unit of page width, the scale stroke
widths and symbol sizes are stored in. No GTK, so export and tests can use
it on any thread.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

import cairo

from .models import Annotation
from .ui.symbols import draw_span_symbol, draw_symbol, is_span

ToScreen = Callable[[float, float], tuple[float, float]]
Rgba = Callable[[str], tuple[float, float, float, float]]

FALLBACK_COLOR = (0.85, 0.15, 0.15, 1.0)


def parse_color(color: str) -> tuple[float, float, float, float]:
    """#rrggbb or #rrggbbaa to cairo's 0..1 channels."""
    try:
        r = int(color[1:3], 16) / 255
        g = int(color[3:5], 16) / 255
        b = int(color[5:7], 16) / 255
        a = int(color[7:9], 16) / 255 if len(color) >= 9 else 1.0
    except (ValueError, IndexError, TypeError):
        return FALLBACK_COLOR
    return (r, g, b, a)


def prepare(ctx: cairo.Context) -> None:
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)


def paint_annotation(ctx: cairo.Context, annotation: Annotation,
                     to_screen: ToScreen, unit: float, rotation: int,
                     rgba: Rgba = parse_color) -> None:
    """Draw one stroke or symbol. Links are not painted here: the viewer
    draws them as markers, and an export leaves them out."""
    if annotation.kind == Annotation.LINK:
        return
    if annotation.kind == Annotation.SYMBOL:
        data = annotation.data
        ctx.set_source_rgba(*rgba(annotation.color))
        paint_symbol(
            ctx, data.get("glyph", "fermata"),
            data.get("x", 0.5), data.get("y", 0.5),
            max(data.get("size", 0.05) * unit, 4.0),
            to_screen, rotation,
            span=data.get("span"), label=data.get("label"),
        )
    else:
        paint_ink(ctx, annotation.points, annotation.color, annotation.width,
                  to_screen, unit, rgba)


def paint_symbol(ctx: cairo.Context, glyph: str, u: float, v: float,
                 size: float, to_screen: ToScreen, rotation: int,
                 span: Optional[float] = None,
                 label: Optional[str] = None) -> None:
    """Draw a symbol in page space, rotated with the page, in the current
    source colour.

    Span symbols run along the vector between their page-space ends, so they
    follow the rotation too.
    """
    sx, sy = to_screen(u, v)
    if span is not None and is_span(glyph):
        ex, ey = to_screen(u + span, v)
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
    ctx.rotate(math.radians(rotation))
    draw_symbol(ctx, glyph, 0, 0, size, label=label)
    ctx.restore()


def paint_ink(ctx: cairo.Context, points: Sequence[tuple[float, float]],
              color: str, width: float, to_screen: ToScreen, unit: float,
              rgba: Rgba = parse_color) -> None:
    if not points:
        return
    screen = [to_screen(u, v) for u, v in points]
    ctx.set_source_rgba(*rgba(color))
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
        # Quadratic through the midpoints: cheap smoothing that takes the
        # jitter out of a finger-drawn stroke.
        for i in range(1, len(screen) - 1):
            mx = (screen[i][0] + screen[i + 1][0]) / 2
            my = (screen[i][1] + screen[i + 1][1]) / 2
            _quad_to(ctx, screen[i][0], screen[i][1], mx, my)
        ctx.line_to(*screen[-1])
    ctx.stroke()


def _quad_to(ctx: cairo.Context, qx: float, qy: float, ex: float, ey: float) -> None:
    x0, y0 = ctx.get_current_point()
    ctx.curve_to(
        x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
        ex + 2 / 3 * (qx - ex), ey + 2 / 3 * (qy - ey),
        ex, ey,
    )
