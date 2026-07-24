# SPDX-License-Identifier: GPL-3.0-or-later
"""Application shell: window, navigation, shared services."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from . import APP_ID
from .config import DB_PATH, Config, ensure_dirs
from .db import Database
from .inhibit import IdleInhibitor
from .models import Score
from .render import RenderService
from .scanner import Scanner, index_file
from .thumbs import ThumbnailWorker
from .ui.dialogs import PreferencesDialog, show_about
from .ui.library import LibraryPage
from .ui.viewer import ViewerPage

log = logging.getLogger(__name__)

STYLE_PATH = Path(__file__).parent / "ui" / "style.css"


class AppContext:
    """Everything the pages need, in one object, so views don't reach into
    the application singleton."""

    def __init__(self, window: "LeggioWindow"):
        self.window = window

    @property
    def db(self) -> Database:
        return self.window.db

    @property
    def config(self) -> Config:
        return self.window.config

    @property
    def scanner(self) -> Scanner:
        return self.window.scanner

    @property
    def thumbs(self) -> ThumbnailWorker:
        return self.window.thumbs

    @property
    def navigation(self) -> Adw.NavigationView:
        return self.window.navigation

    @property
    def render(self) -> RenderService:
        return self.window.render

    @property
    def inhibitor(self) -> IdleInhibitor:
        return self.window.inhibitor

    def toast(self, message: str) -> None:
        self.window.toast(message)

    def open_score(self, score: Score) -> None:
        self.window.open_score(score)

    def open_path(self, path: Path) -> None:
        """Open a PDF given on the command line or by a file manager."""
        try:
            path = path.expanduser().resolve()
        except OSError:
            pass
        if not path.is_file():
            self.toast(f"File not found: {path}")
            return

        score = next(
            (s for s in self.db.all_scores() if s.path == str(path)), None
        )
        if score is None:
            # Not in a scanned folder: index it anyway so it behaves like
            # any other score, reading position included.
            score_id = index_file(self.db, path)
            if score_id is None:
                self.toast(f"Could not open {path.name}")
                return
            score = self.db.get_score(score_id)
            self.library.reload()
        if score is not None:
            self.open_score(score)

    def open_setlist(self, setlist_id: int, scores: list[Score]) -> None:
        self.window.open_setlist(setlist_id, scores)


class LeggioWindow(Adw.ApplicationWindow):
    __gtype_name__ = "LeggioWindow"

    def __init__(self, application: "LeggioApp"):
        super().__init__(application=application)
        self.set_title("Leggio")
        self.set_default_size(1100, 760)

        self.config = application.config
        self.db = application.db
        self.scanner = Scanner(self.db)
        self.thumbs = ThumbnailWorker(
            self.db, width=int(self.config.get("cache", "thumb_width", 480))
        )
        self.render = RenderService(
            max_bytes=int(self.config.get("cache", "max_pixmap_mb", 256))
            * 1024 * 1024
        )
        self.inhibitor = IdleInhibitor(application)

        self.ctx = AppContext(self)

        self.navigation = Adw.NavigationView()
        self.library = LibraryPage(self.ctx)
        self.navigation.add(self.library)

        self.navigation.connect("popped", self._on_popped)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.navigation)
        self.set_content(self.toasts)

        self.connect("close-request", self._on_close)

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def _on_popped(self, _view, page) -> None:
        # Returning from the viewer: last-opened and any crop or rotation
        # change need to reach the library's model.
        if isinstance(page, ViewerPage):
            self.library.reload()

    def open_score(self, score: Score) -> None:
        if score.missing:
            self.toast(f"File not found: {score.path}")
            return
        if not Path(score.path).exists():
            # Flagged only on the next scan, but do not pretend it opened.
            self.db.update_score(score.id, missing=True)
            self.library.reload()
            self.toast(f"File not found: {score.path}")
            return
        self.navigation.push(ViewerPage(self.ctx, [score], score.title))

    def open_path(self, path: Path) -> None:
        """Open a PDF given on the command line or by a file manager."""
        try:
            path = path.expanduser().resolve()
        except OSError:
            pass
        if not path.is_file():
            self.toast(f"File not found: {path}")
            return

        score = next(
            (s for s in self.db.all_scores() if s.path == str(path)), None
        )
        if score is None:
            # Not in a scanned folder: index it anyway so it behaves like
            # any other score, reading position included.
            score_id = index_file(self.db, path)
            if score_id is None:
                self.toast(f"Could not open {path.name}")
                return
            score = self.db.get_score(score_id)
            self.library.reload()
        if score is not None:
            self.open_score(score)

    def open_setlist(self, setlist_id: int, scores: list[Score]) -> None:
        setlist = next(
            (s for s in self.db.all_setlists() if s.id == setlist_id), None
        )
        name = setlist.name if setlist else "Setlist"
        self.navigation.push(ViewerPage(self.ctx, scores, name))

    def _on_close(self, *_args) -> bool:
        self.scanner.cancel()
        self.thumbs.stop()
        self.render.shutdown()
        self.inhibitor.uninhibit()
        return False


class LeggioApp(Adw.Application):
    __gtype_name__ = "LeggioApp"

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            # Registered as a PDF handler in the desktop entry, so it has
            # to actually accept files rather than ignoring the argument.
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.config: Config
        self.db: Database
        self.window: LeggioWindow | None = None

        for name, handler, accels in (
            ("rescan", self._on_rescan, ["<Control>r", "F5"]),
            ("preferences", self._on_preferences, ["<Control>comma"]),
            ("about", self._on_about, []),
            ("quit", self._on_quit, ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        ensure_dirs()
        self.config = Config.load()
        self.db = Database(DB_PATH)
        self._load_style()

    def do_activate(self) -> None:
        if self.window is None:
            self.window = LeggioWindow(self)
            # Scan on first show: the library should be current without the
            # user having to ask for it.
            self.window.library.start_scan()
        self.window.present()

    def do_open(self, files, _n_files, _hint) -> None:
        self.do_activate()
        if self.window is None:
            return
        for file in files:
            path = file.get_path()
            if path:
                self.window.open_path(Path(path))
                break   # one window, one score

    def do_shutdown(self) -> None:
        try:
            self.db.close()
        except Exception:
            log.exception("error closing database")
        Adw.Application.do_shutdown(self)

    def _load_style(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        try:
            provider = Gtk.CssProvider()
            provider.load_from_string(STYLE_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            log.warning("could not load stylesheet: %s", exc)
            return
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _on_rescan(self, *_args) -> None:
        if self.window:
            self.window.library.start_scan()

    def _on_preferences(self, *_args) -> None:
        if not self.window:
            return
        dialog = PreferencesDialog(
            self.config, on_changed=self.window.library.start_scan
        )
        dialog.present(self.window)

    def _on_about(self, *_args) -> None:
        if self.window:
            show_about(self.window)

    def _on_quit(self, *_args) -> None:
        self.quit()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return LeggioApp().run(argv if argv is not None else sys.argv)
