# SPDX-License-Identifier: GPL-3.0-or-later
"""Page overview and bookmarks for the viewer.

The overview covers the score inside the viewer, so fullscreen and the idle
inhibitor stay on: a header with large buttons, a row of score buttons in a
setlist, a row of bookmark chips, and a grid of page thumbnails. Thumbnails
are ordinary render-service requests at a low priority, so they respect
crop and rotation and share the page cache.

Bookmarks are added with "Add bookmark here" or K, and jump with the same
landing as jump links. Each chip's pencil button renames or deletes it; a
delete can be undone from the toast.
"""

from __future__ import annotations

import time
from typing import Optional

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from .. import geometry, links
from ..models import Bookmark, Score
from ..render import PRIORITY_THUMBNAIL, make_key

THUMB_WIDTH = 168          # logical px: readable at arm's length
DEFAULT_PAGE = (595.0, 842.0)


def _big_button(label: str, icon: Optional[str] = None) -> Gtk.Button:
    button = Gtk.Button()
    if icon is None:
        button.set_label(label)
    else:
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.append(Gtk.Image.new_from_icon_name(icon))
        content.append(Gtk.Label(label=label))
        button.set_child(content)
    button.add_css_class("overview-button")
    return button


class PageCard(Gtk.FlowBoxChild):
    """One page: thumbnail, bookmark names, page number."""

    __gtype_name__ = "LeggioPageCard"

    def __init__(self, overview: "PageOverview", page: int, width: int,
                 height: int, names: list[str], current: bool):
        super().__init__()
        self.page = page
        self._overview = overview
        self.add_css_class("page-card")
        if current:
            self.add_css_class("current")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        overlay = Gtk.Overlay()
        self.thumb = Gtk.DrawingArea()
        self.thumb.set_content_width(width)
        self.thumb.set_content_height(height)
        self.thumb.add_css_class("page-thumb")
        self.thumb.set_draw_func(self._draw)
        overlay.set_child(self.thumb)
        if names:
            badges = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            badges.set_valign(Gtk.Align.END)
            badges.set_halign(Gtk.Align.START)
            badges.set_margin_start(6)
            badges.set_margin_bottom(6)
            for name in names:
                badge = Gtk.Label(label=name, xalign=0.0)
                badge.set_max_width_chars(16)
                badge.set_ellipsize(Pango.EllipsizeMode.END)
                badge.add_css_class("bookmark-badge")
                badges.append(badge)
            overlay.add_overlay(badges)
        box.append(overlay)

        number = Gtk.Label(label=str(page + 1))
        number.add_css_class("page-number")
        box.append(number)
        self.set_child(box)
        self.set_tooltip_text(f"Go to page {page + 1}")

    def _draw(self, area, ctx, width: int, height: int) -> None:
        self._overview.draw_thumbnail(area, ctx, width, height, self.page)


class PageOverview(Gtk.Box):
    __gtype_name__ = "LeggioPageOverview"

    def __init__(self, viewer):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.viewer = viewer
        self.index = 0                 # which score of the setlist is shown
        self._cards: list[PageCard] = []
        self.add_css_class("overview")
        self.set_hexpand(True)
        self.set_vexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.title = Gtk.Label(xalign=0.0)
        self.title.add_css_class("overview-title")
        self.title.set_hexpand(True)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self.title)
        self.add_button = _big_button("Add bookmark here", "user-bookmarks-symbolic")
        self.add_button.set_tooltip_text("Bookmark the place you are at (K)")
        self.add_button.connect("clicked", lambda _b: viewer.add_bookmark_here())
        header.append(self.add_button)
        close = _big_button("Close")
        close.set_tooltip_text("Back to the score (Esc)")
        close.connect("clicked", lambda _b: viewer.close_overview())
        header.append(close)
        self.append(header)

        # Setlist: one button per score.
        self.scores_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.scores_scroller = self._row_scroller(self.scores_row)
        self.append(self.scores_scroller)

        # Bookmarks: one chip per bookmark, with its own pencil button.
        self.bookmarks_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.append(self._row_scroller(self.bookmarks_row))

        self.grid = Gtk.FlowBox()
        self.grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.grid.set_activate_on_single_click(True)
        self.grid.set_homogeneous(True)
        self.grid.set_min_children_per_line(2)
        self.grid.set_max_children_per_line(12)
        self.grid.set_row_spacing(12)
        self.grid.set_column_spacing(12)
        self.grid.set_valign(Gtk.Align.START)
        self.grid.connect("child-activated", self._on_page_chosen)
        pages = Gtk.ScrolledWindow()
        pages.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pages.set_vexpand(True)
        pages.set_child(self.grid)
        self.pages_scroller = pages
        self.append(pages)

    @staticmethod
    def _row_scroller(row: Gtk.Box) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_child(row)
        return scroller

    # -------------------------------------------------------------- content

    @property
    def score(self) -> Score:
        return self.viewer.scores[self.index]

    def show_score(self, index: int) -> None:
        self.index = index
        viewer = self.viewer
        viewer._ensure_info(index)
        setlist = len(viewer.scores) > 1
        self.title.set_label(viewer.get_title() if setlist else self.score.title)
        self._fill_scores()
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the bookmark chips and the page grid for the shown score."""
        # "Here" is where the score being played is, so it only makes sense
        # while the overview shows that score.
        self.add_button.set_sensitive(self.index == self.viewer.index)
        bookmarks = self.viewer.ctx.db.bookmarks_for(self.score.id)
        self._fill_bookmarks(bookmarks)
        self._fill_pages(bookmarks)

    def _fill_scores(self) -> None:
        self._clear(self.scores_row)
        scores = self.viewer.scores
        self.scores_scroller.set_visible(len(scores) > 1)
        if len(scores) <= 1:
            return
        first: Optional[Gtk.ToggleButton] = None
        for index, score in enumerate(scores):
            button = Gtk.ToggleButton(label=f"{index + 1}. {score.title}")
            button.add_css_class("overview-button")
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(index == self.index)
            button.connect("toggled", self._on_score_toggled, index)
            self.scores_row.append(button)

    def _fill_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        self._clear(self.bookmarks_row)
        if not bookmarks:
            hint = Gtk.Label(
                label="No bookmarks yet. Use “Add bookmark here”, or K while playing."
            )
            hint.add_css_class("overview-hint")
            self.bookmarks_row.append(hint)
            return
        for bookmark in bookmarks:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            chip.add_css_class("bookmark-chip")
            go = Gtk.Button(label=f"{bookmark.name}  ·  p. {bookmark.page + 1}")
            go.add_css_class("chip-go")
            go.set_tooltip_text(f"Go to “{bookmark.name}”")
            go.connect("clicked", self._on_bookmark_chosen, bookmark)
            chip.append(go)
            edit = Gtk.Button.new_from_icon_name("leggio-annotate-symbolic")
            edit.add_css_class("chip-edit")
            edit.set_tooltip_text(f"Rename or delete “{bookmark.name}”")
            edit.connect("clicked", lambda _b, b=bookmark: self.viewer.edit_bookmark(b))
            chip.append(edit)
            self.bookmarks_row.append(chip)

    def _fill_pages(self, bookmarks: list[Bookmark]) -> None:
        self.grid.remove_all()
        self._cards = []
        viewer = self.viewer
        score = self.score
        info = viewer.infos.get(score.path)
        count = info.page_count if info else score.page_count
        sizes = info.page_sizes if info else []
        names: dict[int, list[str]] = {}
        for bookmark in bookmarks:
            names.setdefault(bookmark.page, []).append(bookmark.name)
        current: set[int] = set()
        if self.index == viewer.index:
            # What the last draw showed, unless the position has moved on
            # since and nothing has been drawn yet.
            current = set(viewer._drawn_pages)
            if viewer.page not in current:
                current = {viewer.page}

        for page in range(count):
            size = sizes[page] if page < len(sizes) else DEFAULT_PAGE
            content_w, content_h = geometry.content_size(
                size, score.crop, score.rotation % 360
            )
            height = max(int(round(THUMB_WIDTH * content_h / content_w)), 1)
            card = PageCard(self, page, THUMB_WIDTH, height,
                            names.get(page, []), page in current)
            self.grid.append(card)
            self._cards.append(card)

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    def focus_current(self) -> None:
        for card in self._cards:
            if card.has_css_class("current"):
                card.grab_focus()
                return
        if self._cards:
            self._cards[0].grab_focus()

    # ----------------------------------------------------------- thumbnails

    def draw_thumbnail(self, area, ctx, width: int, height: int, page: int) -> None:
        viewer = self.viewer
        score = self.score
        factor = area.get_scale_factor() or 1
        key = make_key(score.path, page, width * factor, height * factor,
                       score.rotation % 360, score.crop)
        surface = viewer.ctx.render.request(
            key, lambda *_a: area.queue_draw() or False,
            priority=PRIORITY_THUMBNAIL,
        )
        if surface is None:
            surface = viewer.ctx.render.cache.find_similar(
                score.path, page, score.rotation % 360, key.crop
            )
        if surface is None:
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
        else:
            ctx.save()
            ctx.scale(width / surface.get_width(), height / surface.get_height())
            ctx.set_source_surface(surface, 0, 0)
            ctx.get_source().set_filter(cairo.Filter.GOOD)
            ctx.paint()
            ctx.restore()
        if score.night_mode:
            ctx.set_operator(cairo.Operator.DIFFERENCE)
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()

    # ------------------------------------------------------------- choosing

    def _on_score_toggled(self, button: Gtk.ToggleButton, index: int) -> None:
        if button.get_active() and index != self.index:
            self.index = index
            self.viewer._ensure_info(index)
            self.refresh()

    def _on_page_chosen(self, _grid, card: PageCard) -> None:
        self.viewer.close_overview()
        self.viewer.go_to(card.page, index=self.index)

    def _on_bookmark_chosen(self, _button, bookmark: Bookmark) -> None:
        self.viewer.close_overview()
        self.viewer.go_to(bookmark.page, bookmark.x, bookmark.y, index=self.index)


class OverviewMixin:
    """Overview and bookmark actions, mixed into ViewerPage."""

    def _build_overview(self) -> Gtk.Widget:
        self.overview = PageOverview(self)
        self.overview.set_visible(False)
        return self.overview

    @property
    def overview_open(self) -> bool:
        return hasattr(self, "overview") and self.overview.get_visible()

    def open_overview(self) -> None:
        if self.annotate_mode or self.crop_mode or self._info() is None:
            return
        self._hide_overlay()
        self.overview.show_score(self.index)
        self.overview.set_visible(True)
        self.overview.focus_current()

    def close_overview(self) -> None:
        if not self.overview_open:
            return
        self.overview.set_visible(False)
        self.area.grab_focus()

    def toggle_overview(self) -> None:
        if self.overview_open:
            self.close_overview()
        else:
            self.open_overview()

    # ------------------------------------------------------------ bookmarks

    def _bookmark_here(self) -> Bookmark:
        x, y = links.top_point(self.offset, self.rotation, self.crop)
        return Bookmark(id=0, score_id=self.score.id, name="", page=self.page,
                        x=round(x, 5), y=round(y, 5), created=time.time())

    def add_bookmark_here(self) -> None:
        """Ask for a name, then bookmark where the viewer is now."""
        if self._info() is None:
            return
        bookmark = self._bookmark_here()
        dialog = Adw.AlertDialog(heading="Add Bookmark",
                                 body=f"Page {bookmark.page + 1}")
        entry = Gtk.Entry()
        entry.set_text(f"Page {bookmark.page + 1}")
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")
        dialog.connect("response", self._on_bookmark_named, entry, bookmark)
        dialog.present(self)
        entry.grab_focus()

    def _on_bookmark_named(self, _dialog, response, entry, bookmark) -> None:
        name = entry.get_text().strip()
        if response != "add" or not name:
            return
        bookmark.name = name
        bookmark.id = self.ctx.db.add_bookmark(bookmark)
        self._after_bookmarks_changed()
        self._short_toast(f"Bookmarked “{name}”")

    def edit_bookmark(self, bookmark: Bookmark) -> None:
        dialog = Adw.AlertDialog(heading="Bookmark",
                                 body=f"Page {bookmark.page + 1}")
        entry = Gtk.Entry()
        entry.set_text(bookmark.name)
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("delete", "Delete")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.connect("response", self._on_bookmark_edited, entry, bookmark)
        dialog.present(self)

    def _on_bookmark_edited(self, _dialog, response, entry, bookmark) -> None:
        if response == "rename":
            name = entry.get_text().strip()
            if name and name != bookmark.name:
                self.ctx.db.rename_bookmark(bookmark.id, name)
                self._after_bookmarks_changed()
        elif response == "delete":
            self.delete_bookmark(bookmark)

    def delete_bookmark(self, bookmark: Bookmark) -> None:
        removed = self.ctx.db.delete_bookmark(bookmark.id)
        if removed is None:
            return
        self._after_bookmarks_changed()
        toast = self.ctx.toast(f"Deleted “{removed.name}”")
        # Shown at once, not queued behind another toast: Undo is only
        # useful right after the delete.
        toast.set_priority(Adw.ToastPriority.HIGH)
        toast.set_button_label("Undo")
        toast.connect("button-clicked", lambda _t: self._restore_bookmark(removed))
        self.undo_toast = toast

    def _restore_bookmark(self, bookmark: Bookmark) -> None:
        bookmark.id = self.ctx.db.add_bookmark(bookmark)
        self._after_bookmarks_changed()

    def _after_bookmarks_changed(self) -> None:
        if self.overview_open:
            self.overview.refresh()

    def _short_toast(self, message: str) -> None:
        toast = self.ctx.toast(message)
        toast.set_timeout(2)
