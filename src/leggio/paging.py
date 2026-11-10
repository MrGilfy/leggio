# SPDX-License-Identifier: GPL-3.0-or-later
"""Page turns through one score or a setlist.

A screen shows `step` pages at a time: one, or two in a spread. Every
score starts a screen of its own, so the last screen of a score with an
odd page count holds a single page, and the next score opens on its
first page rather than being paired with it.
"""

from __future__ import annotations

from typing import Sequence


def last_screen(count: int, step: int) -> int:
    """The first page of a score's last screen."""
    last = max(count - 1, 0)
    return last - last % max(step, 1)


def turn(index: int, page: int, delta: int, step: int,
         counts: Sequence[int]) -> tuple[int, int]:
    """Where one turn forward (delta 1) or back (-1) lands.

    Takes and returns (score index, page), the page being the first one
    on screen; `counts` is the page count of each score. At either end of
    the setlist the position is returned unchanged.
    """
    step = max(step, 1)
    count = counts[index]
    if delta > 0:
        if page + step < count:
            return (index, page + step)
        if index + 1 < len(counts):
            return (index + 1, 0)
        return (index, page)
    if page - step >= 0:
        return (index, page - step)
    if page > 0:
        # A spread that began on an odd page: the first page is still
        # ahead of the previous score.
        return (index, 0)
    if index > 0:
        return (index - 1, last_screen(counts[index - 1], step))
    return (index, page)
