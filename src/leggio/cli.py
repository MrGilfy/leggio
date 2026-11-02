# SPDX-License-Identifier: GPL-3.0-or-later
"""Console entry point.

Checks the system dependencies before anything imports gi, so a missing GTK
gives an explanation instead of a traceback.
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
