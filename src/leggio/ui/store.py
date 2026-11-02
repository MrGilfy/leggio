# SPDX-License-Identifier: GPL-3.0-or-later
"""GObject wrappers and the list model plumbing for the library grid."""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GObject, Gtk  # noqa: E402

from ..models import Score

SORT_KEYS = ("title", "composer", "added", "last_opened")
SORT_LABELS = ("Title", "Composer", "Date added", "Last opened")


class ScoreObject(GObject.Object):
    """GObject wrapper for a Score; `changed` lets a recycled card refresh."""

    __gtype_name__ = "LeggioScoreObject"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, score: Score):
        super().__init__()
        self.score = score
        self.thumb: Optional[str] = None

    @property
    def id(self) -> int:
        return self.score.id

    def replace(self, score: Score) -> None:
        self.score = score
        self.emit("changed")

    def set_thumb(self, path: str) -> None:
        self.thumb = path
        self.emit("changed")

    def search_text(self) -> str:
        parts = [self.score.title, self.score.composer or ""]
        parts.extend(self.score.tags)
        return " ".join(parts).casefold()


class LibraryStore:
    """Base list + filter + sort, exposed as one model for the GridView."""

    def __init__(self):
        self.base = Gio.ListStore.new(ScoreObject)
        self._query = ""
        self._mode = "all"          # all | recent | missing | tag
        self._tag: Optional[str] = None
        self._sort_key = "title"
        self._sort_reverse = False

        self.filter = Gtk.CustomFilter.new(self._match)
        self.filtered = Gtk.FilterListModel.new(self.base, self.filter)
        self.sorter = Gtk.CustomSorter.new(self._compare)
        self.sorted = Gtk.SortListModel.new(self.filtered, self.sorter)
        self.selection = Gtk.NoSelection.new(self.sorted)

        self._by_id: dict[int, ScoreObject] = {}

    # ------------------------------------------------------------- contents

    def load(self, scores: list[Score]) -> None:
        self.base.remove_all()
        self._by_id.clear()
        objects = []
        for score in scores:
            obj = ScoreObject(score)
            self._by_id[score.id] = obj
            objects.append(obj)
        if objects:
            self.base.splice(0, 0, objects)
        self._refilter()

    def get(self, score_id: int) -> Optional[ScoreObject]:
        return self._by_id.get(score_id)

    def update(self, score: Score) -> None:
        obj = self._by_id.get(score.id)
        if obj is not None:
            obj.replace(score)
            self._refilter()
            self._resort()

    @property
    def n_total(self) -> int:
        return self.base.get_n_items()

    @property
    def n_visible(self) -> int:
        return self.sorted.get_n_items()

    # -------------------------------------------------------------- filters

    def set_query(self, text: str) -> None:
        query = text.strip().casefold()
        if query != self._query:
            self._query = query
            self._refilter()

    def set_mode(self, mode: str, tag: Optional[str] = None) -> None:
        if (mode, tag) != (self._mode, self._tag):
            self._mode = mode
            self._tag = tag
            self._refilter()

    def _refilter(self) -> None:
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    # GTK passes a trailing user_data; failing on it would silently leave
    # the model in insertion order.
    def _match(self, obj: ScoreObject, *_user_data) -> bool:
        score = obj.score
        if self._mode == "missing":
            if not score.missing:
                return False
        elif self._mode == "recent":
            if score.last_opened is None:
                return False
        elif self._mode == "tag":
            folded = {t.casefold() for t in score.tags}
            if (self._tag or "").casefold() not in folded:
                return False

        if not self._query:
            return True
        haystack = obj.search_text()
        # Every whitespace-separated term must appear somewhere, so
        # "bach prel" narrows the way you would expect.
        return all(term in haystack for term in self._query.split())

    # ----------------------------------------------------------------- sort

    def set_sort(self, key: str, reverse: Optional[bool] = None) -> None:
        if key not in SORT_KEYS:
            return
        self._sort_key = key
        if reverse is None:
            # Text ascending, dates newest first: both are the useful default.
            reverse = key in ("added", "last_opened")
        self._sort_reverse = reverse
        self._resort()

    def _resort(self) -> None:
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _key(self, obj: ScoreObject):
        score = obj.score
        if self._sort_key == "title":
            return (score.title.casefold(),)
        if self._sort_key == "composer":
            # Unknown composers sort last in either direction.
            composer = (score.composer or "").casefold()
            return (composer == "", composer, score.title.casefold())
        if self._sort_key == "added":
            return (score.added or 0.0, score.title.casefold())
        return (score.last_opened or 0.0, score.title.casefold())

    def _compare(self, a: ScoreObject, b: ScoreObject, *_user_data) -> int:
        ka, kb = self._key(a), self._key(b)
        if ka == kb:
            return 0
        result = -1 if ka < kb else 1
        return -result if self._sort_reverse else result
