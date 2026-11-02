# SPDX-License-Identifier: GPL-3.0-or-later
"""Recursive PDF discovery, on a worker thread.

Files whose size and mtime match the database are skipped unopened, which
keeps rescans of a large library fast.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from gi.repository import GLib

from .db import Database
from .pdf import PdfError, clean_title, content_key, open_document, read_metadata

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    found: int = 0
    added: int = 0
    updated: int = 0
    missing: int = 0
    unreadable: list[str] = field(default_factory=list)
    cancelled: bool = False


def find_pdfs(directories: Iterable[Path], follow_symlinks: bool = False) -> list[Path]:
    """Every .pdf under the given roots, de-duplicated by resolved path."""
    seen: set[Path] = set()
    out: list[Path] = []
    for root in directories:
        if not root.is_dir():
            log.info("skipping missing library directory %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            # Don't descend into dotted directories - no .git, no trash.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if not name.lower().endswith(".pdf") or name.startswith("."):
                    continue
                path = Path(dirpath) / name
                try:
                    key = path.resolve()
                except OSError:
                    key = path
                if key in seen:
                    continue
                seen.add(key)
                out.append(path)
    return out


def index_file(db: Database, path: Path) -> Optional[int]:
    """Add or refresh one PDF opened from outside the library; returns its id."""
    try:
        stat = path.stat()
        document = open_document(path)
        page_count, composer = read_metadata(document)
    except (OSError, PdfError) as exc:
        log.warning("cannot index %s: %s", path, exc)
        return None
    finally:
        document = None

    score_id, _is_new = db.sync_from_scan(
        str(path),
        default_title=clean_title(path),
        composer=composer,
        page_count=page_count,
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
        content_key=content_key(path),
    )
    return score_id


class Scanner:
    """One scan at a time on a worker thread; callbacks run on the main loop."""

    def __init__(self, db: Database):
        self.db = db
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()

    def start(
        self,
        directories: list[Path],
        *,
        follow_symlinks: bool = False,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        on_finished: Optional[Callable[[ScanResult], None]] = None,
    ) -> bool:
        if self.running:
            return False
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(directories, follow_symlinks, on_progress, on_finished),
            name="leggio-scanner",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, directories, follow_symlinks, on_progress, on_finished) -> None:
        result = ScanResult()
        try:
            paths = find_pdfs(directories, follow_symlinks)
            result.found = len(paths)

            known = {
                score.path: score
                for score in self.db.all_scores()
            }
            seen_ids: list[int] = []

            for index, path in enumerate(paths):
                if self._cancel.is_set():
                    result.cancelled = True
                    break

                if on_progress:
                    GLib.idle_add(on_progress, index, len(paths), path.name)

                try:
                    stat = path.stat()
                except OSError as exc:
                    log.warning("cannot stat %s: %s", path, exc)
                    result.unreadable.append(str(path))
                    continue

                existing = known.get(str(path))
                unchanged = (
                    existing is not None
                    and not existing.missing
                    and existing.file_size == stat.st_size
                    and existing.file_mtime == stat.st_mtime
                    and existing.page_count > 0
                )
                if unchanged:
                    seen_ids.append(existing.id)
                    continue

                try:
                    document = open_document(path)
                    page_count, composer = read_metadata(document)
                except PdfError as exc:
                    log.warning("unreadable PDF %s: %s", path, exc)
                    result.unreadable.append(str(path))
                    continue
                finally:
                    document = None  # release the handle on this thread

                score_id, is_new = self.db.sync_from_scan(
                    str(path),
                    default_title=clean_title(path),
                    composer=composer,
                    page_count=page_count,
                    file_size=stat.st_size,
                    file_mtime=stat.st_mtime,
                    content_key=content_key(path),
                )
                seen_ids.append(score_id)
                if is_new:
                    result.added += 1
                else:
                    result.updated += 1

            if not result.cancelled:
                # Anything not seen this pass is flagged, never deleted.
                result.missing = self.db.mark_missing_except(seen_ids)

        except Exception:
            log.exception("scan failed")
        finally:
            if on_finished:
                GLib.idle_add(on_finished, result)
