# SPDX-License-Identifier: GPL-3.0-or-later
"""Install hints printed when a system library is missing."""

from __future__ import annotations

import pytest

from leggio import deps


@pytest.mark.parametrize("name", [name for name, _probe in deps._CHECKS])
def test_every_dependency_has_a_hint_for_each_distribution(name):
    message = deps.format_message(name)
    for command in ("pacman -S", "dnf install", "apt install", "zypper install"):
        assert command in message
    packages = deps._PACKAGES[name]
    assert len(packages) == 4 and all(packages)
