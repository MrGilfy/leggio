# SPDX-License-Identifier: GPL-3.0-or-later
"""The Flatpak sandbox, and the paths the document portal hands out.

Inside a Flatpak, Leggio has no access to the user's files except what
the portals grant. A folder picked in the file chooser, or a PDF opened
from the file manager, arrives as a path under /run/user/<uid>/doc/. The
grant is kept by the document portal, so the same path keeps working
after Leggio restarts.

These paths are meaningless to the user, so anything shown on screen
goes through display_path(), which asks the document portal for the
real location.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FLATPAK_INFO = Path("/.flatpak-info")

# Set by xdg-document-portal on every file and folder it exports.
HOST_PATH_XATTR = "user.document-portal.host-path"


def in_flatpak() -> bool:
    return FLATPAK_INFO.exists()


def host_path(path: os.PathLike | str) -> Optional[str]:
    """Where a document portal path really is, or None for any other path."""
    try:
        raw = os.getxattr(path, HOST_PATH_XATTR)
    except OSError:
        return None
    return os.fsdecode(raw.rstrip(b"\0")) or None


def display_path(path: os.PathLike | str) -> str:
    """A path as the user knows it, with their home folder shortened to ~."""
    text = host_path(path) or str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text
