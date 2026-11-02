# SPDX-License-Identifier: GPL-3.0-or-later
"""Jump links: D.S., D.C., To Coda and first/second endings, one tap away.

A link is an annotation row of kind "link". Its page is the source page and
its data holds the source spot and the target, all in page space (fractions
of the uncropped, unrotated page), so links survive crop and rotation and
the eraser, undo, clearing and score deletion all work as for ink:

    {"x": .., "y": .., "target_page": n, "tx": .., "ty": ..}

This module is the arithmetic behind them and has no GTK dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from . import geometry
from .models import Annotation, CropRect

LINK_COLOR = "#3584e4"

# Fraction of the screen height left above a target that lands at the top,
# so the bar before it is still in view.
TOP_MARGIN = 0.04


@dataclass(frozen=True)
class Link:
    id: int
    page: int
    x: float
    y: float
    target_page: int
    tx: float
    ty: float

    @property
    def forward(self) -> bool:
        """True for a jump further into the score (To Coda), False for one
        back (D.S., D.C., a repeat)."""
        return (self.target_page, self.ty) > (self.page, self.y)

    @classmethod
    def from_annotation(cls, annotation: Annotation) -> Optional["Link"]:
        if annotation.kind != Annotation.LINK:
            return None
        data = annotation.data
        try:
            return cls(
                id=annotation.id,
                page=annotation.page,
                x=float(data["x"]),
                y=float(data["y"]),
                target_page=int(data["target_page"]),
                tx=float(data["tx"]),
                ty=float(data["ty"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def make_link(score_id: int, page: int, x: float, y: float,
              target_page: int, tx: float, ty: float,
              created: float = 0.0) -> Annotation:
    """The annotation row for a new link."""
    return Annotation(
        id=0, score_id=score_id, page=page, kind=Annotation.LINK,
        color=LINK_COLOR, width=0.0,
        data={
            "x": round(_clamp(x), 5),
            "y": round(_clamp(y), 5),
            "target_page": int(target_page),
            "tx": round(_clamp(tx), 5),
            "ty": round(_clamp(ty), 5),
        },
        created=created,
    )


# ----------------------------------------------------------------- landing

def display_fraction(u: float, v: float, rotation: int,
                     crop: Optional[CropRect]) -> float:
    """How far down the page as drawn (cropped and rotated) a page-space
    point is, from 0 at the top edge to 1 at the bottom."""
    _a, b = geometry.rotate_point(u, v, rotation)
    box = geometry.rotate_rect(crop or geometry.FULL_PAGE, rotation)
    return _clamp((b - box.y0) / max(box.height, 1e-9))


def landing(link: Link, view: str, *, rotation: int,
            crop: Optional[CropRect], current_page: int = 0,
            margin: float = 0.0) -> tuple[int, float]:
    """Where to put the viewer, as (page, offset), to show a link's target.

    view is "top" for half-page and fit-to-width views, where the target is
    scrolled to the top of the screen with `margin` (a fraction of the page
    height) above it; "page" for the whole-page view, which shows the target
    page whole; and "spread", which shows the spread holding the target
    while keeping the current pairing of left and right pages.
    """
    if view == "top":
        offset = display_fraction(link.tx, link.ty, rotation, crop) - margin
        return (link.target_page, min(max(offset, 0.0), 0.999))
    if view == "spread":
        parity = (link.target_page - current_page) % 2
        return (max(link.target_page - parity, 0), 0.0)
    return (link.target_page, 0.0)


# --------------------------------------------------------- picking on screen

# A marker as drawn: the link, its centre in device pixels, and the index
# of its page in the draw order (reading order: top to bottom, left page
# before right page).
Spot = tuple[Link, float, float, int]


def marker_at(spots: Sequence[Spot], x: float, y: float,
              radius: float) -> Optional[Link]:
    """The link whose marker contains the point, nearest first."""
    best: Optional[Link] = None
    best_distance = radius
    for link, sx, sy, _order in spots:
        distance = math.hypot(x - sx, y - sy)
        if distance <= best_distance:
            best, best_distance = link, distance
    return best


def first_in_reading_order(spots: Sequence[Spot],
                           viewport_height: float) -> Optional[Link]:
    """The first link on screen in reading order, for the J key."""
    visible = [s for s in spots if 0.0 <= s[2] <= viewport_height]
    if not visible:
        return None
    return min(visible, key=lambda s: (s[3], s[2], s[1]))[0]


# -------------------------------------------------------------------- back

Position = tuple[int, int, float]   # (score index, page, offset)


class BackState:
    """Where the Back control returns to.

    Set by a jump and cleared once used, by another jump replacing it, or
    after TURNS_TO_EXPIRE page turns: past that the jump was a To Coda you
    are not coming back from, and the button would only be in the way.
    """

    TURNS_TO_EXPIRE = 2

    def __init__(self) -> None:
        self.position: Optional[Position] = None
        self._turns = 0

    @property
    def available(self) -> bool:
        return self.position is not None

    def jumped(self, position: Position) -> None:
        self.position = position
        self._turns = 0

    def turned(self) -> None:
        if self.position is None:
            return
        self._turns += 1
        if self._turns >= self.TURNS_TO_EXPIRE:
            self.clear()

    def take(self) -> Optional[Position]:
        position = self.position
        self.clear()
        return position

    def clear(self) -> None:
        self.position = None
        self._turns = 0
