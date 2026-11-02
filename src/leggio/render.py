# SPDX-License-Identifier: GPL-3.0-or-later
"""Page rendering service.

Pages are rasterised on one background thread at their exact on-screen
device-pixel size, cached as cairo image surfaces and blitted 1:1, so a page
turn is a cache lookup. Neighbouring pages are prefetched at lower priority.
Only finished surfaces, never Poppler objects, reach the main loop.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

import cairo
from gi.repository import GLib

from .models import CropRect
from .pdf import PdfError, open_document

log = logging.getLogger(__name__)

# Guard against a pathological zoom asking for a surface bigger than we
# could sensibly allocate or upload.
MAX_DIMENSION = 8192

# Documents kept open on the worker. A setlist paging across scores wants
# more than one; beyond a handful the memory is not worth it.
MAX_OPEN_DOCUMENTS = 4

PRIORITY_INFO = 0
PRIORITY_VISIBLE = 1
PRIORITY_PREFETCH = 2


@dataclass(frozen=True)
class RenderKey:
    """Everything that affects the pixels. Two requests with equal keys are
    guaranteed to produce identical output, which is what makes the cache
    sound."""

    path: str
    page: int
    width: int          # device pixels
    height: int         # device pixels
    rotation: int       # 0 | 90 | 180 | 270
    crop: tuple[float, float, float, float]

    @property
    def nbytes(self) -> int:
        return self.width * self.height * 4


class PageCache:
    """LRU bounded by bytes: one page can be <1 MB or ~34 MB."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._entries: "OrderedDict[RenderKey, cairo.ImageSurface]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: RenderKey) -> Optional[cairo.ImageSurface]:
        with self._lock:
            surface = self._entries.get(key)
            if surface is not None:
                self._entries.move_to_end(key)
            return surface

    def put(self, key: RenderKey, surface: cairo.ImageSurface) -> None:
        with self._lock:
            if key in self._entries:
                self._bytes -= key.nbytes
                del self._entries[key]
            self._entries[key] = surface
            self._bytes += key.nbytes
            while self._bytes > self.max_bytes and len(self._entries) > 1:
                evicted, _ = self._entries.popitem(last=False)
                self._bytes -= evicted.nbytes

    def find_similar(
        self, path: str, page: int, rotation: int, crop: tuple
    ) -> Optional[cairo.ImageSurface]:
        """Any cached render of this page at another size, shown soft while
        the right one renders.
        """
        with self._lock:
            best = None
            best_area = 0
            for key, surface in self._entries.items():
                if (key.path, key.page, key.rotation, key.crop) == (
                    path, page, rotation, crop
                ):
                    area = key.width * key.height
                    if area > best_area:
                        best, best_area = surface, area
            return best

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    @property
    def nbytes(self) -> int:
        return self._bytes


@dataclass
class DocumentInfo:
    path: str
    page_count: int
    page_sizes: list[tuple[float, float]]   # in PDF points


def _apply_transform(
    ctx: cairo.Context,
    key: RenderKey,
    page_w: float,
    page_h: float,
    scale: float,
) -> None:
    """Map the cropped, rotated page onto a surface of key.width x key.height."""
    if key.rotation == 90:
        ctx.translate(key.width, 0)
        ctx.rotate(math.pi / 2)
    elif key.rotation == 180:
        ctx.translate(key.width, key.height)
        ctx.rotate(math.pi)
    elif key.rotation == 270:
        ctx.translate(0, key.height)
        ctx.rotate(-math.pi / 2)

    ctx.scale(scale, scale)
    # Bring the crop box's top-left corner to the origin.
    ctx.translate(-page_w * key.crop[0], -page_h * key.crop[1])


class RenderService:
    """One worker thread, one priority queue, one cache."""

    def __init__(self, max_bytes: int = 256 * 1024 * 1024):
        self.cache = PageCache(max_bytes)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()
        self._generation = 0
        self._inflight: set[RenderKey] = set()
        self._documents: "OrderedDict[str, object]" = OrderedDict()
        self._info: dict[str, DocumentInfo] = {}
        self._thread = threading.Thread(
            target=self._run, name="leggio-render", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------- lifecycle

    def shutdown(self) -> None:
        self._stop.set()
        self._queue.put((-1, -1, None))

    def invalidate(self) -> int:
        """Drop queued work that has not started (after a resize or rotation)."""
        self._generation += 1
        return self._generation

    # --------------------------------------------------------------- queueing

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def request_info(
        self, path: str, callback: Callable[[Optional[DocumentInfo]], None]
    ) -> None:
        """Page count and per-page sizes, needed on the main thread to lay
        out before anything can be rendered."""
        cached = self._info.get(path)
        if cached is not None:
            GLib.idle_add(callback, cached)
            return
        self._queue.put((
            PRIORITY_INFO, self._next_seq(), ("info", path, callback),
        ))

    def request(
        self,
        key: RenderKey,
        callback: Callable[[RenderKey, cairo.ImageSurface], None],
        priority: int = PRIORITY_VISIBLE,
    ) -> Optional[cairo.ImageSurface]:
        """Return the cached surface, or queue a render and return None."""
        surface = self.cache.get(key)
        if surface is not None:
            return surface
        if key in self._inflight:
            return None
        self._inflight.add(key)
        self._queue.put((
            priority, self._next_seq(),
            ("render", key, callback, self._generation),
        ))
        return None

    # ----------------------------------------------------------------- worker

    def _run(self) -> None:
        while not self._stop.is_set():
            _priority, _seq, job = self._queue.get()
            if job is None:
                break
            try:
                if job[0] == "info":
                    self._do_info(job[1], job[2])
                else:
                    self._do_render(job[1], job[2], job[3])
            except Exception:
                log.exception("render worker error")

    def _document(self, path: str):
        document = self._documents.get(path)
        if document is not None:
            self._documents.move_to_end(path)
            return document
        document = open_document(path)
        self._documents[path] = document
        while len(self._documents) > MAX_OPEN_DOCUMENTS:
            self._documents.popitem(last=False)
        return document

    def _do_info(self, path: str, callback) -> None:
        try:
            document = self._document(path)
        except PdfError as exc:
            log.warning("%s", exc)
            GLib.idle_add(callback, None)
            return
        count = document.get_n_pages()
        sizes = []
        for index in range(count):
            page = document.get_page(index)
            sizes.append(page.get_size() if page is not None else (595.0, 842.0))
        info = DocumentInfo(path, count, sizes)
        self._info[path] = info
        GLib.idle_add(callback, info)

    def _do_render(self, key: RenderKey, callback, generation: int) -> None:
        try:
            if generation != self._generation:
                return  # geometry moved on while this sat in the queue
            if self.cache.get(key) is not None:
                return

            document = self._document(key.path)
            page = document.get_page(key.page)
            if page is None:
                return
            page_w, page_h = page.get_size()
            if page_w <= 0 or page_h <= 0:
                return

            crop = key.crop
            content_w = page_w * (crop[2] - crop[0])
            content_h = page_h * (crop[3] - crop[1])
            if content_w <= 0 or content_h <= 0:
                return

            swapped = key.rotation in (90, 270)
            scale = key.width / (content_h if swapped else content_w)

            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, key.width, key.height
            )
            ctx = cairo.Context(surface)
            # Poppler composites onto whatever is there; sheet music wants
            # paper, not transparency.
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            _apply_transform(ctx, key, page_w, page_h, scale)
            page.render(ctx)
            surface.flush()

            self.cache.put(key, surface)
            GLib.idle_add(callback, key, surface)
        except (PdfError, cairo.Error, MemoryError) as exc:
            log.warning("could not render %s p%d: %s", key.path, key.page, exc)
        finally:
            self._inflight.discard(key)


def make_key(
    path: str,
    page: int,
    width: int,
    height: int,
    rotation: int,
    crop: Optional[CropRect],
) -> RenderKey:
    box = (crop.x0, crop.y0, crop.x1, crop.y1) if crop else (0.0, 0.0, 1.0, 1.0)
    return RenderKey(
        path=path,
        page=page,
        width=max(1, min(int(width), MAX_DIMENSION)),
        height=max(1, min(int(height), MAX_DIMENSION)),
        rotation=rotation % 360,
        crop=box,
    )
