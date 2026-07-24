# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime dependency check.

Leggio's dependencies are system libraries reached through GObject
introspection, not things pip can install. When one is missing the raw
failure is an ImportError or a ValueError from gi.require_version, neither
of which tells anyone which package to install. This module turns that into
a straight answer.

Imported before anything touches gi, so it must not import gi itself at
module level.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

# (what it is, Arch package, Fedora package, Debian/Ubuntu package)
_PACKAGES = {
    "PyGObject": ("python-gobject", "python3-gobject", "python3-gi"),
    "pycairo": ("python-cairo", "python3-cairo", "python3-gi-cairo"),
    "GTK 4": ("gtk4", "gtk4", "gir1.2-gtk-4.0"),
    "libadwaita": ("libadwaita", "libadwaita", "gir1.2-adw-1"),
    "poppler-glib": ("poppler-glib", "poppler-glib", "gir1.2-poppler-0.18"),
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


# Ordered so the most fundamental missing piece is reported first: without
# PyGObject every later probe would fail too, and naming all five would be
# noise.
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
    arch, fedora, debian = _PACKAGES[missing]
    return "\n".join([
        f"Leggio cannot start: {missing} is not available.",
        "",
        "Install it with:",
        f"  Arch, Manjaro     sudo pacman -S --needed {arch}",
        f"  Fedora            sudo dnf install {fedora}",
        f"  Debian, Ubuntu    sudo apt install {debian}",
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
