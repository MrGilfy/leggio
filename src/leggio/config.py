# SPDX-License-Identifier: GPL-3.0-or-later
"""XDG paths and config.toml handling.

Reads with tomllib; writes with the small serialiser below, which covers
flat tables of scalars and string lists.
"""

from __future__ import annotations

import copy
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from .sandbox import in_flatpak

log = logging.getLogger(__name__)

APP_NAME = "leggio"


def _xdg(env: str, fallback: str) -> Path:
    value = os.environ.get(env)
    if value:
        return Path(value).expanduser()
    return Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / APP_NAME

CONFIG_PATH = CONFIG_DIR / "config.toml"
DB_PATH = DATA_DIR / "library.db"
THUMB_DIR = CACHE_DIR / "thumbs"


DEFAULTS: dict[str, dict[str, Any]] = {
    "library": {
        # A Flatpak can only open folders picked in the file chooser, so it
        # starts with none and the library asks for one.
        "directories": [] if in_flatpak() else ["~/Music"],
        "follow_symlinks": False,
    },
    "viewer": {
        "fit_mode": "page",         # page (whole page) | width
        "half_page": False,
        "spread": "auto",           # auto | single | double
        "overlay_timeout": 4.0,
        "tap_zones": True,
        "swipe": True,
    },
    "cache": {
        "max_pixmap_mb": 256,
        "thumb_width": 480,
    },
}


def _fmt(value: Any) -> str:
    # bool before int: bool is a subclass of int and must not print as 0/1.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} to TOML")


def _dump(data: dict[str, dict[str, Any]]) -> str:
    lines = ["# Leggio configuration", ""]
    for section, values in data.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_fmt(value)}")
        lines.append("")
    return "\n".join(lines)


class Config:
    """Merged view of DEFAULTS and the user's config.toml."""

    def __init__(self, data: dict[str, dict[str, Any]], path: Path = CONFIG_PATH):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                with path.open("rb") as fh:
                    user = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                log.warning("could not read %s (%s); using defaults", path, exc)
                user = {}
            for section, values in user.items():
                if isinstance(values, dict):
                    data.setdefault(section, {}).update(values)
        cfg = cls(data, path)
        if not path.exists():
            cfg.save()
        return cfg

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".toml.tmp")
            tmp.write_text(_dump(self.data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.error("could not write %s: %s", self.path, exc)

    def get(self, section: str, key: str, default: Any = None) -> Any:
        fallback = DEFAULTS.get(section, {}).get(key, default)
        return self.data.get(section, {}).get(key, fallback)

    def set(self, section: str, key: str, value: Any) -> None:
        self.data.setdefault(section, {})[key] = value

    @property
    def directories(self) -> list[Path]:
        raw = self.get("library", "directories") or []
        out: list[Path] = []
        for entry in raw:
            try:
                out.append(Path(entry).expanduser())
            except (TypeError, ValueError):
                log.warning("ignoring malformed library directory: %r", entry)
        return out

    @directories.setter
    def directories(self, paths: list[Path]) -> None:
        home = str(Path.home())
        stored = []
        for p in paths:
            text = str(p)
            if text.startswith(home):
                text = "~" + text[len(home):]
            stored.append(text)
        self.set("library", "directories", stored)

    def replace_directory(self, old: Path, new: Path) -> None:
        """Put `new` where `old` was in the list, keeping the order."""
        directories = self.directories
        if new in directories:
            directories = [d for d in directories if d != old]
        elif old in directories:
            directories[directories.index(old)] = new
        else:
            directories.append(new)
        self.directories = directories


def ensure_dirs() -> None:
    for directory in (CONFIG_DIR, DATA_DIR, CACHE_DIR, THUMB_DIR):
        directory.mkdir(parents=True, exist_ok=True)
