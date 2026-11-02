# SPDX-License-Identifier: GPL-3.0-or-later
"""Thumbnail generation and auto-crop detection, on a worker thread.

Both need the document open, so each PDF is opened once for both. Kept off
the render thread so a first-run scan never stalls a page turn.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Callable, Optional

import cairo
from gi.repository import GLib

from .config import THUMB_DIR
from .crop import detect_crop
from .db import Database
from .models import CropRect, Score
from .pdf import PdfError, open_document

log = logging.getLogger(__name__)


def thumb_path(score: Score, width: int) -> Path:
    """Cache filename keyed by content, so a moved file keeps its thumbnail
    and an edited file gets a fresh one."""
    key = score.content_key or f"id{score.id}"
    return THUMB_DIR / f"{key}-{width}.png"


def _render_thumb(
    document,
    crop: Optional[CropRect],
    rotation: int,
    width: int,
    destination: Path,
) -> bool:
    page = document.get_page(0)
    if page is None:
        return False
    page_w, page_h = page.get_size()
    if page_w <= 0 or page_h <= 0:
        return False

    box = crop if crop is not None else CropRect(0.0, 0.0, 1.0, 1.0)
    visible_w = page_w * box.width
    visible_h = page_h * box.height
    if visible_w <= 0 or visible_h <= 0:
        return False

    swapped = rotation in (90, 270)
    out_w = width
    scale = out_w / (visible_h if swapped else visible_w)
    out_h = max(1, int(round((visible_w if swapped else visible_h) * scale)))

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, out_w, out_h)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()

    if rotation:
        ctx.translate(out_w / 2, out_h / 2)
        ctx.rotate(rotation * 3.141592653589793 / 180.0)
        ctx.translate(-(out_h if swapped else out_w) / 2,
                      -(out_w if swapped else out_h) / 2)

    ctx.scale(scale, scale)
    # Shift so the crop box's top-left lands at the origin.
    ctx.translate(-page_w * box.x0, -page_h * box.y0)
    page.render(ctx)
    surface.flush()

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".png.tmp")
    surface.write_to_png(str(tmp))
    tmp.replace(destination)
    return True


class ThumbnailWorker:
    """Single background thread draining a queue of scores."""

    def __init__(self, db: Database, width: int = 320):
        self.db = db
        self.width = width
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._queued: set[int] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="leggio-thumbs", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def request(
        self,
        score: Score,
        on_ready: Callable[[int, str, Optional[CropRect]], None],
        force: bool = False,
    ) -> None:
        """Queue a score. on_ready fires on the main loop with
        (score_id, thumbnail_path, detected_crop_or_None)."""
        with self._lock:
            if score.id in self._queued:
                return
            self._queued.add(score.id)
        self._queue.put((score, on_ready, force))

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            score, on_ready, force = item
            try:
                self._process(score, on_ready, force)
            except Exception:
                log.exception("thumbnail worker failed on %s", score.path)
            finally:
                with self._lock:
                    self._queued.discard(score.id)

    def _process(self, score: Score, on_ready, force: bool) -> None:
        destination = thumb_path(score, self.width)
        crop = score.crop
        needs_crop = crop is None and score.crop_auto

        if destination.exists() and not needs_crop and not force:
            GLib.idle_add(on_ready, score.id, str(destination), None)
            return

        path = Path(score.path)
        if not path.exists():
            return

        try:
            document = open_document(path)
        except PdfError as exc:
            log.warning("thumbnail: %s", exc)
            return

        detected = None
        if needs_crop:
            detected = detect_crop(document, max(1, score.page_count))
            # Write only a found crop, so a miss is retried next time.
            if detected is not None:
                self.db.set_crop(score.id, detected, auto=True)
                crop = detected

        ok = False
        try:
            ok = _render_thumb(document, crop, score.rotation, self.width, destination)
        except (cairo.Error, OSError, MemoryError) as exc:
            log.warning("could not render thumbnail for %s: %s", score.path, exc)
        finally:
            document = None

        if ok:
            GLib.idle_add(on_ready, score.id, str(destination), detected)
