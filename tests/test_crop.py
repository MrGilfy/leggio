# SPDX-License-Identifier: GPL-3.0-or-later
"""Margin detection on synthetic pages."""

from __future__ import annotations

import pytest

cairo = pytest.importorskip("cairo")

from leggio.crop import MIN_AREA, PADDING, SAMPLE_WIDTH, detect_crop, sample_pages
from leggio.models import CropRect

# One sample pixel, as a page fraction, plus a little slack.
TOLERANCE = 1.5 / SAMPLE_WIDTH


class FakePage:
    """Duck-types the two Poppler.Page methods detect_crop uses."""

    def __init__(self, boxes=(), size=(600.0, 800.0), grey=None, broken=False):
        self.boxes = boxes      # ink rectangles as page fractions
        self.size = size
        self.grey = grey        # background tint, 0..1
        self.broken = broken

    def get_size(self):
        return self.size

    def render(self, ctx):
        if self.broken:
            raise RuntimeError("damaged page")
        width, height = self.size
        if self.grey is not None:
            ctx.set_source_rgb(self.grey, self.grey, self.grey)
            ctx.paint()
        ctx.set_source_rgb(0, 0, 0)
        for x0, y0, x1, y1 in self.boxes:
            ctx.rectangle(x0 * width, y0 * height,
                          (x1 - x0) * width, (y1 - y0) * height)
        ctx.fill()


class FakeDocument:
    def __init__(self, pages):
        self.pages = pages

    def get_page(self, index):
        return self.pages[index]


def detect(pages):
    return detect_crop(FakeDocument(pages), len(pages))


def assert_crop(result, expected):
    assert result is not None
    padded = (
        expected[0] - PADDING, expected[1] - PADDING,
        expected[2] + PADDING, expected[3] + PADDING,
    )
    assert (result.x0, result.y0, result.x1, result.y1) == pytest.approx(
        padded, abs=TOLERANCE
    )


@pytest.mark.parametrize(
    "count, expected",
    [(0, []), (1, [0]), (3, [0, 1, 2]), (5, [0, 1, 2, 3, 4])],
)
def test_sample_pages_takes_every_page_of_short_scores(count, expected):
    assert sample_pages(count) == expected


def test_sample_pages_spreads_across_long_scores():
    pages = sample_pages(100)
    assert pages[0] == 0 and pages[-1] == 99
    assert len(pages) == 5
    assert pages == sorted(set(pages))


def test_single_page_box_is_padded():
    box = (0.2, 0.15, 0.8, 0.85)
    assert_crop(detect([FakePage([box])]), box)


def test_union_keeps_ink_from_every_sampled_page():
    top = FakePage([(0.1, 0.1, 0.5, 0.5)])
    bottom = FakePage([(0.4, 0.4, 0.85, 0.9)])
    assert_crop(detect([top, bottom]), (0.1, 0.1, 0.85, 0.9))


def test_blank_score_gives_no_crop():
    assert detect([FakePage(), FakePage()]) is None


def test_faint_scanner_background_counts_as_paper():
    box = (0.2, 0.15, 0.8, 0.85)
    assert_crop(detect([FakePage([box], grey=0.97)]), box)


def test_implausibly_small_crop_is_discarded():
    side = (MIN_AREA ** 0.5) / 2
    assert detect([FakePage([(0.4, 0.4, 0.4 + side, 0.4 + side)])]) is None


def test_nothing_to_trim_gives_no_crop():
    assert detect([FakePage([(0.0, 0.0, 1.0, 1.0)])]) is None


def test_damaged_page_is_skipped():
    box = (0.2, 0.15, 0.8, 0.85)
    assert_crop(detect([FakePage(broken=True), FakePage([box])]), box)


def test_crop_is_clamped_to_the_page():
    result = detect([FakePage([(0.0, 0.0, 0.7, 0.6)])])
    assert result is not None
    assert result.x0 == 0.0 and result.y0 == 0.0
    assert result == result.clamped()


def test_real_pdf_through_poppler(tmp_path):
    pytest.importorskip("gi")
    try:
        from leggio.pdf import open_document
    except (ImportError, ValueError) as exc:
        pytest.skip(f"Poppler bindings unavailable: {exc}")

    path = tmp_path / "score.pdf"
    surface = cairo.PDFSurface(str(path), 600, 800)
    ctx = cairo.Context(surface)
    for _ in range(3):
        ctx.set_source_rgb(0, 0, 0)
        ctx.rectangle(120, 160, 360, 480)     # 0.2..0.8 x 0.2..0.8
        ctx.fill()
        ctx.show_page()
    surface.finish()

    result = detect_crop(open_document(str(path)), 3)
    assert_crop(result, (0.2, 0.2, 0.8, 0.8))
    assert isinstance(result, CropRect)
