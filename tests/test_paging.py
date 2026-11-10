# SPDX-License-Identifier: GPL-3.0-or-later
"""Page turns through a setlist, one page or a two-page spread at a time."""

from __future__ import annotations

import pytest

from leggio.geometry import content_size
from leggio.models import CropRect
from leggio.paging import last_screen, spread_suits, turn

# The scores from issue #18, by page count.
GYMNOPEDIE, CHOPIN, FUR_ELISE, CLAIR_DE_LUNE = 3, 4, 3, 6

SETLISTS = [
    [CHOPIN, CLAIR_DE_LUNE, FUR_ELISE, GYMNOPEDIE],
    [CHOPIN, CLAIR_DE_LUNE, FUR_ELISE],
    [CHOPIN, GYMNOPEDIE, FUR_ELISE],
    [CHOPIN, GYMNOPEDIE, FUR_ELISE, CLAIR_DE_LUNE],
    [CHOPIN, CLAIR_DE_LUNE, GYMNOPEDIE, FUR_ELISE],
    [GYMNOPEDIE, FUR_ELISE],
    [GYMNOPEDIE, CHOPIN, FUR_ELISE, CLAIR_DE_LUNE],
    [2, 3, 2],          # a two-page score could not be reached at all
    [1, 1, 5],
]


def on_screen(index, page, step, counts):
    """The (score, page) pairs one screen shows; never two scores at once."""
    return [(index, p) for p in range(page, min(page + step, counts[index]))]


def walk(counts, step, delta, start):
    """Every screen from `start` to the end of the setlist."""
    position = start
    screens = [on_screen(*position, step, counts)]
    while True:
        moved = turn(*position, delta, step, counts)
        if moved == position:
            return screens
        position = moved
        screens.append(on_screen(*position, step, counts))


def every_page(counts):
    return [(i, p) for i, count in enumerate(counts) for p in range(count)]


@pytest.mark.parametrize("counts", SETLISTS)
@pytest.mark.parametrize("step", [1, 2])
def test_forward_shows_every_page_once(counts, step):
    screens = walk(counts, step, 1, (0, 0))
    assert [page for screen in screens for page in screen] == every_page(counts)


@pytest.mark.parametrize("counts", SETLISTS)
@pytest.mark.parametrize("step", [1, 2])
def test_backward_shows_the_same_screens_in_reverse(counts, step):
    forward = walk(counts, step, 1, (0, 0))
    end = (len(counts) - 1, last_screen(counts[-1], step))
    assert walk(counts, step, -1, end) == forward[::-1]


@pytest.mark.parametrize("counts", SETLISTS)
def test_every_score_opens_on_its_first_page_in_a_spread(counts):
    screens = walk(counts, 2, 1, (0, 0))
    for index in range(len(counts)):
        first = next(s for s in screens if s[0][0] == index)
        assert first[0] == (index, 0)


def test_issue_18_gymnopedie_is_not_skipped_after_fur_elise():
    counts = [CHOPIN, CLAIR_DE_LUNE, FUR_ELISE, GYMNOPEDIE]
    assert turn(2, 2, 1, 2, counts) == (3, 0)     # was (3, 1): pages 2 and 3
    assert turn(3, 0, 1, 2, counts) == (3, 2)
    assert turn(3, 0, -1, 2, counts) == (2, 2)    # back to Für Elise's last page


def test_the_ends_of_the_setlist_stay_put():
    counts = [CHOPIN, FUR_ELISE]
    assert turn(0, 0, -1, 2, counts) == (0, 0)
    assert turn(1, 2, 1, 2, counts) == (1, 2)
    # A full last spread is not shifted to show its right page alone.
    assert turn(0, 2, 1, 2, [CHOPIN]) == (0, 2)
    assert turn(0, 3, 1, 1, [CHOPIN]) == (0, 3)


def test_a_spread_that_starts_on_an_odd_page_keeps_its_pairing():
    # Reached by switching to a spread on page 2, or by a jump link.
    counts = [CHOPIN, CLAIR_DE_LUNE]
    assert turn(1, 1, 1, 2, counts) == (1, 3)
    assert turn(1, 3, 1, 2, counts) == (1, 5)
    assert turn(1, 3, -1, 2, counts) == (1, 1)
    # Back from pages 2-3 shows page 1, not the previous score.
    assert turn(1, 1, -1, 2, counts) == (1, 0)
    assert turn(1, 0, -1, 2, counts) == (0, 2)
    # Forward from an odd pairing still opens the next score on page 1.
    assert turn(0, 3, 1, 2, counts) == (1, 0)


@pytest.mark.parametrize("count, step, first", [
    (1, 2, 0), (2, 2, 0), (3, 2, 2), (4, 2, 2), (6, 2, 4),
    (3, 1, 2), (0, 2, 0),
])
def test_last_screen(count, step, first):
    assert last_screen(count, step) == first


# ------------------------------------------------- one page or a spread

A4 = (595.0, 842.0)
A4_LANDSCAPE = (842.0, 595.0)
LETTER = (612.0, 792.0)


@pytest.mark.parametrize("screen", [
    (1920, 1080), (1920, 1200), (2880, 1920), (1024, 768), (1280, 1024),
])
@pytest.mark.parametrize("page", [A4, LETTER])
def test_upright_pages_share_a_wide_screen(screen, page):
    assert spread_suits(screen, page, gap=24)


@pytest.mark.parametrize("screen", [
    (1920, 1080), (1920, 1200), (2880, 1920), (1024, 768),
])
@pytest.mark.parametrize("page", [A4_LANDSCAPE, (792.0, 612.0), (1024.0, 768.0)])
def test_landscape_pages_get_the_wide_screen_to_themselves(screen, page):
    # Issue #17: side by side they were too small to read.
    assert not spread_suits(screen, page, gap=24)


@pytest.mark.parametrize("page", [A4, A4_LANDSCAPE, (500.0, 500.0)])
@pytest.mark.parametrize("screen", [(1080, 1920), (1200, 1200), (0, 0)])
def test_an_upright_or_square_screen_never_shows_a_spread(screen, page):
    assert not spread_suits(screen, page, gap=24)


def test_a_very_wide_screen_fits_two_landscape_pages():
    assert spread_suits((5120, 1440), A4_LANDSCAPE, gap=24)


def test_a_spread_is_dropped_once_it_would_shrink_the_pages_too_far():
    # Square pages: two fill a 2:1 screen, but halve in size on a 4:3 one.
    assert spread_suits((2000, 1000), (500.0, 500.0))
    assert spread_suits((1500, 1000), (500.0, 500.0))      # exactly 0.75
    assert not spread_suits((1400, 1000), (500.0, 500.0))


def test_rotation_and_crop_count_towards_the_page_shape():
    screen = (1920, 1080)
    # A landscape scan turned upright reads as an upright page...
    assert spread_suits(screen, content_size(A4_LANDSCAPE, None, 90), gap=24)
    # ...an upright page turned on its side as a landscape one...
    assert not spread_suits(screen, content_size(A4, None, 270), gap=24)
    # ...and so does one cropped to a wide strip.
    strip = CropRect(0.0, 0.0, 1.0, 0.4)
    assert not spread_suits(screen, content_size(A4, strip, 0), gap=24)


def test_a_page_without_a_size_is_not_a_spread():
    assert not spread_suits((1920, 1080), (0.0, 842.0))
