# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless start-up check.

Builds the whole UI and shuts it down again. Catches the class of breakage
that only shows up at widget-construction time - a renamed GTK method, a
missing icon, a signal handler with the wrong signature - which importing
the modules alone would not.

Needs a display; CI runs it under xvfb.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# Sandbox the XDG dirs before importing anything that reads them, so the
# check never touches a real library.
_sandbox = Path(tempfile.mkdtemp(prefix="leggio-smoke-"))
for variable, name in (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
):
    os.environ[variable] = str(_sandbox / name)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from leggio import APP_ID, __version__  # noqa: E402
from leggio.deps import first_missing  # noqa: E402

missing = first_missing()
if missing is not None:
    print(f"dependency missing: {missing}", file=sys.stderr)
    raise SystemExit(1)

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from leggio.app import LeggioApp  # noqa: E402
from leggio.ui.library import LibraryPage  # noqa: E402

failures: list[str] = []

_UI = Path(__file__).resolve().parent.parent / "src" / "leggio" / "ui"
BUNDLED_ICONS = sorted({
    name
    for source in _UI.glob("*.py")
    for name in re.findall(r"leggio-[a-z0-9-]+-symbolic", source.read_text())
})


def check_icons() -> None:
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if not BUNDLED_ICONS:
        failures.append("no bundled icon names found in src/leggio/ui")
    for name in BUNDLED_ICONS:
        if not theme.has_icon(name):
            failures.append(f"bundled icon not found: {name}")


def inspect() -> bool:
    window = app.window
    try:
        if window is None:
            failures.append("no window was created")
            return False
        page = app.window.navigation.get_visible_page()
        if not isinstance(page, LibraryPage):
            failures.append(f"expected the library, got {type(page).__name__}")
        if window.get_title() != "Leggio":
            failures.append(f"unexpected window title {window.get_title()!r}")
        print(f"leggio {__version__} ({APP_ID})")
        print(f"  window:    {window.get_title()}")
        print(f"  page:      {type(page).__name__}")
        print(f"  scores:    {window.library.store.n_total}")
        check_icons()
        print(f"  icons:     {len(BUNDLED_ICONS)} bundled")
    except Exception as exc:                      # noqa: BLE001
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        app.quit()
    return False


app = LeggioApp()
app.connect("activate", lambda *_: GLib.timeout_add(1200, inspect))
status = app.run([])

if failures:
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    raise SystemExit(1)
if status != 0:
    print(f"FAIL: application exited with {status}", file=sys.stderr)
    raise SystemExit(1)
print("smoke test passed")
