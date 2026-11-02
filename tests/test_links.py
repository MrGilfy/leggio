# SPDX-License-Identifier: GPL-3.0-or-later
"""Jump links: the data, where a jump lands, picking a marker, Back."""

from __future__ import annotations

import pytest

from leggio import links
from leggio.links import BackState, Link, landing, make_link
from leggio.models import Annotation, CropRect


def link(page=3, x=0.8, y=0.9, target_page=1, tx=0.1, ty=0.25, id=7):
    return Link(id=id, page=page, x=x, y=y, target_page=target_page,
                tx=tx, ty=ty)


# -------------------------------------------------------------------- data

def test_make_link_round_trips_through_an_annotation():
    annotation = make_link(5, 3, 0.8, 0.9, 1, 0.1, 0.25, created=2.0)
    assert annotation.kind == Annotation.LINK
    assert annotation.page == 3
    annotation.id = 11
    assert Link.from_annotation(annotation) == link(id=11)


def test_make_link_clamps_to_the_page():
    data = make_link(1, 0, -0.2, 1.4, 2, 1.01, -3).data
    assert (data["x"], data["y"], data["tx"], data["ty"]) == (0.0, 1.0, 1.0, 0.0)


def test_other_kinds_and_broken_data_are_not_links():
    ink = Annotation(id=1, score_id=1, page=0, kind=Annotation.INK,
                     color="#000000", width=0.01, data={"points": []})
    assert Link.from_annotation(ink) is None
    broken = make_link(1, 0, 0.5, 0.5, 1, 0.5, 0.5)
    del broken.data["target_page"]
    assert Link.from_annotation(broken) is None


@pytest.mark.parametrize("target_page, ty, forward", [
    (5, 0.1, True),     # To Coda, later page
    (3, 0.95, True),    # same page, further down
    (3, 0.2, False),    # same page, back up: a repeat
    (0, 0.4, False),    # D.S. to an earlier page
])
def test_direction(target_page, ty, forward):
    assert link(page=3, y=0.9, target_page=target_page, ty=ty).forward is forward


# ----------------------------------------------------------------- landing

@pytest.mark.parametrize("rotation, expected", [
    (0, 0.25),      # ty as is
    (90, 0.1),      # rotated a quarter turn clockwise: tx runs top to bottom
    (180, 0.75),
    (270, 0.9),
])
def test_display_fraction_follows_rotation(rotation, expected):
    assert links.display_fraction(0.1, 0.25, rotation, None) == pytest.approx(expected)


def test_display_fraction_follows_crop():
    crop = CropRect(0.0, 0.2, 1.0, 0.7)    # 0.2 .. 0.7 of the page is shown
    assert links.display_fraction(0.5, 0.45, 0, crop) == pytest.approx(0.5)
    # Above the crop: clamped to the top edge rather than off the screen.
    assert links.display_fraction(0.5, 0.1, 0, crop) == 0.0


def test_top_landing_puts_the_target_at_the_top_with_a_margin():
    page, offset = landing(link(ty=0.25), "top", rotation=0, crop=None,
                           margin=0.05)
    assert page == 1
    assert offset == pytest.approx(0.20)


def test_top_landing_never_scrolls_above_the_page():
    assert landing(link(ty=0.01), "top", rotation=0, crop=None,
                   margin=0.05) == (1, 0.0)


def test_top_landing_survives_crop_and_rotation():
    crop = CropRect(0.0, 0.0, 1.0, 0.5)
    # Rotated 180 degrees, ty 0.25 sits halfway down the cropped half.
    page, offset = landing(link(ty=0.25), "top", rotation=180, crop=crop)
    assert page == 1
    rotated = links.display_fraction(0.1, 0.25, 180, crop)
    assert offset == pytest.approx(rotated)


def test_whole_page_landing_shows_the_page_whole():
    assert landing(link(ty=0.8), "page", rotation=0, crop=None) == (1, 0.0)


@pytest.mark.parametrize("current, target, left", [
    (0, 5, 4),    # spreads 0-1, 2-3, 4-5: target on the right of 4-5
    (1, 5, 5),    # spreads 1-2, 3-4, 5-6: target on the left of 5-6
    (1, 2, 1),
    (2, 0, 0),
    (1, 0, 0),    # nothing before page 0 to pair it with
])
def test_spread_landing_keeps_the_current_pairing(current, target, left):
    page, offset = landing(link(target_page=target), "spread", rotation=0,
                           crop=None, current_page=current)
    assert (page, offset) == (left, 0.0)


# ---------------------------------------------------------- picking markers

def test_marker_at_picks_the_nearest_within_reach():
    near, far = link(id=1), link(id=2)
    spots = [(far, 130.0, 100.0, 0), (near, 108.0, 100.0, 0)]
    assert links.marker_at(spots, 100, 100, 22).id == 1
    assert links.marker_at(spots, 100, 150, 22) is None


def test_reading_order_is_page_order_then_top_to_bottom():
    on_right_page_top = (link(id=1), 900.0, 50.0, 1)
    on_left_page_bottom = (link(id=2), 100.0, 700.0, 0)
    on_left_page_middle = (link(id=3), 300.0, 400.0, 0)
    spots = [on_right_page_top, on_left_page_bottom, on_left_page_middle]
    assert links.first_in_reading_order(spots, 800).id == 3


def test_reading_order_skips_markers_off_screen():
    above = (link(id=1), 100.0, -20.0, 0)
    visible = (link(id=2), 100.0, 300.0, 1)
    assert links.first_in_reading_order([above, visible], 800).id == 2
    assert links.first_in_reading_order([above], 800) is None


# -------------------------------------------------------------------- back

def test_back_returns_to_where_the_jump_started_once():
    state = BackState()
    assert not state.available
    state.jumped((0, 4, 0.5))
    assert state.available
    assert state.take() == (0, 4, 0.5)
    assert state.take() is None


def test_back_expires_after_two_page_turns():
    state = BackState()
    state.jumped((0, 4, 0.0))
    state.turned()
    assert state.available
    state.turned()
    assert not state.available


def test_a_new_jump_replaces_back_and_restarts_the_count():
    state = BackState()
    state.jumped((0, 1, 0.0))
    state.turned()
    state.jumped((0, 6, 0.0))
    state.turned()
    assert state.take() == (0, 6, 0.0)


# ------------------------------------------------- bookmarks share landing

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("crop", [None, CropRect(0.1, 0.05, 0.9, 0.8)])
@pytest.mark.parametrize("offset", [0.0, 0.25, 0.5, 0.9])
def test_top_point_is_the_reverse_of_display_fraction(rotation, crop, offset):
    u, v = links.top_point(offset, rotation, crop)
    assert links.display_fraction(u, v, rotation, crop) == pytest.approx(offset)


def test_top_point_without_offset_is_the_top_of_the_page():
    assert links.top_point(0.0, 0, None) == (0.5, 0.0)
    # Rotated a quarter turn, the top of the screen is the page's left edge.
    assert links.top_point(0.0, 90, None) == pytest.approx((0.0, 0.5))


def test_a_bookmark_lands_where_it_was_added():
    # Added in the half-page view with page 4 scrolled halfway.
    u, v = links.top_point(0.5, 0, None)
    assert links.land(4, u, v, "top", rotation=0, crop=None) == (4, 0.5)
    assert links.land(4, u, v, "page", rotation=0, crop=None) == (4, 0.0)
