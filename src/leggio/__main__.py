# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point: python -m leggio"""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
