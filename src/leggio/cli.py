# SPDX-License-Identifier: GPL-3.0-or-later
"""Console entry point.

Deliberately thin, and deliberately the thing `leggio` in PATH points at:
it checks the system dependencies *before* importing anything that touches
gi, so a missing GTK produces an explanation rather than a traceback.
"""

from __future__ import annotations

import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    from .deps import ensure_dependencies

    if not ensure_dependencies():
        return 1

    from .app import main as run_app

    return run_app(argv)


if __name__ == "__main__":
    sys.exit(main())
