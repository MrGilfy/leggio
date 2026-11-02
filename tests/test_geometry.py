# SPDX-License-Identifier: GPL-3.0-or-later
"""Page space <-> screen transforms, including rotation and crop."""

from __future__ import annotations

import itertools

import pytest

from leggio.geometry import (
    content_size,
    page_to_screen,
    page_unit,
    rotate_point,
    rotate_rect,
    screen_to_page,
    unrotate_point,
    unrotate_rect,
)
from leggio.models import CropRect

ROTATIONS = (0, 90, 180, 270)
POINTS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (0.25, 0.75), (0.6, 0.1)]
CROPS = [None, CropRect(0.1, 0.2, 0.6, 0.7), CropRect(0.0, 0.05, 0.9, 1.0)]
RECT = (10.0, 20.0, 300.0, 400.0)


def approx(pair):
    return pytest.approx(pair, abs=1e-9)


@pytest.mark.parametrize(
    "rotation, expected",
    [
        (0, (0.0, 0.0)),
        (90, (1.0, 0.0)),    # clockwise: top-left goes to top-right
        (180, (1.0, 1.0)),
        (270, (0.0, 1.0)),
    ],
)
def test_rotate_point_moves_top_left_corner_clockwise(rotation, expected):
    assert rotate_point(0.0, 0.0, rotation) == approx(expected)


@pytest.mark.parametrize("rotation, point", itertools.product(ROTATIONS, POINTS))
def test_unrotate_point_inverts_rotate_point(rotation, point):
    assert unrotate_point(*rotate_point(*point, rotation), rotation) == approx(point)


def test_four_quarter_turns_are_identity():
    u, v = 0.3, 0.8
    a, b = u, v
    for _ in range(4):
        a, b = rotate_point(a, b, 90)
    assert (a, b) == approx((u, v))


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_rotate_rect_is_bounding_box_of_rotated_corners(rotation):
    rect = CropRect(0.1, 0.2, 0.6, 0.9)
    corners = [
        rotate_point(x, y, rotation)
        for x in (rect.x0, rect.x1)
        for y in (rect.y0, rect.y1)
    ]
    rotated = rotate_rect(rect, rotation)
    assert rotated.x0 == pytest.approx(min(c[0] for c in corners))
    assert rotated.x1 == pytest.approx(max(c[0] for c in corners))
    assert rotated.y0 == pytest.approx(min(c[1] for c in corners))
    assert rotated.y1 == pytest.approx(max(c[1] for c in corners))


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_unrotate_rect_inverts_rotate_rect(rotation):
    rect = CropRect(0.1, 0.2, 0.6, 0.9)
    back = unrotate_rect(rotate_rect(rect, rotation), rotation)
    assert (back.x0, back.y0, back.x1, back.y1) == pytest.approx(
        (rect.x0, rect.y0, rect.x1, rect.y1)
    )


def test_page_to_screen_without_crop_or_rotation_is_linear():
    assert page_to_screen(0.5, 0.5, RECT, 0, None) == approx((160.0, 220.0))
    assert page_to_screen(0.0, 0.0, RECT, 0, None) == approx((10.0, 20.0))
    assert page_to_screen(1.0, 1.0, RECT, 0, None) == approx((310.0, 420.0))


def test_crop_corners_land_on_the_page_rectangle_corners():
    crop = CropRect(0.1, 0.2, 0.6, 0.7)
    assert page_to_screen(0.1, 0.2, RECT, 0, crop) == approx((10.0, 20.0))
    assert page_to_screen(0.6, 0.7, RECT, 0, crop) == approx((310.0, 420.0))


def test_rotated_crop_puts_page_top_left_at_screen_top_right():
    crop = CropRect(0.1, 0.2, 0.6, 0.7)
    x, y, w, _h = RECT
    assert page_to_screen(0.1, 0.2, RECT, 90, crop) == approx((x + w, y))


@pytest.mark.parametrize(
    "rotation, crop, point", itertools.product(ROTATIONS, CROPS, POINTS)
)
def test_screen_to_page_inverts_page_to_screen(rotation, crop, point):
    screen = page_to_screen(*point, RECT, rotation, crop)
    assert screen_to_page(*screen, RECT, rotation, crop) == approx(point)


def test_page_unit_scales_with_crop_width():
    assert page_unit(300.0, 0, None) == pytest.approx(300.0)
    assert page_unit(300.0, 0, CropRect(0.0, 0.0, 0.5, 1.0)) == pytest.approx(600.0)
    # A quarter turn puts the crop's height across the screen.
    assert page_unit(300.0, 90, CropRect(0.0, 0.0, 1.0, 0.5)) == pytest.approx(
        600.0
    )


def test_content_size_crops_then_rotates():
    crop = CropRect(0.0, 0.0, 0.5, 0.25)
    assert content_size((600.0, 800.0), None, 0) == (600.0, 800.0)
    assert content_size((600.0, 800.0), crop, 0) == (300.0, 200.0)
    assert content_size((600.0, 800.0), crop, 90) == (200.0, 300.0)
    assert content_size((600.0, 800.0), crop, 270) == (200.0, 300.0)
