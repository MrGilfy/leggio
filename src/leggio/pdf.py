# SPDX-License-Identifier: GPL-3.0-or-later
"""Poppler access.

Documents are never shared between threads: each worker opens its own
handle. Concurrent use of one document can segfault.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Poppler", "0.18")
from gi.repository import Gio, GLib, Poppler  # noqa: E402

log = logging.getLogger(__name__)

# How much of the file feeds the relink fingerprint. Enough to be unique in
# practice, small enough to stay cheap on a spinning disk or network mount.
HASH_BYTES = 256 * 1024


class PdfError(Exception):
    """The file could not be opened or read as a PDF."""


def open_document(path: Path | str) -> Poppler.Document:
    file = Gio.File.new_for_path(str(path))
    try:
        document = Poppler.Document.new_from_gfile(file, None, None)
    except GLib.Error as exc:
        raise PdfError(f"{path}: {exc.message}") from exc
    if document is None:
        raise PdfError(f"{path}: could not be opened")
    return document


def content_key(path: Path) -> Optional[str]:
    """Fingerprint (size + first 256 KiB) to recognise a file that moved."""
    try:
        size = path.stat().st_size
        digest = hashlib.sha256(str(size).encode("ascii"))
        with path.open("rb") as fh:
            digest.update(fh.read(HASH_BYTES))
        return digest.hexdigest()
    except OSError as exc:
        log.debug("could not fingerprint %s: %s", path, exc)
        return None


_WS = re.compile(r"\s+")


def clean_title(path: Path) -> str:
    """Filename to a human title: drop the extension, underscores to spaces."""
    stem = path.stem.replace("_", " ")
    stem = _WS.sub(" ", stem).strip()
    return stem or path.name


def read_metadata(document: Poppler.Document) -> tuple[int, Optional[str]]:
    """Page count, and composer taken from the PDF author field if present."""
    page_count = document.get_n_pages()
    composer = None
    try:
        author = document.get_author()
    except GLib.Error:
        author = None
    if author:
        author = _WS.sub(" ", author).strip()
        if author:
            composer = author
    return page_count, composer
