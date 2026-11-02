# SPDX-License-Identifier: GPL-3.0-or-later
"""Export a score, or a whole setlist, as a new PDF with annotations drawn in.

Pages go through Poppler's render_for_printing into a cairo PDF surface, so
the music stays vector, and ink and symbols are drawn on top as vectors by
the same code the viewer uses. The score's rotation is always applied, its
crop only on request. Jump-link markers, bookmarks and night mode are not
part of the export.

The original PDF is never written to. The export is built in a temporary
file and only copied over the chosen file once complete, and a destination
that is one of the source PDFs is refused by content, not by path: inside
Flatpak the save portal hands back a /run/user/.../doc/... path that never
equals the source path even when it is the same file.

No GTK here: the UI runs export_pdf() on a worker thread.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import cairo

from . import annotation_paint, geometry
from .models import Annotation, CropRect
from .pdf import PdfError, content_key, open_document

PRINTED_KINDS = (Annotation.INK, Annotation.SYMBOL)

Matrix = tuple[float, float, float, float, float, float]


class ExportCancelled(Exception):
    """The export was cancelled before it finished."""


class ExportError(Exception):
    """The export could not be written."""


@dataclass
class ExportScore:
    """One score to export, with everything read from the database up front
    so the worker thread never touches it."""

    path: str
    title: str
    rotation: int = 0
    crop: Optional[CropRect] = None      # None: the whole page
    annotations: dict[int, list[Annotation]] = field(default_factory=dict)


# ---------------------------------------------------------------- geometry

def page_layout(page_size: tuple[float, float], crop: Optional[CropRect],
                rotation: int) -> tuple[float, float, Matrix]:
    """Output page size in points, and the cairo matrix taking the PDF
    page's own coordinates to the output page: crop, then rotate."""
    out_w, out_h = geometry.content_size(page_size, crop, rotation)
    page_w, page_h = page_size

    def to_out(u: float, v: float) -> tuple[float, float]:
        return geometry.page_to_screen(u, v, (0.0, 0.0, out_w, out_h),
                                       rotation, crop)

    x0, y0 = to_out(0.0, 0.0)
    x1, y1 = to_out(1.0, 0.0)
    x2, y2 = to_out(0.0, 1.0)
    matrix = (
        (x1 - x0) / page_w, (y1 - y0) / page_w,
        (x2 - x0) / page_h, (y2 - y0) / page_h,
        x0, y0,
    )
    return out_w, out_h, matrix


def output_point(u: float, v: float, page_size: tuple[float, float],
                 crop: Optional[CropRect], rotation: int) -> tuple[float, float]:
    """Where a page-space point lands on the output page, in points."""
    out_w, out_h = geometry.content_size(page_size, crop, rotation)
    return geometry.page_to_screen(u, v, (0.0, 0.0, out_w, out_h), rotation, crop)


# ------------------------------------------------------------------ naming

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')


def suggested_name(title: str, annotated: bool = True,
                   setlist: bool = False) -> str:
    base = " ".join(_UNSAFE.sub(" ", title).split()).strip(" .") or "Score"
    if setlist:
        return f"{base}.pdf"
    return f"{base} ({'annotated' if annotated else 'export'}).pdf"


def is_source(destination: Path, sources: Sequence[str]) -> bool:
    """True if writing to `destination` would overwrite one of the sources."""
    if not destination.exists():
        return False
    for source in sources:
        try:
            if os.path.samefile(destination, source):
                return True
        except OSError:
            pass
    key = content_key(destination)
    if key is None:
        return False
    return any(content_key(Path(source)) == key for source in sources)


# ------------------------------------------------------------------ export

def export_pdf(
    scores: Sequence[ExportScore],
    destination: Path,
    *,
    title: str = "",
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
    temp_dir: Optional[Path] = None,
) -> int:
    """Write the scores, in order, into one PDF at `destination`.

    progress(done, total) is called after each page. Returns the page count.
    Raises ExportCancelled, ExportError or PdfError; in every failure case
    `destination` is left as it was.
    """
    if is_source(destination, [s.path for s in scores]):
        raise ExportError("That is the original PDF. Choose another name.")

    documents = []
    for score in scores:
        documents.append(open_document(score.path))   # PdfError if unreadable
    total = sum(document.get_n_pages() for document in documents)
    if total == 0:
        raise ExportError("There are no pages to export.")

    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix="leggio-export-", suffix=".pdf",
        dir=str(temp_dir) if temp_dir is not None else None,
    )
    os.close(handle)
    temp = Path(temp_name)
    try:
        done = _write(scores, documents, temp, title, total, progress, cancel)
        if cancel is not None and cancel.is_set():
            raise ExportCancelled()
        _copy_into(temp, destination)
        return done
    finally:
        temp.unlink(missing_ok=True)


def _write(scores, documents, temp: Path, title: str, total: int,
           progress, cancel) -> int:
    surface = cairo.PDFSurface(str(temp), 595.0, 842.0)
    try:
        surface.set_metadata(cairo.PDFMetadata.TITLE, title or scores[0].title)
        surface.set_metadata(cairo.PDFMetadata.CREATOR, "Leggio")
    except (AttributeError, cairo.Error):
        pass                                # older cairo: no metadata
    done = 0
    for score, document in zip(scores, documents, strict=True):
        for index in range(document.get_n_pages()):
            if cancel is not None and cancel.is_set():
                surface.finish()
                raise ExportCancelled()
            page = document.get_page(index)
            if page is None:
                continue
            _draw_page(surface, page, score, index)
            done += 1
            if progress is not None:
                progress(done, total)
    surface.finish()
    return done


def _draw_page(surface: cairo.PDFSurface, page, score: ExportScore,
               index: int) -> None:
    page_size = page.get_size()
    crop = score.crop
    rotation = score.rotation % 360
    out_w, out_h, matrix = page_layout(page_size, crop, rotation)
    surface.set_size(out_w, out_h)
    ctx = cairo.Context(surface)

    ctx.save()
    ctx.rectangle(0, 0, out_w, out_h)
    ctx.clip()
    ctx.transform(cairo.Matrix(*matrix))
    page.render_for_printing(ctx)
    ctx.restore()

    annotations = score.annotations.get(index, [])
    if annotations:
        def to_out(u: float, v: float) -> tuple[float, float]:
            return geometry.page_to_screen(
                u, v, (0.0, 0.0, out_w, out_h), rotation, crop
            )

        unit = geometry.page_unit(out_w, rotation, crop)
        ctx.save()
        ctx.rectangle(0, 0, out_w, out_h)
        ctx.clip()
        annotation_paint.prepare(ctx)
        for annotation in annotations:
            if annotation.kind in PRINTED_KINDS:
                annotation_paint.paint_annotation(
                    ctx, annotation, to_out, unit, rotation
                )
        ctx.restore()
    ctx.show_page()


def _copy_into(temp: Path, destination: Path) -> None:
    """Copy the finished export over the chosen file. Written in place, not
    renamed: the save portal grants that one file and nothing next to it."""
    try:
        with temp.open("rb") as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    except OSError as exc:
        message = f"Could not write {destination.name}: {exc.strerror}"
        raise ExportError(message) from exc


__all__ = [
    "ExportCancelled", "ExportError", "ExportScore", "PdfError",
    "export_pdf", "is_source", "output_point", "page_layout", "suggested_name",
]
