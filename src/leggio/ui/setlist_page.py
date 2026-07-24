# SPDX-License-Identifier: GPL-3.0-or-later
"""Setlist detail: an ordered list of scores.

Order is the whole point - in the viewer a setlist pages through
end-to-end as though it were a single document - so this screen is about
getting the sequence right.
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402


class SetlistPage(Adw.NavigationPage):
    __gtype_name__ = "LeggioSetlistPage"

    def __init__(self, ctx, setlist_id: int, on_changed: Callable[[], None]):
        super().__init__(title="Setlist")
        self.ctx = ctx
        self.setlist_id = setlist_id
        self.on_changed = on_changed
        self._scores: list = []

        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("boxed-list")
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_valign(Gtk.Align.START)

        clamp = Adw.Clamp()
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(18)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        clamp.set_child(self.list_box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(clamp)
        scroller.set_vexpand(True)

        self.empty = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="Empty Setlist",
            description="Add scores from the library with a long press or right click.",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.empty, "empty")

        header = Adw.HeaderBar()

        play = Gtk.Button()
        play.set_child(Adw.ButtonContent(
            icon_name="media-playback-start-symbolic", label="Open"
        ))
        play.add_css_class("suggested-action")
        play.connect("clicked", self._on_open)
        header.pack_end(play)

        menu = Gio.Menu()
        menu.append("Rename…", "setlist.rename")
        menu.append("Delete Setlist", "setlist.delete")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("rename", self._on_rename),
            ("delete", self._on_delete),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
        self.insert_action_group("setlist", group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.set_child(view)

        self.reload()

    # ---------------------------------------------------------------- state

    def _setlist(self):
        for setlist in self.ctx.db.all_setlists():
            if setlist.id == self.setlist_id:
                return setlist
        return None

    def reload(self) -> None:
        setlist = self._setlist()
        if setlist is None:
            self.ctx.navigation.pop()
            return
        self.set_title(setlist.name)

        while (row := self.list_box.get_row_at_index(0)) is not None:
            self.list_box.remove(row)

        self._scores = self.ctx.db.setlist_scores(self.setlist_id)
        self.stack.set_visible_child_name("list" if self._scores else "empty")

        total_pages = sum(s.page_count for s in self._scores)
        for index, score in enumerate(self._scores):
            row = Adw.ActionRow(title=f"{index + 1}.  {score.title}")
            subtitle = score.composer or "Unknown composer"
            if score.missing:
                subtitle = f"{subtitle}  —  file missing"
                row.add_css_class("error")
            row.set_subtitle(subtitle)

            up = Gtk.Button.new_from_icon_name("go-up-symbolic")
            up.add_css_class("flat")
            up.set_valign(Gtk.Align.CENTER)
            up.set_sensitive(index > 0)
            up.set_tooltip_text("Move up")
            up.connect("clicked", self._on_move, index, -1)
            row.add_suffix(up)

            down = Gtk.Button.new_from_icon_name("go-down-symbolic")
            down.add_css_class("flat")
            down.set_valign(Gtk.Align.CENTER)
            down.set_sensitive(index < len(self._scores) - 1)
            down.set_tooltip_text("Move down")
            down.connect("clicked", self._on_move, index, 1)
            row.add_suffix(down)

            remove = Gtk.Button.new_from_icon_name("list-remove-symbolic")
            remove.add_css_class("flat")
            remove.set_valign(Gtk.Align.CENTER)
            remove.set_tooltip_text("Remove from setlist")
            remove.connect("clicked", self._on_remove, index)
            row.add_suffix(remove)

            self.list_box.append(row)

        if self._scores:
            self.empty.set_description(f"{total_pages} pages in total")

    # -------------------------------------------------------------- actions

    def _save_order(self) -> None:
        self.ctx.db.set_setlist_order(
            self.setlist_id, [s.id for s in self._scores]
        )
        self.reload()
        self.on_changed()

    def _on_move(self, _button, index: int, delta: int) -> None:
        target = index + delta
        if not (0 <= target < len(self._scores)):
            return
        self._scores[index], self._scores[target] = (
            self._scores[target], self._scores[index]
        )
        self._save_order()

    def _on_remove(self, _button, index: int) -> None:
        del self._scores[index]
        self._save_order()

    def _on_open(self, _button) -> None:
        playable = [s for s in self._scores if not s.missing]
        if not playable:
            self.ctx.toast("Nothing to open — every file is missing")
            return
        self.ctx.open_setlist(self.setlist_id, playable)

    def _on_rename(self, *_args) -> None:
        setlist = self._setlist()
        if setlist is None:
            return
        dialog = Adw.AlertDialog(heading="Rename Setlist")
        entry = Gtk.Entry()
        entry.set_text(setlist.name)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.connect("response", self._on_rename_response, entry)
        dialog.present(self)

    def _on_rename_response(self, _dialog, response: str, entry: Gtk.Entry) -> None:
        if response != "rename":
            return
        name = entry.get_text().strip()
        if name:
            self.ctx.db.rename_setlist(self.setlist_id, name)
            self.reload()
            self.on_changed()

    def _on_delete(self, *_args) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete Setlist?",
            body="The scores themselves stay in your library.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_response)
        dialog.present(self)

    def _on_delete_response(self, _dialog, response: str) -> None:
        if response != "delete":
            return
        self.ctx.db.delete_setlist(self.setlist_id)
        self.on_changed()
        self.ctx.navigation.pop()
