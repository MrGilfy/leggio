# SPDX-License-Identifier: GPL-3.0-or-later
"""PDF export: page geometry, and real round trips through cairo and Poppler."""

from __future__ import annotations

import hashlib
import itertools
import shutil
import threading

import pytest

cairo = pytest.importorskip("cairo")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler  # noqa: E402
except (ValueError, ImportError):
    pytest.skip("Poppler introspection data not installed", allow_module_level=True)

from leggio import export  # noqa: E402
from leggio.export import (  # noqa: E402
    ExportCancelled,
    ExportError,
    ExportScore,
    export_pdf,
    output_point,
    page_layout,
    suggested_name,
)
from leggio.links import make_link  # noqa: E402
from leggio.models import Annotation, CropRect  # noqa: E402
from leggio.pdf import open_document  # noqa: E402

PAGE = (600.0, 800.0)


def make_pdf(path, pages, label="Source"):
    surface = cairo.PDFSurface(str(path), *PAGE)
    ctx = cairo.Context(surface)
    for number in range(1, pages + 1):
        ctx.set_source_rgb(0, 0, 0)
        ctx.select_font_face("sans")
        ctx.set_font_size(24)
        ctx.move_to(60, 60)
        ctx.show_text(f"{label} page {number}")
        ctx.show_page()
    surface.finish()
    return path


def ink(page, points, width=0.01, color="#000000"):
    return Annotation(id=0, score_id=1, page=page, kind=Annotation.INK,
                      color=color, width=width,
                      data={"points": [list(p) for p in points]})


def fermata(page, x, y, size=0.08):
    return Annotation(id=0, score_id=1, page=page, kind=Annotation.SYMBOL,
                      color="#000000", width=0.003,
                      data={"glyph": "fermata", "x": x, "y": y, "size": size})


def render(page, scale=1.0):
    width, height = page.get_size()
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24,
                                 int(width * scale), int(height * scale))
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
    page.render(ctx)
    surface.flush()
    return surface


def darkness(surface, x, y, radius=2):
    """Darkest grey level (0 black .. 255 white) in a small square."""
    data = surface.get_data()
    stride = surface.get_stride()
    darkest = 255
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            px, py = int(x) + dx, int(y) + dy
            if 0 <= px < surface.get_width() and 0 <= py < surface.get_height():
                offset = py * stride + px * 4
                darkest = min(darkest, data[offset], data[offset + 1],
                              data[offset + 2])
    return darkest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- geometry

CROPS = [None, CropRect(0.1, 0.05, 0.9, 0.75)]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("crop", CROPS)
def test_page_matrix_agrees_with_annotation_placement(rotation, crop):
    """The PDF content and the annotations go through two code paths; a
    page-space point must land on the same output spot in both."""
    out_w, out_h, matrix = page_layout(PAGE, crop, rotation)
    m = cairo.Matrix(*matrix)
    for u, v in itertools.product((0.0, 0.3, 0.75, 1.0), repeat=2):
        expected = output_point(u, v, PAGE, crop, rotation)
        assert m.transform_point(u * PAGE[0], v * PAGE[1]) == pytest.approx(expected)


@pytest.mark.parametrize("rotation, size", [
    (0, (600.0, 800.0)), (90, (800.0, 600.0)),
    (180, (600.0, 800.0)), (270, (800.0, 600.0)),
])
def test_rotation_turns_the_output_page(rotation, size):
    assert page_layout(PAGE, None, rotation)[:2] == size


def test_quarter_turn_moves_the_top_left_corner_to_the_top_right():
    # Clockwise, like the viewer: the page's top edge becomes the right edge.
    assert output_point(0.0, 0.0, PAGE, None, 90) == pytest.approx((800.0, 0.0))
    assert output_point(0.0, 1.0, PAGE, None, 90) == pytest.approx((0.0, 0.0))


def test_crop_shrinks_the_page_and_shifts_points():
    crop = CropRect(0.25, 0.5, 0.75, 1.0)
    assert page_layout(PAGE, crop, 0)[:2] == (300.0, 400.0)
    assert output_point(0.25, 0.5, PAGE, crop, 0) == pytest.approx((0.0, 0.0))


def test_suggested_names():
    assert suggested_name("Sonata in C") == "Sonata in C (annotated).pdf"
    assert suggested_name("Sonata", annotated=False) == "Sonata (export).pdf"
    assert suggested_name("Gig: 12/5", setlist=True) == "Gig 12 5.pdf"
    assert suggested_name("..") == "Score (annotated).pdf"


# -------------------------------------------------------------- round trip

def test_round_trip_keeps_vector_text_and_draws_annotations(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 2)
    before = sha(source)
    line = ink(0, [(0.2, 0.5), (0.5, 0.5), (0.8, 0.5)], width=0.01)
    score = ExportScore(path=str(source), title="Source",
                        annotations={0: [line, fermata(0, 0.5, 0.3)]})
    destination = tmp_path / "out.pdf"

    pages = export_pdf([score], destination, title="Source")

    assert pages == 2
    assert sha(source) == before                      # original untouched
    document = open_document(destination)
    assert document.get_n_pages() == 2
    assert document.get_title() == "Source"
    page = document.get_page(0)
    assert page.get_size() == pytest.approx(PAGE)
    # Text survives as text: the page was not flattened to a bitmap.
    assert "Source page 1" in page.get_text()

    image = render(page)
    assert darkness(image, *output_point(0.5, 0.5, PAGE, None, 0)) < 60
    assert darkness(image, *output_point(0.5, 0.7, PAGE, None, 0)) > 240
    fx, fy = output_point(0.5, 0.3, PAGE, None, 0)
    assert darkness(image, fx, fy - 10, radius=12) < 60  # the fermata's arc
    # The second page carries no annotations.
    assert darkness(render(document.get_page(1)),
                    *output_point(0.5, 0.5, PAGE, None, 0)) > 240


def test_round_trip_with_rotation_and_crop(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    crop = CropRect(0.0, 0.0, 1.0, 0.75)
    # A short vertical stroke in page space, near the left edge.
    stroke = ink(0, [(0.1, 0.4), (0.1, 0.6)], width=0.012)
    score = ExportScore(path=str(source), title="Rotated", rotation=90,
                        crop=crop, annotations={0: [stroke]})
    destination = tmp_path / "rotated.pdf"
    export_pdf([score], destination)

    page = open_document(destination).get_page(0)
    out_w, out_h = page_layout(PAGE, crop, 90)[:2]
    assert page.get_size() == pytest.approx((out_w, out_h))
    assert (out_w, out_h) == (600.0, 600.0)
    image = render(page)
    # Rotated a quarter turn, the stroke runs horizontally near the top.
    sx, sy = output_point(0.1, 0.5, PAGE, crop, 90)
    assert sy < out_h * 0.2
    assert darkness(image, sx, sy) < 60
    assert darkness(image, sx, sy + 40) > 240
    # The text was rotated with the page, and the crop cut nothing it needed.
    assert "Source page 1" in page.get_text()


def test_links_bookmarks_and_hidden_kinds_are_not_printed(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    link = make_link(1, 0, 0.5, 0.5, 0, 0.5, 0.9)
    score = ExportScore(path=str(source), title="S", annotations={0: [link]})
    destination = tmp_path / "out.pdf"
    export_pdf([score], destination)
    image = render(open_document(destination).get_page(0))
    assert darkness(image, *output_point(0.5, 0.5, PAGE, None, 0), radius=15) > 240


def test_setlist_is_one_pdf_in_order(tmp_path):
    first = make_pdf(tmp_path / "a.pdf", 2, label="First")
    second = make_pdf(tmp_path / "b.pdf", 3, label="Second")
    destination = tmp_path / "Gig.pdf"
    progress = []
    pages = export_pdf(
        [ExportScore(str(first), "First"), ExportScore(str(second), "Second")],
        destination, title="Gig", progress=lambda d, t: progress.append((d, t)),
    )
    assert pages == 5
    assert progress == [(n, 5) for n in range(1, 6)]
    document = open_document(destination)
    texts = [document.get_page(i).get_text().strip() for i in range(5)]
    assert texts == ["First page 1", "First page 2",
                     "Second page 1", "Second page 2", "Second page 3"]


# ----------------------------------------------------------------- safety

def test_refuses_to_write_over_the_source(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    before = sha(source)
    with pytest.raises(ExportError, match="original"):
        export_pdf([ExportScore(str(source), "S")], source)
    assert sha(source) == before


def test_refuses_the_source_under_another_path(tmp_path):
    """What the Flatpak save portal does: the same file under a
    /run/user/.../doc/... path. Recognised by content."""
    source = make_pdf(tmp_path / "source.pdf", 1)
    portal_copy = tmp_path / "doc" / "source.pdf"
    portal_copy.parent.mkdir()
    shutil.copy(source, portal_copy)
    assert export.is_source(portal_copy, [str(source)])
    with pytest.raises(ExportError):
        export_pdf([ExportScore(str(source), "S")], portal_copy)


def test_overwrites_an_older_export(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    destination = tmp_path / "out.pdf"
    destination.write_bytes(b"an older export")
    export_pdf([ExportScore(str(source), "S")], destination)
    assert open_document(destination).get_n_pages() == 1


def test_cancel_leaves_no_file_and_no_temp(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 3)
    destination = tmp_path / "out.pdf"
    temp_dir = tmp_path / "cache"
    cancel = threading.Event()
    with pytest.raises(ExportCancelled):
        export_pdf([ExportScore(str(source), "S")], destination,
                   progress=lambda done, total: cancel.set(),
                   cancel=cancel, temp_dir=temp_dir)
    assert not destination.exists()
    assert list(temp_dir.iterdir()) == []


def test_cancel_keeps_an_existing_file_as_it_was(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 2)
    destination = tmp_path / "out.pdf"
    destination.write_bytes(b"keep me")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ExportCancelled):
        export_pdf([ExportScore(str(source), "S")], destination, cancel=cancel)
    assert destination.read_bytes() == b"keep me"


def test_unreadable_source_fails_cleanly(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    destination = tmp_path / "out.pdf"
    with pytest.raises(export.PdfError):
        export_pdf([ExportScore(str(broken), "B")], destination)
    assert not destination.exists()


def test_poppler_is_real():
    # Guards against these tests silently running against a stub.
    assert hasattr(Poppler.Page, "render_for_printing")
