# SPDX-License-Identifier: GPL-3.0-or-later
"""Page space <-> screen transforms.

Page space is fractions of the uncropped, unrotated page; crops and
annotations are stored in it. Display space is the same fractions after
the score's rotation. A page rectangle (x, y, w, h) is where the cropped,
rotated page is drawn on screen.
"""

from __future__ import annotations

from typing import Optional

from .models import FULL_PAGE, CropRect

PageRect = tuple[float, float, float, float]


def rotate_point(u: float, v: float, rotation: int) -> tuple[float, float]:
    """Page space -> display space."""
    if rotation == 90:
        return (1 - v, u)
    if rotation == 180:
        return (1 - u, 1 - v)
    if rotation == 270:
        return (v, 1 - u)
    return (u, v)


def unrotate_point(a: float, b: float, rotation: int) -> tuple[float, float]:
    """Display space -> page space."""
    if rotation == 90:
        return (b, 1 - a)
    if rotation == 180:
        return (1 - a, 1 - b)
    if rotation == 270:
        return (1 - b, a)
    return (a, b)


def rotate_rect(rect: CropRect, rotation: int) -> CropRect:
    """Page-space rectangle -> display space."""
    if rotation == 90:
        return CropRect(1 - rect.y1, rect.x0, 1 - rect.y0, rect.x1)
    if rotation == 180:
        return CropRect(1 - rect.x1, 1 - rect.y1, 1 - rect.x0, 1 - rect.y0)
    if rotation == 270:
        return CropRect(rect.y0, 1 - rect.x1, rect.y1, 1 - rect.x0)
    return rect


def unrotate_rect(rect: CropRect, rotation: int) -> CropRect:
    """Display-space rectangle -> page space."""
    if rotation == 90:
        return CropRect(rect.y0, 1 - rect.x1, rect.y1, 1 - rect.x0)
    if rotation == 180:
        return CropRect(1 - rect.x1, 1 - rect.y1, 1 - rect.x0, 1 - rect.y0)
    if rotation == 270:
        return CropRect(1 - rect.y1, rect.x0, 1 - rect.y0, rect.x1)
    return rect


def page_to_screen(u: float, v: float, rect: PageRect, rotation: int,
                   crop: Optional[CropRect]) -> tuple[float, float]:
    x, y, w, h = rect
    a, b = rotate_point(u, v, rotation)
    box = rotate_rect(crop or FULL_PAGE, rotation)
    return (
        x + (a - box.x0) / max(box.width, 1e-9) * w,
        y + (b - box.y0) / max(box.height, 1e-9) * h,
    )


def screen_to_page(sx: float, sy: float, rect: PageRect, rotation: int,
                   crop: Optional[CropRect]) -> tuple[float, float]:
    x, y, w, h = rect
    box = rotate_rect(crop or FULL_PAGE, rotation)
    a = box.x0 + (sx - x) / max(w, 1e-9) * box.width
    b = box.y0 + (sy - y) / max(h, 1e-9) * box.height
    return unrotate_point(a, b, rotation)


def page_unit(width: float, rotation: int, crop: Optional[CropRect]) -> float:
    """Screen pixels per unit of page width, the scale annotation sizes use."""
    box = rotate_rect(crop or FULL_PAGE, rotation)
    return width / max(box.width, 1e-9)
