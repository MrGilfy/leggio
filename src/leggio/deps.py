# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime dependency check.

Turns the ImportError or gi.require_version ValueError of a missing system
library into the name of the package to install. Must not import gi at
module level.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

# What it is -> (Arch, Fedora, Debian/Ubuntu, openSUSE) package.
_PACKAGES = {
    "PyGObject": (
        "python-gobject", "python3-gobject", "python3-gi", "python3-gobject",
    ),
    "pycairo": (
        "python-cairo", "python3-cairo", "python3-gi-cairo",
        "python3-gobject-cairo",
    ),
    "GTK 4": ("gtk4", "gtk4", "gir1.2-gtk-4.0", "typelib-1_0-Gtk-4_0"),
    "libadwaita": (
        "libadwaita", "libadwaita", "gir1.2-adw-1", "typelib-1_0-Adw-1",
    ),
    "poppler-glib": (
        "poppler-glib", "poppler-glib", "gir1.2-poppler-0.18",
        "typelib-1_0-Poppler-0_18",
    ),
}


def _probe_pygobject() -> bool:
    import gi  # noqa: F401
    return True


def _probe_pycairo() -> bool:
    import cairo  # noqa: F401
    return True


def _probe_namespace(namespace: str, version: str) -> Callable[[], bool]:
    def probe() -> bool:
        import gi

        gi.require_version(namespace, version)
        __import__("gi.repository", fromlist=[namespace])
        return True

    return probe


# Most fundamental first: without PyGObject every later probe fails too.
_CHECKS: list[tuple[str, Callable[[], bool]]] = [
    ("PyGObject", _probe_pygobject),
    ("pycairo", _probe_pycairo),
    ("GTK 4", _probe_namespace("Gtk", "4.0")),
    ("libadwaita", _probe_namespace("Adw", "1")),
    ("poppler-glib", _probe_namespace("Poppler", "0.18")),
]


def first_missing() -> Optional[str]:
    """Name of the first dependency that cannot be loaded, or None."""
    for name, probe in _CHECKS:
        try:
            probe()
        except (ImportError, ValueError, AttributeError):
            return name
    return None


def format_message(missing: str) -> str:
    arch, fedora, debian, opensuse = _PACKAGES[missing]
    return "\n".join([
        f"Leggio cannot start: {missing} is not available.",
        "",
        "Install it with:",
        f"  Arch, Manjaro     sudo pacman -S --needed {arch}",
        f"  Fedora            sudo dnf install {fedora}",
        f"  Debian, Ubuntu    sudo apt install {debian}",
        f"  openSUSE          sudo zypper install {opensuse}",
        "",
        "Leggio needs PyGObject, pycairo, GTK 4, libadwaita and",
        "poppler-glib. See the README for the full list.",
    ])


def ensure_dependencies() -> bool:
    """True if everything loads; otherwise explain and return False."""
    missing = first_missing()
    if missing is None:
        return True
    print(format_message(missing), file=sys.stderr)
    return False
