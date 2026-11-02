# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite library index.

One connection shared across threads behind a lock, in WAL mode.

The scanner never deletes rows or overwrites user metadata: a file that
disappears is only flagged `missing`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .models import Annotation, CropRect, Score, Setlist

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
  id              INTEGER PRIMARY KEY,
  path            TEXT    NOT NULL UNIQUE,
  title           TEXT    NOT NULL,
  composer        TEXT,
  page_count      INTEGER NOT NULL DEFAULT 0,
  file_size       INTEGER,
  file_mtime      REAL,
  content_key     TEXT,
  added           REAL    NOT NULL,
  last_opened     REAL,
  last_page       INTEGER NOT NULL DEFAULT 0,
  last_offset     REAL    NOT NULL DEFAULT 0.0,
  rotation        INTEGER NOT NULL DEFAULT 0,
  crop_x0         REAL,
  crop_y0         REAL,
  crop_x1         REAL,
  crop_y1         REAL,
  crop_auto       INTEGER NOT NULL DEFAULT 1,
  fit_mode        TEXT,
  spread_override TEXT,
  missing         INTEGER NOT NULL DEFAULT 0,
  title_locked    INTEGER NOT NULL DEFAULT 0,
  composer_locked INTEGER NOT NULL DEFAULT 0,
  night_mode      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scores_title       ON scores(title);
CREATE INDEX IF NOT EXISTS idx_scores_composer    ON scores(composer);
CREATE INDEX IF NOT EXISTS idx_scores_content_key ON scores(content_key);

CREATE TABLE IF NOT EXISTS tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS score_tags (
  score_id INTEGER NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
  PRIMARY KEY (score_id, tag_id)
);

CREATE TABLE IF NOT EXISTS setlists (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  created     REAL NOT NULL,
  last_opened REAL
);

CREATE TABLE IF NOT EXISTS setlist_items (
  id         INTEGER PRIMARY KEY,
  setlist_id INTEGER NOT NULL REFERENCES setlists(id) ON DELETE CASCADE,
  score_id   INTEGER NOT NULL REFERENCES scores(id)   ON DELETE CASCADE,
  position   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_setlist_items ON setlist_items(setlist_id, position);

-- Annotations are vectors in page coordinates (fractions of the uncropped,
-- unrotated page), never pixels. That is the only representation that stays
-- glued to the notation when the crop, rotation, zoom or window size change.
CREATE TABLE IF NOT EXISTS annotations (
  id       INTEGER PRIMARY KEY,
  score_id INTEGER NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  page     INTEGER NOT NULL,
  kind     TEXT    NOT NULL,   -- 'ink' | 'symbol'
  color    TEXT    NOT NULL,   -- #rrggbb or #rrggbbaa
  width    REAL    NOT NULL,   -- stroke width as a fraction of page width
  data     TEXT    NOT NULL,   -- JSON payload, see models.Annotation
  created  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_annotations ON annotations(score_id, page);
"""

# The only columns update_score() may set by name; keeps its dynamic SQL safe.
WRITABLE = frozenset({
    "title", "composer", "last_opened", "last_page", "last_offset",
    "rotation", "crop_x0", "crop_y0", "crop_x1", "crop_y1", "crop_auto",
    "fit_mode", "spread_override", "missing", "title_locked",
    "composer_locked", "page_count", "night_mode",
})

# Columns added after the first release: (name, definition).
ADDED_COLUMNS = (
    ("night_mode", "INTEGER NOT NULL DEFAULT 0"),   # schema 3
)


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        conn = self._conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS leaves an older scores table as it was.
        present = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
        for name, definition in ADDED_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE scores ADD COLUMN {name} {definition}")
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    # ---------------------------------------------------------------- scores

    def _tags_map(self) -> dict[int, tuple[str, ...]]:
        rows = self._conn.execute(
            "SELECT st.score_id, t.name FROM score_tags st "
            "JOIN tags t ON t.id = st.tag_id ORDER BY t.name"
        ).fetchall()
        out: dict[int, list[str]] = {}
        for row in rows:
            out.setdefault(row["score_id"], []).append(row["name"])
        return {k: tuple(v) for k, v in out.items()}

    def all_scores(self) -> list[Score]:
        with self._lock:
            tags = self._tags_map()
            rows = self._conn.execute("SELECT * FROM scores").fetchall()
            return [Score.from_row(r, tags.get(r["id"], ())) for r in rows]

    def get_score(self, score_id: int) -> Optional[Score]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scores WHERE id = ?", (score_id,)
            ).fetchone()
            if row is None:
                return None
            return Score.from_row(row, self._tags_map().get(score_id, ()))

    def update_score(self, score_id: int, **fields) -> None:
        unknown = set(fields) - WRITABLE
        if unknown:
            raise ValueError(f"not writable: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [
            int(v) if isinstance(v, bool) else v for v in fields.values()
        ]
        with self._lock:
            self._conn.execute(
                f"UPDATE scores SET {assignments} WHERE id = ?",
                (*values, score_id),
            )
            self._conn.commit()

    def set_crop(self, score_id: int, crop: Optional[CropRect], auto: bool) -> None:
        if crop is None:
            self.update_score(
                score_id, crop_x0=None, crop_y0=None, crop_x1=None,
                crop_y1=None, crop_auto=auto,
            )
        else:
            self.update_score(
                score_id, crop_x0=crop.x0, crop_y0=crop.y0, crop_x1=crop.x1,
                crop_y1=crop.y1, crop_auto=auto,
            )

    def sync_from_scan(
        self,
        path: str,
        *,
        default_title: str,
        composer: Optional[str],
        page_count: int,
        file_size: int,
        file_mtime: float,
        content_key: Optional[str],
    ) -> tuple[int, bool]:
        """Insert, update or relink one scanned file.

        Returns (score_id, is_new). User-edited fields are never clobbered:
        title and composer are only refreshed while their *_locked flag is 0.
        """
        with self._lock:
            conn = self._conn
            row = conn.execute(
                "SELECT * FROM scores WHERE path = ?", (path,)
            ).fetchone()

            if row is None and content_key:
                # A moved file keeps its row: relink one with the same
                # fingerprint whose old path is gone. Checked on disk, as
                # `missing` is only set after the whole scan.
                candidates = conn.execute(
                    "SELECT * FROM scores WHERE content_key = ? "
                    "ORDER BY missing DESC, last_opened DESC",
                    (content_key,),
                ).fetchall()
                for candidate in candidates:
                    if not Path(candidate["path"]).exists():
                        row = candidate
                        log.info("relinking %r -> %r", row["path"], path)
                        conn.execute(
                            "UPDATE scores SET path = ? WHERE id = ?",
                            (path, row["id"]),
                        )
                        break

            if row is None:
                cursor = conn.execute(
                    "INSERT INTO scores (path, title, composer, page_count, "
                    "file_size, file_mtime, content_key, added, missing) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (path, default_title, composer, page_count, file_size,
                     file_mtime, content_key, time.time()),
                )
                conn.commit()
                return int(cursor.lastrowid), True

            updates = {
                "page_count": page_count,
                "file_size": file_size,
                "file_mtime": file_mtime,
                "content_key": content_key,
                "missing": 0,
            }
            if not row["title_locked"]:
                updates["title"] = default_title
            if not row["composer_locked"] and composer:
                updates["composer"] = composer
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE scores SET {assignments} WHERE id = ?",
                (*updates.values(), row["id"]),
            )
            conn.commit()
            return int(row["id"]), False

    def mark_missing_except(self, seen_ids: Iterable[int]) -> int:
        """Flag every score not seen by this scan. Rows are kept forever;
        the user's metadata outlives the file."""
        seen = list(seen_ids)
        with self._lock:
            if seen:
                placeholders = ",".join("?" * len(seen))
                cursor = self._conn.execute(
                    f"UPDATE scores SET missing = 1 "
                    f"WHERE missing = 0 AND id NOT IN ({placeholders})",
                    seen,
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE scores SET missing = 1 WHERE missing = 0"
                )
            self._conn.commit()
            return cursor.rowcount

    def forget_score(self, score_id: int) -> None:
        """Explicit user action only - removes a row and its links."""
        with self._lock:
            self._conn.execute("DELETE FROM scores WHERE id = ?", (score_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ tags

    def all_tags(self) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.name, COUNT(st.score_id) AS n FROM tags t "
                "LEFT JOIN score_tags st ON st.tag_id = t.id "
                "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
            ).fetchall()
            return [(r["name"], r["n"]) for r in rows]

    def set_tags(self, score_id: int, names: Sequence[str]) -> None:
        cleaned = []
        for name in names:
            name = name.strip()
            if name and name.casefold() not in {c.casefold() for c in cleaned}:
                cleaned.append(name)
        with self._lock:
            conn = self._conn
            conn.execute("DELETE FROM score_tags WHERE score_id = ?", (score_id,))
            for name in cleaned:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
                row = conn.execute(
                    "SELECT id FROM tags WHERE name = ?", (name,)
                ).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO score_tags (score_id, tag_id) VALUES (?, ?)",
                    (score_id, row["id"]),
                )
            # Drop tags that no score references any more.
            conn.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM score_tags)"
            )
            conn.commit()

    # -------------------------------------------------------------- setlists

    def all_setlists(self) -> list[Setlist]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, COUNT(i.id) AS n FROM setlists s "
                "LEFT JOIN setlist_items i ON i.setlist_id = s.id "
                "GROUP BY s.id ORDER BY s.name COLLATE NOCASE"
            ).fetchall()
            return [
                Setlist(r["id"], r["name"], r["created"], r["last_opened"], r["n"])
                for r in rows
            ]

    def create_setlist(self, name: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO setlists (name, created) VALUES (?, ?)",
                (name, time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def rename_setlist(self, setlist_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE setlists SET name = ? WHERE id = ?", (name, setlist_id)
            )
            self._conn.commit()

    def delete_setlist(self, setlist_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM setlists WHERE id = ?", (setlist_id,))
            self._conn.commit()

    def setlist_scores(self, setlist_id: int) -> list[Score]:
        with self._lock:
            tags = self._tags_map()
            rows = self._conn.execute(
                "SELECT sc.* FROM setlist_items i "
                "JOIN scores sc ON sc.id = i.score_id "
                "WHERE i.setlist_id = ? ORDER BY i.position",
                (setlist_id,),
            ).fetchall()
            return [Score.from_row(r, tags.get(r["id"], ())) for r in rows]

    # ----------------------------------------------------------- annotations

    def annotations_for(self, score_id: int, page: int) -> list[Annotation]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM annotations WHERE score_id = ? AND page = ? "
                "ORDER BY id",
                (score_id, page),
            ).fetchall()
            return [Annotation.from_row(r) for r in rows]

    def annotated_pages(self, score_id: int) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT page FROM annotations WHERE score_id = ?",
                (score_id,),
            ).fetchall()
            return {r["page"] for r in rows}

    def add_annotation(self, annotation: Annotation) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO annotations (score_id, page, kind, color, width, "
                "data, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (annotation.score_id, annotation.page, annotation.kind,
                 annotation.color, annotation.width, annotation.encoded_data(),
                 annotation.created),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def delete_annotations(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"DELETE FROM annotations WHERE id IN ({placeholders})", list(ids)
            )
            self._conn.commit()

    def clear_annotations(self, score_id: int, page: int) -> list[Annotation]:
        """Wipe one page, returning what was removed so it can be undone."""
        existing = self.annotations_for(score_id, page)
        self.delete_annotations([a.id for a in existing])
        return existing

    def add_to_setlist(self, setlist_id: int, score_id: int) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS pos "
                "FROM setlist_items WHERE setlist_id = ?",
                (setlist_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO setlist_items (setlist_id, score_id, position) "
                "VALUES (?, ?, ?)",
                (setlist_id, score_id, row["pos"]),
            )
            self._conn.commit()

    def set_setlist_order(self, setlist_id: int, score_ids: Sequence[int]) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM setlist_items WHERE setlist_id = ?", (setlist_id,)
            )
            for position, score_id in enumerate(score_ids):
                self._conn.execute(
                    "INSERT INTO setlist_items (setlist_id, score_id, position) "
                    "VALUES (?, ?, ?)",
                    (setlist_id, score_id, position),
                )
            self._conn.commit()
