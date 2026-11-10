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
from .models import Score
from .pdf import PdfError, clean_title, content_key, open_document, read_metadata

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    found: int = 0
    added: int = 0
    updated: int = 0
    missing: int = 0
    unreadable: list[str] = field(default_factory=list)
    # Library folders that could not be opened, each with the number of
    # scores the library already holds from it. Those scores are kept as
    # they were rather than flagged missing.
    unreachable: dict[Path, int] = field(default_factory=dict)
    cancelled: bool = False


def is_reachable(root: Path) -> bool:
    """Whether a library folder can be listed right now.

    False for a folder that was deleted or moved, a drive that is not
    plugged in, and, inside a Flatpak, a folder Leggio was never given
    access to or whose access was revoked.
    """
    try:
        return root.is_dir() and os.access(root, os.R_OK | os.X_OK)
    except OSError:
        return False


def is_under(path: str, root: Path) -> bool:
    prefix = str(root).rstrip("/") + "/"
    return path.startswith(prefix)


def find_pdfs(directories: Iterable[Path], follow_symlinks: bool = False) -> list[Path]:
    """Every .pdf under the given roots, de-duplicated by resolved path."""
    seen: set[Path] = set()
    out: list[Path] = []
    for root in directories:
        if not is_reachable(root):
            log.info("skipping library folder %s: it cannot be opened", root)
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


def find_same_file(path: Path, scores: Iterable[Score]) -> Optional[Score]:
    """The library's entry for this file under another path, if it has one.

    Inside a Flatpak, a PDF opened from the file manager arrives under a
    document portal path of its own, even when it sits in a library folder
    that Leggio already sees under a different one. Matching by content
    keeps its annotations and reading position instead of adding a second
    copy.
    """
    key = content_key(path)
    if key is None:
        return None
    for score in scores:
        if (score.content_key == key and not score.missing
                and score.path != str(path) and Path(score.path).is_file()):
            return score
    return None


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
            result = self.scan(directories, follow_symlinks, on_progress)
        except Exception:
            log.exception("scan failed")
        finally:
            if on_finished:
                GLib.idle_add(on_finished, result)

    def scan(self, directories, follow_symlinks=False, on_progress=None) -> ScanResult:
        """One full pass; runs on the calling thread."""
        result = ScanResult()
        paths = find_pdfs(directories, follow_symlinks)
        result.found = len(paths)

        scores = self.db.all_scores()
        known = {score.path: score for score in scores}
        seen_ids: list[int] = []

        # A folder that cannot be opened says nothing about whether its
        # files still exist, so its scores count as seen.
        for root in directories:
            if is_reachable(root):
                continue
            held = [s.id for s in scores if is_under(s.path, root)]
            result.unreachable[root] = len(held)
            seen_ids.extend(held)
            if held:
                log.warning(
                    "cannot open library folder %s; keeping its %d scores",
                    root, len(held),
                )

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
                library_roots=directories,
            )
            seen_ids.append(score_id)
            if is_new:
                result.added += 1
            else:
                result.updated += 1

        if not result.cancelled:
            # Anything not seen this pass is flagged, never deleted.
            result.missing = self.db.mark_missing_except(seen_ids)
        return result
