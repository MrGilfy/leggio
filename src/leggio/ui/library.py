# SPDX-License-Identifier: GPL-3.0-or-later
"""The library screen: sidebar, search, sort, grid of score cards."""

from __future__ import annotations

import logging
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GObject, Gtk  # noqa: E402

from ..models import CropRect, Score
from .dialogs import MetadataDialog, SetlistDialog
from .score_card import ScoreCard
from .setlist_page import SetlistPage
from .store import SORT_KEYS, SORT_LABELS, LibraryStore, ScoreObject

log = logging.getLogger(__name__)


class LibraryPage(Adw.NavigationPage):
    __gtype_name__ = "LeggioLibraryPage"

    def __init__(self, ctx):
        super().__init__(title="Leggio")
        self.ctx = ctx
        self.store = LibraryStore()
        self._last_scan_toast: Optional[Adw.Toast] = None
        self._context_popover: Optional[Gtk.PopoverMenu] = None
        self._context_score: Optional[Score] = None
        self._setlist_popover: Optional[Gtk.PopoverMenu] = None
        self._context_setlist: Optional[tuple[int, str]] = None

        self._build_actions()

        self.split = Adw.OverlaySplitView()
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self._build_content())
        self.split.set_max_sidebar_width(260)
        self.set_child(self.split)

        self.reload()

    # ------------------------------------------------------------- actions

    def _build_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("open", self._act_open),
            ("edit", self._act_edit),
            ("setlist", self._act_setlist),
            ("reveal", self._act_reveal),
            ("forget", self._act_forget),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
        self.insert_action_group("score", group)

        setlists = Gio.SimpleActionGroup()
        for name, handler in (
            ("open", self._act_setlist_open),
            ("rename", self._act_setlist_rename),
            ("delete", self._act_setlist_delete),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            setlists.add_action(action)
        self.insert_action_group("setlists", setlists)

    # ------------------------------------------------------------- sidebar

    def _build_sidebar(self) -> Gtk.Widget:
        self.views_list = Gtk.ListBox()
        self.views_list.add_css_class("navigation-sidebar")
        for title, icon in (
            ("All Scores", "view-grid-symbolic"),
            ("Recently Opened", "document-open-recent-symbolic"),
            ("Missing Files", "dialog-warning-symbolic"),
        ):
            row = Adw.ActionRow(title=title)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.views_list.append(row)
        # Shown only once something is missing. Hidden, not removed, so the
        # row index _on_view_selected maps to a filter stays put.
        self.missing_row = self.views_list.get_row_at_index(2)
        self.missing_row.set_visible(False)
        self.views_list.connect("row-selected", self._on_view_selected)

        self.tags_list = Gtk.ListBox()
        self.tags_list.add_css_class("navigation-sidebar")
        self.tags_list.connect("row-selected", self._on_tag_selected)

        self.setlists_list = Gtk.ListBox()
        self.setlists_list.add_css_class("navigation-sidebar")
        self.setlists_list.connect("row-activated", self._on_setlist_activated)
        self.setlists_list.set_selection_mode(Gtk.SelectionMode.NONE)

        # Deleting a setlist used to mean opening it first and finding a
        # menu inside; put it where the setlist actually is.
        setlist_menu = Gtk.GestureClick.new()
        setlist_menu.set_button(Gdk.BUTTON_SECONDARY)
        setlist_menu.connect("pressed", self._on_setlist_secondary)
        self.setlists_list.add_controller(setlist_menu)

        setlist_press = Gtk.GestureLongPress.new()
        setlist_press.set_touch_only(True)
        setlist_press.connect("pressed", self._on_setlist_long_press)
        self.setlists_list.add_controller(setlist_press)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.views_list)

        self.tags_heading = Gtk.Label(label="TAGS", xalign=0.0)
        self.tags_heading.add_css_class("sidebar-heading")
        box.append(self.tags_heading)
        box.append(self.tags_list)

        self.setlists_heading = Gtk.Label(label="SETLISTS", xalign=0.0)
        self.setlists_heading.add_css_class("sidebar-heading")
        box.append(self.setlists_heading)
        box.append(self.setlists_list)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(box)
        scroller.set_vexpand(True)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Gtk.Label(label="Library"))

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroller)
        return view

    # ------------------------------------------------------------- content

    def _build_content(self) -> Gtk.Widget:
        self.sidebar_toggle = Gtk.ToggleButton()
        self.sidebar_toggle.set_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle.set_active(True)
        self.sidebar_toggle.set_tooltip_text("Toggle sidebar")
        self.sidebar_toggle.bind_property(
            "active", self.split, "show-sidebar",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search title, composer or tag")
        self.search.set_max_width_chars(34)
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search_changed)

        self.sort_dropdown = Gtk.DropDown.new_from_strings(list(SORT_LABELS))
        self.sort_dropdown.set_tooltip_text("Sort by")
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)

        self.sort_reverse = Gtk.ToggleButton()
        self.sort_reverse.set_icon_name("view-sort-descending-symbolic")
        self.sort_reverse.set_tooltip_text("Reverse order")
        self.sort_reverse.connect("toggled", self._on_sort_changed)

        menu = Gio.Menu()
        menu.append("Rescan Library", "app.rescan")
        menu.append("Preferences", "app.preferences")
        menu.append("About Leggio", "app.about")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(menu)

        header = Adw.HeaderBar()
        header.pack_start(self.sidebar_toggle)
        header.set_title_widget(self.search)
        header.pack_end(menu_button)
        header.pack_end(self.sort_reverse)
        header.pack_end(self.sort_dropdown)

        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("osd")
        self.progress_revealer = Gtk.Revealer()
        self.progress_revealer.set_child(self.progress)
        self.progress_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", self._on_unbind)

        self.grid = Gtk.GridView(model=self.store.selection, factory=factory)
        self.grid.set_max_columns(12)
        self.grid.set_min_columns(1)
        # Single click activates: a touchscreen has no double-tap idiom.
        self.grid.set_single_click_activate(True)
        self.grid.connect("activate", self._on_activate)
        self.grid.set_margin_start(12)
        self.grid.set_margin_end(12)
        self.grid.set_margin_top(12)
        self.grid.set_margin_bottom(12)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.grid)
        scroller.set_vexpand(True)

        empty = Adw.StatusPage(
            icon_name="folder-symbolic",
            title="No Scores Yet",
            description="Add a folder of PDFs to build your library.",
        )
        add_button = Gtk.Button(label="Add Folder…")
        add_button.add_css_class("suggested-action")
        add_button.add_css_class("pill")
        add_button.set_halign(Gtk.Align.CENTER)
        add_button.set_action_name("app.preferences")
        empty.set_child(add_button)

        no_results = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="No Matches",
            description="Try a different search or filter.",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "grid")
        self.stack.add_named(empty, "empty")
        self.stack.add_named(no_results, "no-results")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.progress_revealer)
        view.set_content(self.stack)
        return view

    # --------------------------------------------------------- grid factory

    def _on_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(ScoreCard(on_context=self._on_card_context))

    def _on_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        card = list_item.get_child()
        obj = list_item.get_item()
        card.bind(obj)
        # Thumbnails are requested as cards bind, so scrolling drives the work.
        if obj.thumb is None and not obj.score.missing:
            self.ctx.thumbs.request(obj.score, self._on_thumb_ready)

    def _on_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        list_item.get_child().unbind()

    def _on_thumb_ready(
        self, score_id: int, path: str, crop: Optional[CropRect]
    ) -> None:
        obj = self.store.get(score_id)
        if obj is None:
            return
        if crop is not None:
            obj.score.crop = crop
        obj.set_thumb(path)

    # ------------------------------------------------------------ filtering

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self.store.set_query(entry.get_text())
        self._update_stack()

    def _on_sort_changed(self, *_args) -> None:
        index = self.sort_dropdown.get_selected()
        if index == Gtk.INVALID_LIST_POSITION:
            return
        key = SORT_KEYS[index]
        default_reverse = key in ("added", "last_opened")
        self.store.set_sort(key, default_reverse != self.sort_reverse.get_active())

    def _on_view_selected(self, _listbox, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is None:
            return
        self.tags_list.select_row(None)
        self.store.set_mode(("all", "recent", "missing")[row.get_index()])
        self._update_stack()

    def _on_tag_selected(self, _listbox, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is None:
            return
        self.views_list.select_row(None)
        self.store.set_mode("tag", row.get_title())
        self._update_stack()

    def _on_setlist_secondary(self, gesture, _n, x, y) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_setlist_menu(x, y)

    def _on_setlist_long_press(self, gesture, x, y) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_setlist_menu(x, y)

    def _show_setlist_menu(self, x: float, y: float) -> None:
        row = self.setlists_list.get_row_at_y(int(y))
        setlist_id = getattr(row, "setlist_id", None) if row else None
        if setlist_id is None:
            return
        self._context_setlist = (setlist_id, row.get_title())

        menu = Gio.Menu()
        menu.append("Open", "setlists.open")
        menu.append("Rename…", "setlists.rename")
        section = Gio.Menu()
        section.append("Delete Setlist", "setlists.delete")
        menu.append_section(None, section)

        if self._setlist_popover is not None:
            self._setlist_popover.unparent()
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self.setlists_list)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        self._setlist_popover = popover
        popover.popup()

    def _act_setlist_open(self, *_args) -> None:
        if self._context_setlist:
            page = SetlistPage(self.ctx, self._context_setlist[0],
                               on_changed=self.reload)
            self.ctx.navigation.push(page)

    def _act_setlist_rename(self, *_args) -> None:
        if not self._context_setlist:
            return
        setlist_id, name = self._context_setlist
        dialog = Adw.AlertDialog(heading="Rename Setlist")
        entry = Gtk.Entry()
        entry.set_text(name)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.connect("response", self._on_setlist_renamed, entry, setlist_id)
        dialog.present(self)

    def _on_setlist_renamed(self, _dialog, response, entry, setlist_id) -> None:
        if response == "rename" and entry.get_text().strip():
            self.ctx.db.rename_setlist(setlist_id, entry.get_text().strip())
            self._populate_setlists()

    def _act_setlist_delete(self, *_args) -> None:
        if not self._context_setlist:
            return
        setlist_id, name = self._context_setlist
        dialog = Adw.AlertDialog(
            heading="Delete Setlist?",
            body=f"“{name}” will be removed. The scores themselves stay in "
                 "your library.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_setlist_deleted, setlist_id)
        dialog.present(self)

    def _on_setlist_deleted(self, _dialog, response, setlist_id) -> None:
        if response == "delete":
            self.ctx.db.delete_setlist(setlist_id)
            self._populate_setlists()
            self.ctx.toast("Setlist deleted")

    def _on_setlist_activated(self, _listbox, row: Gtk.ListBoxRow) -> None:
        setlist_id = getattr(row, "setlist_id", None)
        if setlist_id is None:
            return
        page = SetlistPage(self.ctx, setlist_id, on_changed=self.reload)
        self.ctx.navigation.push(page)

    def _update_stack(self) -> None:
        if self.store.n_total == 0:
            self.stack.set_visible_child_name("empty")
        elif self.store.n_visible == 0:
            self.stack.set_visible_child_name("no-results")
        else:
            self.stack.set_visible_child_name("grid")

    # --------------------------------------------------------------- reload

    def reload(self) -> None:
        scores = self.ctx.db.all_scores()
        self.store.load(scores)
        missing = any(score.missing for score in scores)
        self.missing_row.set_visible(missing)
        if not missing and self.views_list.get_selected_row() is self.missing_row:
            self.views_list.select_row(self.views_list.get_row_at_index(0))
        self._on_sort_changed()
        self._populate_tags()
        self._populate_setlists()
        if self.views_list.get_selected_row() is None:
            first = self.views_list.get_row_at_index(0)
            if first is not None:
                self.views_list.select_row(first)
        self._update_stack()

    def _populate_tags(self) -> None:
        while (row := self.tags_list.get_row_at_index(0)) is not None:
            self.tags_list.remove(row)
        tags = self.ctx.db.all_tags()
        self.tags_heading.set_visible(bool(tags))
        self.tags_list.set_visible(bool(tags))
        for name, count in tags:
            row = Adw.ActionRow(title=name)
            row.add_prefix(Gtk.Image.new_from_icon_name("user-bookmarks-symbolic"))
            badge = Gtk.Label(label=str(count))
            badge.add_css_class("dim-label")
            row.add_suffix(badge)
            self.tags_list.append(row)

    def _populate_setlists(self) -> None:
        while (row := self.setlists_list.get_row_at_index(0)) is not None:
            self.setlists_list.remove(row)
        setlists = self.ctx.db.all_setlists()
        # Appears only once you have made one, from a card's context menu.
        self.setlists_heading.set_visible(bool(setlists))
        self.setlists_list.set_visible(bool(setlists))
        for setlist in setlists:
            row = Adw.ActionRow(title=setlist.name)
            row.set_subtitle(
                "1 score" if setlist.item_count == 1
                else f"{setlist.item_count} scores"
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
            row.set_activatable(True)
            row.setlist_id = setlist.id
            self.setlists_list.append(row)

    # -------------------------------------------------------------- context

    def _on_card_context(
        self, card: ScoreCard, obj: ScoreObject, x: float, y: float
    ) -> None:
        self._context_score = obj.score

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Open", "score.open")
        section.append("Edit Details…", "score.edit")
        menu.append_section(None, section)

        section = Gio.Menu()
        section.append("Add to Setlist…", "score.setlist")
        section.append("Show in Files", "score.reveal")
        menu.append_section(None, section)

        if obj.score.missing:
            section = Gio.Menu()
            section.append("Remove from Library", "score.forget")
            menu.append_section(None, section)

        if self._context_popover is not None:
            self._context_popover.unparent()
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(card)
        popover.set_has_arrow(True)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        self._context_popover = popover
        popover.popup()

    def _act_open(self, *_args) -> None:
        if self._context_score:
            self.ctx.open_score(self._context_score)

    def _act_edit(self, *_args) -> None:
        if not self._context_score:
            return
        dialog = MetadataDialog(
            self.ctx.db, self._context_score, on_saved=self._on_score_saved
        )
        dialog.present(self)

    def _act_setlist(self, *_args) -> None:
        if not self._context_score:
            return
        dialog = SetlistDialog(
            self.ctx.db, self._context_score, on_done=self._populate_setlists
        )
        dialog.present(self)

    def _act_reveal(self, *_args) -> None:
        if not self._context_score:
            return
        launcher = Gtk.FileLauncher.new(
            Gio.File.new_for_path(self._context_score.path)
        )
        launcher.open_containing_folder(self.ctx.window, None, None)

    def _act_forget(self, *_args) -> None:
        if not self._context_score:
            return
        score = self._context_score
        dialog = Adw.AlertDialog(
            heading="Remove from Library?",
            body=(
                f"“{score.title}” and its tags, crop and reading position "
                "will be forgotten. The file itself is not touched."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_forget_response, score.id)
        dialog.present(self)

    def _on_forget_response(self, _dialog, response: str, score_id: int) -> None:
        if response == "remove":
            self.ctx.db.forget_score(score_id)
            self.reload()

    def _on_score_saved(self, score_id: int) -> None:
        score = self.ctx.db.get_score(score_id)
        if score is not None:
            self.store.update(score)
        self._populate_tags()
        self._update_stack()

    def _on_activate(self, _grid, position: int) -> None:
        obj = self.store.sorted.get_item(position)
        if obj is not None:
            self.ctx.open_score(obj.score)

    # ----------------------------------------------------------------- scan

    def start_scan(self) -> None:
        directories = self.ctx.config.directories
        if not directories:
            self._scan_toast("No library folders configured")
            return
        started = self.ctx.scanner.start(
            directories,
            follow_symlinks=self.ctx.config.get("library", "follow_symlinks", False),
            on_progress=self._on_scan_progress,
            on_finished=self._on_scan_finished,
        )
        if started:
            self.progress.set_fraction(0.0)
            self.progress_revealer.set_reveal_child(True)
        else:
            self._scan_toast("A scan is already running")

    def _on_scan_progress(self, done: int, total: int, name: str) -> None:
        if total:
            self.progress.set_fraction(done / total)
        self.progress.set_text(name)

    def _on_scan_finished(self, result) -> None:
        self.progress_revealer.set_reveal_child(False)
        self.reload()

        if result.cancelled:
            self._scan_toast("Scan cancelled")
            return

        parts = []
        if result.added:
            parts.append(f"{result.added} added")
        if result.updated:
            parts.append(f"{result.updated} updated")
        if result.missing:
            parts.append(f"{result.missing} now missing")
        if result.unreadable:
            parts.append(f"{len(result.unreadable)} unreadable")
        if not parts:
            parts.append(f"{result.found} scores, nothing changed")
        self._scan_toast("Scan complete — " + ", ".join(parts))

    def _scan_toast(self, message: str) -> None:
        # Scans run in the background; their news never covers a score.
        if self.ctx.viewer_open:
            return
        self.dismiss_scan_toast()
        self._last_scan_toast = self.ctx.toast(message)

    def dismiss_scan_toast(self) -> None:
        if self._last_scan_toast is not None:
            self._last_scan_toast.dismiss()
            self._last_scan_toast = None
