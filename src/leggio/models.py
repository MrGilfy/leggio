# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain data types shared between the database, workers and the UI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class CropRect:
    """A crop expressed as fractions of the page, so it survives any
    resolution or rotation change."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def is_sane(self) -> bool:
        return (
            0.0 <= self.x0 < self.x1 <= 1.0
            and 0.0 <= self.y0 < self.y1 <= 1.0
            and self.area >= 0.05
        )

    def clamped(self) -> "CropRect":
        x0, x1 = sorted((min(max(self.x0, 0.0), 1.0), min(max(self.x1, 0.0), 1.0)))
        y0, y1 = sorted((min(max(self.y0, 0.0), 1.0), min(max(self.y1, 0.0), 1.0)))
        return CropRect(x0, y0, x1, y1)

    def union(self, other: "CropRect") -> "CropRect":
        return CropRect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )


FULL_PAGE = CropRect(0.0, 0.0, 1.0, 1.0)


@dataclass
class Score:
    id: int
    path: str
    title: str
    composer: Optional[str] = None
    page_count: int = 0
    file_size: Optional[int] = None
    file_mtime: Optional[float] = None
    content_key: Optional[str] = None
    added: float = 0.0
    last_opened: Optional[float] = None
    last_page: int = 0
    last_offset: float = 0.0
    rotation: int = 0
    crop: Optional[CropRect] = None
    crop_auto: bool = True
    fit_mode: Optional[str] = None
    spread_override: Optional[str] = None
    missing: bool = False
    title_locked: bool = False
    composer_locked: bool = False
    night_mode: bool = False
    tags: tuple[str, ...] = ()

    @property
    def file(self) -> Path:
        return Path(self.path)

    @classmethod
    def from_row(cls, row: Any, tags: tuple[str, ...] = ()) -> "Score":
        crop = None
        if row["crop_x0"] is not None:
            crop = CropRect(
                row["crop_x0"], row["crop_y0"], row["crop_x1"], row["crop_y1"]
            )
        return cls(
            id=row["id"],
            path=row["path"],
            title=row["title"],
            composer=row["composer"],
            page_count=row["page_count"],
            file_size=row["file_size"],
            file_mtime=row["file_mtime"],
            content_key=row["content_key"],
            added=row["added"],
            last_opened=row["last_opened"],
            last_page=row["last_page"],
            last_offset=row["last_offset"],
            rotation=row["rotation"],
            crop=crop,
            crop_auto=bool(row["crop_auto"]),
            fit_mode=row["fit_mode"],
            spread_override=row["spread_override"],
            missing=bool(row["missing"]),
            title_locked=bool(row["title_locked"]),
            composer_locked=bool(row["composer_locked"]),
            night_mode=bool(row["night_mode"]),
            tags=tags,
        )


@dataclass
class Annotation:
    """One pen stroke or one stamped symbol.

    Geometry lives in page space: fractions of the *uncropped, unrotated*
    page, the same space crop rectangles use. Everything the viewer does to
    the page - crop, rotate, zoom, resize, two-page spread - is a transform
    applied at draw time, so an annotation never drifts off the note it was
    put against.

    `data` is:
      ink     {"points": [[x, y], ...]}
      symbol  {"glyph": "fermata", "x": .., "y": .., "size": ..}
    """

    id: int
    score_id: int
    page: int
    kind: str
    color: str
    width: float
    data: dict
    created: float = 0.0

    INK = "ink"
    SYMBOL = "symbol"

    def encoded_data(self) -> str:
        return json.dumps(self.data, separators=(",", ":"))

    @property
    def points(self) -> list[tuple[float, float]]:
        return [(p[0], p[1]) for p in self.data.get("points", ())]

    @classmethod
    def from_row(cls, row: Any) -> "Annotation":
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            data = {}
        return cls(
            id=row["id"],
            score_id=row["score_id"],
            page=row["page"],
            kind=row["kind"],
            color=row["color"],
            width=row["width"],
            data=data,
            created=row["created"],
        )


@dataclass
class Setlist:
    id: int
    name: str
    created: float = 0.0
    last_opened: Optional[float] = None
    item_count: int = 0
