# SPDX-License-Identifier: GPL-3.0-or-later
"""Dialogs: metadata editing, setlist assignment, preferences, about."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import APP_ID, __version__
from ..config import Config
from ..db import Database
from ..models import Score
from ..pdf import clean_title


class MetadataDialog(Adw.Dialog):
    """Edit title, composer and tags.

    These live only in the database - the PDF on disk is never modified.
    """

    __gtype_name__ = "LeggioMetadataDialog"

    def __init__(self, db: Database, score: Score, on_saved: Callable[[int], None]):
        super().__init__()
        self.db = db
        self.score = score
        self.on_saved = on_saved

        self.set_title("Edit Score")
        self.set_content_width(460)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)

        header = Adw.HeaderBar()
        header.pack_start(cancel)
        header.pack_end(save)
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        group = Adw.PreferencesGroup()

        self.title_row = Adw.EntryRow(title="Title")
        self.title_row.set_text(score.title)
        group.add(self.title_row)

        reset = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        reset.set_tooltip_text("Reset to file name")
        reset.set_valign(Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.connect(
            "clicked",
            lambda _b: self.title_row.set_text(clean_title(Path(score.path))),
        )
        self.title_row.add_suffix(reset)

        self.composer_row = Adw.EntryRow(title="Composer")
        self.composer_row.set_text(score.composer or "")
        group.add(self.composer_row)

        self.tags_row = Adw.EntryRow(title="Tags (comma separated)")
        self.tags_row.set_text(", ".join(score.tags))
        group.add(self.tags_row)

        info = Adw.ActionRow(title="File")
        info.set_subtitle(score.path)
        info.set_subtitle_selectable(True)
        info.add_css_class("property")
        group.add(info)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(box)
        self.set_child(view)

        self.set_default_widget(save)

    def _on_save(self, _button) -> None:
        default_title = clean_title(Path(self.score.path))
        title = self.title_row.get_text().strip() or default_title
        composer = self.composer_row.get_text().strip() or None

        fields: dict = {"title": title}
        # A title other than the cleaned filename is the user's: lock it
        # against rescans. Typing the filename back in (reset) unlocks it.
        fields["title_locked"] = title != default_title
        if composer != self.score.composer:
            fields["composer"] = composer
            fields["composer_locked"] = True

        self.db.update_score(self.score.id, **fields)

        tags = [t.strip() for t in self.tags_row.get_text().split(",")]
        self.db.set_tags(self.score.id, [t for t in tags if t])

        self.on_saved(self.score.id)
        self.close()


class SetlistDialog(Adw.Dialog):
    """Add a score to an existing setlist, or to a brand new one."""

    __gtype_name__ = "LeggioSetlistDialog"

    def __init__(self, db: Database, score: Score, on_done: Callable[[], None]):
        super().__init__()
        self.db = db
        self.score = score
        self.on_done = on_done

        self.set_title("Add to Setlist")
        self.set_content_width(420)

        header = Adw.HeaderBar()
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda _b: self.close())
        header.pack_start(close)
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        new_group = Adw.PreferencesGroup(title="New setlist")
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.connect("entry-activated", lambda _r: self._create())
        create = Gtk.Button.new_from_icon_name("list-add-symbolic")
        create.set_valign(Gtk.Align.CENTER)
        create.add_css_class("flat")
        create.connect("clicked", lambda _b: self._create())
        self.name_row.add_suffix(create)
        new_group.add(self.name_row)
        box.append(new_group)

        self.existing_group = Adw.PreferencesGroup(title="Existing setlists")
        box.append(self.existing_group)
        self._rows: list[Adw.ActionRow] = []
        self._populate()

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(box)
        self.set_child(view)

    def _populate(self) -> None:
        for row in self._rows:
            self.existing_group.remove(row)
        self._rows.clear()

        setlists = self.db.all_setlists()
        if not setlists:
            row = Adw.ActionRow(title="No setlists yet")
            row.set_sensitive(False)
            self.existing_group.add(row)
            self._rows.append(row)
            return

        for setlist in setlists:
            row = Adw.ActionRow(title=setlist.name)
            row.set_subtitle(
                "1 score" if setlist.item_count == 1 else f"{setlist.item_count} scores"
            )
            row.set_activatable(True)
            row.connect("activated", self._on_pick, setlist.id)
            row.add_suffix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
            self.existing_group.add(row)
            self._rows.append(row)

    def _create(self) -> None:
        name = self.name_row.get_text().strip()
        if not name:
            return
        setlist_id = self.db.create_setlist(name)
        self.db.add_to_setlist(setlist_id, self.score.id)
        self.on_done()
        self.close()

    def _on_pick(self, _row, setlist_id: int) -> None:
        self.db.add_to_setlist(setlist_id, self.score.id)
        self.on_done()
        self.close()


class PreferencesDialog(Adw.PreferencesDialog):
    """Library folders. Viewer settings arrive with the viewer."""

    __gtype_name__ = "LeggioPreferencesDialog"

    def __init__(self, config: Config, on_changed: Callable[[], None]):
        super().__init__()
        self.config = config
        self.on_changed = on_changed

        page = Adw.PreferencesPage(title="Library", icon_name="folder-symbolic")
        self.folder_group = Adw.PreferencesGroup(
            title="Folders",
            description="Scanned recursively for PDF files.",
        )

        add = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add.set_valign(Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.set_tooltip_text("Add folder")
        add.connect("clicked", self._on_add)
        self.folder_group.set_header_suffix(add)

        page.add(self.folder_group)
        self.add(page)

        self._rows: list[Adw.ActionRow] = []
        self._populate()

    def _populate(self) -> None:
        for row in self._rows:
            self.folder_group.remove(row)
        self._rows.clear()

        directories = self.config.directories
        if not directories:
            row = Adw.ActionRow(title="No folders configured")
            row.set_subtitle("Add one to build your library.")
            row.set_sensitive(False)
            self.folder_group.add(row)
            self._rows.append(row)
            return

        for directory in directories:
            row = Adw.ActionRow(title=directory.name or str(directory))
            row.set_subtitle(str(directory))
            if not directory.is_dir():
                row.set_subtitle(f"{directory}  —  not found")
                row.add_css_class("error")
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.set_valign(Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text("Remove folder")
            remove.connect("clicked", self._on_remove, directory)
            row.add_suffix(remove)
            self.folder_group.add(row)
            self._rows.append(row)

    def _on_add(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Add Library Folder")
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return  # user cancelled
        if folder is None:
            return
        path = Path(folder.get_path())
        directories = self.config.directories
        if path not in directories:
            directories.append(path)
            self.config.directories = directories
            self.config.save()
            self._populate()
            self.on_changed()

    def _on_remove(self, _button, directory: Path) -> None:
        directories = [d for d in self.config.directories if d != directory]
        self.config.directories = directories
        self.config.save()
        self._populate()
        self.on_changed()


def show_about(parent: Gtk.Widget) -> None:
    about = Adw.AboutDialog(
        application_name="Leggio",
        application_icon=APP_ID,
        developer_name="Leggio",
        version=__version__,
        comments="A sheet music viewer built for use at arm's length.",
        license_type=Gtk.License.GPL_3_0,
    )
    about.present(parent)
