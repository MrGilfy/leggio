# SPDX-License-Identifier: GPL-3.0-or-later
"""Exporting from the UI: the options, the save dialog, a worker thread
and the toasts that report on it.

One ExportController per window, shared by the viewer, the library and the
setlist page, so only one export runs at a time. The save dialog is a
Gtk.FileDialog, which goes through the save portal inside Flatpak: that is
the only way to write outside the sandbox, since home is read-only there.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..config import CACHE_DIR
from ..export import (
    PRINTED_KINDS,
    ExportCancelled,
    ExportError,
    ExportScore,
    PdfError,
    export_pdf,
    is_source,
    suggested_name,
)
from ..models import Score

log = logging.getLogger(__name__)

EXPORT_TEMP = CACHE_DIR / "export"


class ExportController:
    def __init__(self, ctx):
        self.ctx = ctx
        self._cancel: Optional[threading.Event] = None
        self._progress: Optional[Adw.Toast] = None
        # Left behind only if Leggio was killed mid-export.
        for stale in EXPORT_TEMP.glob("leggio-export-*.pdf"):
            stale.unlink(missing_ok=True)

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    @property
    def running(self) -> bool:
        return self._cancel is not None

    # ---------------------------------------------------------- the dialogs

    def start(self, parent: Gtk.Widget, scores: list[Score], title: str,
              setlist: bool = False) -> None:
        """Ask for the options, then for a file, then export."""
        if self.running:
            self.ctx.toast("An export is already running")
            return
        scores = [s for s in scores if not s.missing and Path(s.path).exists()]
        if not scores:
            self.ctx.toast("There is nothing to export: the PDF is missing")
            return

        body = (f"{len(scores)} scores, in setlist order, as one PDF"
                if setlist else "A new PDF. The original is not changed.")
        dialog = Adw.AlertDialog(heading=f"Export “{title}”", body=body)
        rows = Gtk.ListBox()
        rows.add_css_class("boxed-list")
        rows.set_selection_mode(Gtk.SelectionMode.NONE)
        annotations = Adw.SwitchRow(title="Include annotations", active=True)
        crop = Adw.SwitchRow(title="Apply crop", active=False)
        crop.set_subtitle("Leave out the margins you cropped in Leggio")
        rows.append(annotations)
        rows.append(crop)
        dialog.set_extra_child(rows)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("choose", "Choose File…")
        dialog.set_response_appearance("choose", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("choose")
        dialog.connect(
            "response", self._on_options, parent, scores, title, setlist,
            annotations, crop,
        )
        dialog.present(parent)
        self.options_dialog = dialog

    def _on_options(self, _dialog, response, parent, scores, title, setlist,
                    annotations_row, crop_row) -> None:
        if response != "choose":
            return
        include = annotations_row.get_active()
        apply_crop = crop_row.get_active()

        chooser = Gtk.FileDialog()
        chooser.set_title("Export PDF")
        chooser.set_modal(True)
        chooser.set_initial_name(suggested_name(title, annotated=include,
                                                setlist=setlist))
        folder = Path(scores[0].path).parent
        if folder.is_dir():
            chooser.set_initial_folder(Gio.File.new_for_path(str(folder)))
        pdfs = Gtk.FileFilter()
        pdfs.set_name("PDF documents")
        pdfs.add_mime_type("application/pdf")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pdfs)
        chooser.set_filters(filters)
        chooser.set_default_filter(pdfs)
        chooser.save(parent.get_root(), None, self._on_file_chosen,
                     scores, title, include, apply_crop)

    def _on_file_chosen(self, chooser, result, scores, title, include,
                        apply_crop) -> None:
        try:
            file = chooser.save_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                log.warning("save dialog failed: %s", exc.message)
                self.ctx.toast(f"Could not export: {exc.message}")
            return
        if file is None:
            return
        path = file.get_path()
        if path is None:
            self.ctx.toast("Choose a file in a local folder")
            return
        self.export(scores, Path(path), title, include, apply_crop)

    # ------------------------------------------------------------- export

    def collect(self, scores: list[Score], include: bool,
                apply_crop: bool) -> list[ExportScore]:
        """Read everything the worker needs from the database, here on the
        main thread."""
        db = self.ctx.db
        out = []
        for score in scores:
            annotations = {}
            if include:
                for page in sorted(db.annotated_pages(score.id)):
                    printed = [a for a in db.annotations_for(score.id, page)
                               if a.kind in PRINTED_KINDS]
                    if printed:
                        annotations[page] = printed
            out.append(ExportScore(
                path=score.path, title=score.title, rotation=score.rotation,
                crop=score.crop if apply_crop else None,
                annotations=annotations,
            ))
        return out

    def export(self, scores: list[Score], destination: Path, title: str,
               include: bool = True, apply_crop: bool = False) -> None:
        """Export on a worker thread. Returns at once; toasts report back."""
        if self.running:
            self.ctx.toast("An export is already running")
            return
        if is_source(destination, [s.path for s in scores]):
            self.ctx.toast("That is the original PDF. Choose another name.")
            return
        jobs = self.collect(scores, include, apply_crop)
        cancel = threading.Event()
        self._cancel = cancel

        toast = Adw.Toast(title=f"Exporting “{title}”…", timeout=0)
        toast.set_button_label("Cancel")
        toast.connect("button-clicked", lambda _t: cancel.set())
        self.ctx.window.toasts.add_toast(toast)
        self._progress = toast

        def progress(done: int, total: int) -> None:
            GLib.idle_add(self._on_progress, title, done, total)

        def work() -> None:
            try:
                export_pdf(jobs, destination, title=title, progress=progress,
                           cancel=cancel, temp_dir=EXPORT_TEMP)
            except ExportCancelled:
                GLib.idle_add(self._on_finished, destination, "Export cancelled")
            except (ExportError, PdfError) as exc:
                GLib.idle_add(self._on_finished, destination, str(exc))
            except Exception as exc:   # noqa: BLE001 - never die silently
                log.exception("export failed")
                GLib.idle_add(self._on_finished, destination,
                              f"Export failed: {exc}")
            else:
                GLib.idle_add(self._on_finished, destination, None)

        threading.Thread(target=work, name="leggio-export", daemon=True).start()

    def _on_progress(self, title: str, done: int, total: int) -> bool:
        if self._progress is not None:
            self._progress.set_title(f"Exporting “{title}”… page {done} of {total}")
        return False

    def _on_finished(self, destination: Path, error: Optional[str]) -> bool:
        if self._progress is not None:
            self._progress.dismiss()
        self._progress = None
        self._cancel = None
        if error is not None:
            self.ctx.toast(error)
            return False
        toast = Adw.Toast(title=f"Saved “{destination.name}”", timeout=6)
        toast.set_button_label("Open")
        toast.connect("button-clicked", lambda _t: self._open(destination))
        self.ctx.window.toasts.add_toast(toast)
        self.done_toast = toast
        return False

    def hide_progress(self) -> None:
        """Keep a running export's toast off a score that just opened."""
        if self._progress is not None:
            self._progress.dismiss()
            self._progress = None

    def _open(self, destination: Path) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(destination)))
        launcher.launch(self.ctx.window, None, self._on_launched)

    @staticmethod
    def _on_launched(launcher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            log.warning("could not open the export: %s", exc.message)
