# SPDX-License-Identifier: GPL-3.0-or-later
"""Whitespace margin detection.

Sheet music PDFs are typically 60-70% margin by area. Trimming that is the
single largest readability win on a fixed-size screen, so it runs
automatically at scan time.

Method: render a few sample pages small (~200px wide) onto opaque white,
then find the bounding box of everything that is not white. The pixel scan
is done with memoryview.cast('I') so the per-row minimum is one C-level
min() call rather than a Python loop over pixels.
"""

from __future__ import annotations

import logging
from typing import Optional

import cairo

from .models import CropRect

log = logging.getLogger(__name__)

# A pixel counts as ink below this packed ARGB value. The slack absorbs
# antialiasing and the faint off-white background some scanners produce.
INK_THRESHOLD = 0xFFE0E0E0

SAMPLE_WIDTH = 200
# Fraction of the page added back around the detected box, so stems and
# slurs that graze the bounding box are not shaved off.
PADDING = 0.015
# Below this fraction of the page, assume detection failed (blank page,
# scanned grey background) rather than emit an unreadable crop.
MIN_AREA = 0.25


def _page_ink_bbox(page) -> Optional[tuple[float, float, float, float]]:
    """Bounding box of non-white content on one page, as 0..1 fractions."""
    page_w, page_h = page.get_size()
    if page_w <= 0 or page_h <= 0:
        return None

    scale = SAMPLE_WIDTH / page_w
    width = max(1, int(page_w * scale))
    height = max(1, int(page_h * scale))

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    # Opaque white: with ARGB32 a blank pixel is then exactly 0xFFFFFFFF,
    # which makes the threshold comparison meaningful. (FORMAT_RGB24 leaves
    # the top byte undefined, so it cannot be compared this way.)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
    page.render(ctx)
    surface.flush()

    data = surface.get_data()
    stride = surface.get_stride()
    view = memoryview(data)

    rows = []
    for y in range(height):
        start = y * stride
        rows.append(view[start:start + width * 4].cast("I"))

    top = bottom = None
    for y, row in enumerate(rows):
        if min(row) < INK_THRESHOLD:
            if top is None:
                top = y
            bottom = y
    if top is None:
        return None  # blank page

    # Column extents: stride by 2 rows, which halves the work and cannot
    # miss a stave (staves are many pixels tall even at this scale).
    col_min = [0xFFFFFFFF] * width
    for y in range(top, bottom + 1, 2):
        row = rows[y]
        for x in range(width):
            value = row[x]
            if value < col_min[x]:
                col_min[x] = value

    left = right = None
    for x, value in enumerate(col_min):
        if value < INK_THRESHOLD:
            if left is None:
                left = x
            right = x
    if left is None:
        return None

    return (
        left / width,
        top / height,
        (right + 1) / width,
        (bottom + 1) / height,
    )


def sample_pages(page_count: int, limit: int = 5) -> list[int]:
    """First, last and a few evenly spaced pages in between."""
    if page_count <= limit:
        return list(range(page_count))
    step = (page_count - 1) / (limit - 1)
    return sorted({int(round(i * step)) for i in range(limit)})


def detect_crop(document, page_count: int, limit: int = 5) -> Optional[CropRect]:
    """Union of the ink bounding boxes of a sample of pages.

    Union, not intersection or average: a title page with a wide header and
    a final page holding one short system must both stay fully visible. The
    union is the only direction that cannot cut notation off.
    """
    boxes: list[CropRect] = []
    for index in sample_pages(page_count, limit):
        try:
            page = document.get_page(index)
            if page is None:
                continue
            box = _page_ink_bbox(page)
        except Exception as exc:  # a damaged page must not abort the scan
            log.debug("crop detection failed on page %d: %s", index, exc)
            continue
        if box:
            boxes.append(CropRect(*box))

    if not boxes:
        return None

    result = boxes[0]
    for box in boxes[1:]:
        result = result.union(box)

    result = CropRect(
        result.x0 - PADDING,
        result.y0 - PADDING,
        result.x1 + PADDING,
        result.y1 + PADDING,
    ).clamped()

    if result.area < MIN_AREA:
        log.debug("discarding implausible crop %r (area %.2f)", result, result.area)
        return None
    # Nothing meaningful to trim.
    if result.area > 0.985:
        return None
    return result
