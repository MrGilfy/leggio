# SPDX-License-Identifier: GPL-3.0-or-later
"""Scanning library folders, including folders that can't be opened.

Inside the Flatpak, Leggio sees only folders picked through the file
chooser portal. A folder from before that, or one whose access was taken
away, must not quietly turn the library into missing files.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

cairo = pytest.importorskip("cairo")
pytest.importorskip("gi")

from leggio import sandbox  # noqa: E402
from leggio.config import Config  # noqa: E402
from leggio.db import Database  # noqa: E402
from leggio.models import Annotation, Bookmark  # noqa: E402
from leggio.scanner import (  # noqa: E402
    Scanner,
    find_same_file,
    is_reachable,
    is_under,
)


def make_pdf(path: Path, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    surface = cairo.PDFSurface(str(path), 200, 300)
    ctx = cairo.Context(surface)
    ctx.move_to(20, 40)
    ctx.show_text(label)
    ctx.show_page()
    surface.finish()
    return path


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "library.db")
    yield database
    database.close()


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "Music"
    make_pdf(root / "Bach" / "Aria.pdf", "Aria")
    make_pdf(root / "Sonata.pdf", "Sonata")
    return root


def scan(db, *roots):
    return Scanner(db).scan(list(roots))


def test_scan_adds_every_pdf(db, music):
    result = scan(db, music)
    assert (result.found, result.added, result.unreachable) == (2, 2, {})
    assert {Path(s.path).name for s in db.all_scores()} == {"Aria.pdf", "Sonata.pdf"}


def test_unreachable_folder_keeps_its_scores(db, music, tmp_path):
    scan(db, music)
    hidden = tmp_path / "hidden"
    music.rename(hidden)          # what the sandbox sees without access

    result = scan(db, music)

    assert result.unreachable == {music: 2}
    assert result.missing == 0
    assert not any(s.missing for s in db.all_scores())


def test_removed_files_in_a_reachable_folder_are_still_flagged(db, music):
    scan(db, music)
    (music / "Sonata.pdf").unlink()
    result = scan(db, music)
    assert result.missing == 1
    assert result.unreachable == {}


def test_folder_without_permission_is_unreachable(db, music):
    if os.geteuid() == 0:
        pytest.skip("root can open any folder")
    scan(db, music)
    music.chmod(0)
    try:
        assert not is_reachable(music)
        assert scan(db, music).unreachable == {music: 2}
    finally:
        music.chmod(0o755)


def test_empty_unreachable_folder_reports_no_scores(db, tmp_path):
    # The old default ~/Music, for someone who has no such folder.
    result = scan(db, tmp_path / "Music")
    assert result.unreachable == {tmp_path / "Music": 0}


def test_choosing_the_folder_again_relinks_scores(db, music, tmp_path):
    """The document portal hands the same folder back under a new path.
    Scores keep their rows, and so their annotations and reading position."""
    scan(db, music)
    before = {Path(s.path).name: s.id for s in db.all_scores()}
    db.update_score(before["Sonata.pdf"], last_page=3)

    portal = tmp_path / "doc" / "a1b2c3" / "Music"
    portal.parent.mkdir(parents=True)
    music.rename(portal)
    result = scan(db, music, portal)

    assert result.added == 0
    after = {Path(s.path).name: s for s in db.all_scores()}
    assert {name: s.id for name, s in after.items()} == before
    assert all(is_under(s.path, portal) for s in after.values())
    assert after["Sonata.pdf"].last_page == 3


def test_is_under_respects_folder_boundaries():
    assert is_under("/m/Music/a.pdf", Path("/m/Music"))
    assert not is_under("/m/Music2/a.pdf", Path("/m/Music"))
    assert not is_under("/m/Music", Path("/m/Music"))


def test_same_file_under_another_path(db, music, tmp_path):
    scan(db, music)
    scores = db.all_scores()
    forwarded = tmp_path / "doc" / "xyz" / "Sonata.pdf"
    forwarded.parent.mkdir(parents=True)
    shutil.copy(music / "Sonata.pdf", forwarded)

    match = find_same_file(forwarded, scores)
    assert match is not None and Path(match.path) == music / "Sonata.pdf"
    # A file that's in the library under its own path is not "another" one.
    assert find_same_file(music / "Sonata.pdf", scores) is None
    other = make_pdf(tmp_path / "Other.pdf", "Other")
    assert find_same_file(other, scores) is None


# ----------------------------------------------------------------- config

def test_replace_directory_keeps_the_order(tmp_path):
    config = Config({"library": {"directories": []}}, tmp_path / "c.toml")
    config.directories = [Path("/a"), Path("/b"), Path("/c")]
    config.replace_directory(Path("/b"), Path("/doc/1/b"))
    assert config.directories == [Path("/a"), Path("/doc/1/b"), Path("/c")]
    # Choosing a folder that is already listed just drops the old one.
    config.replace_directory(Path("/a"), Path("/c"))
    assert config.directories == [Path("/doc/1/b"), Path("/c")]


# ---------------------------------------------------------------- sandbox

def test_display_path_uses_the_portal_host_path(monkeypatch, tmp_path):
    portal = "/run/user/1000/doc/a1b2c3/Music"
    home = str(Path.home())

    def getxattr(path, name):
        assert name == sandbox.HOST_PATH_XATTR
        if str(path) == portal:
            return f"{home}/Music\0".encode()
        raise OSError(61, "No data available")

    monkeypatch.setattr(sandbox.os, "getxattr", getxattr)
    assert sandbox.host_path(portal) == f"{home}/Music"
    assert sandbox.display_path(portal) == "~/Music"
    assert sandbox.host_path(tmp_path) is None
    assert sandbox.display_path("/srv/scores") == "/srv/scores"


def test_in_flatpak(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / ".flatpak-info")
    assert not sandbox.in_flatpak()
    (tmp_path / ".flatpak-info").write_text("[Application]\n")
    assert sandbox.in_flatpak()


# ------------------------------------------- issue #19: folder, then subfolders

def portal(tmp_path, doc_id: str, folder: Path) -> Path:
    """A folder as the document portal hands it out: the same files under
    a path of its own, which keeps working after the folder is removed
    from the library."""
    link = tmp_path / "doc" / doc_id / folder.name
    link.parent.mkdir(parents=True)
    link.symlink_to(folder, target_is_directory=True)
    return link


@pytest.fixture
def nested(tmp_path):
    root = tmp_path / "host" / "leggio_test"
    make_pdf(root / "one" / "chopin.pdf", "chopin")
    make_pdf(root / "one" / "clair.pdf", "clair")
    make_pdf(root / "two" / "elise.pdf", "elise")
    make_pdf(root / "two" / "gymnopedie.pdf", "gymnopedie")
    return root


def test_subfolders_added_after_removing_their_parent_relink(db, nested, tmp_path):
    top = portal(tmp_path, "aaa", nested)
    scan(db, top)
    before = {Path(s.path).name: s.id for s in db.all_scores()}
    db.update_score(before["chopin.pdf"], last_page=2)
    db.add_bookmark(Bookmark(0, before["chopin.pdf"], "Trio", page=1))
    db.add_annotation(Annotation(
        0, before["elise.pdf"], 0, Annotation.INK, "#000000", 0.002,
        {"points": [[0.1, 0.1], [0.2, 0.2]]},
    ))

    # The parent is removed from the library, then its subfolders are
    # added one at a time. The parent's portal path still opens.
    one = portal(tmp_path, "bbb", nested / "one")
    first = scan(db, one)
    two = portal(tmp_path, "ccc", nested / "two")
    second = scan(db, one, two)

    assert (first.added, second.added) == (0, 0)
    after = {Path(s.path).name: s for s in db.all_scores()}
    assert {name: s.id for name, s in after.items()} == before
    assert not any(s.missing for s in after.values())
    assert is_under(after["chopin.pdf"].path, one)
    assert is_under(after["elise.pdf"].path, two)
    assert after["chopin.pdf"].last_page == 2
    assert [b.name for b in db.bookmarks_for(before["chopin.pdf"])] == ["Trio"]
    assert len(db.annotations_for(before["elise.pdf"], 0)) == 1


def test_scores_already_flagged_missing_relink_to_a_subfolder(db, nested, tmp_path):
    """With another library folder left, removing the parent flags its
    scores as missing before the subfolders are added."""
    other = tmp_path / "Other"
    make_pdf(other / "Sonata.pdf", "Sonata")
    top = portal(tmp_path, "aaa", nested)
    scan(db, top, other)
    before = {Path(s.path).name: s.id for s in db.all_scores()}

    assert scan(db, other).missing == 4
    one = portal(tmp_path, "bbb", nested / "one")
    two = portal(tmp_path, "ccc", nested / "two")
    result = scan(db, other, one, two)

    assert result.added == 0
    after = {Path(s.path).name: s for s in db.all_scores()}
    assert {name: s.id for name, s in after.items()} == before
    assert not any(s.missing for s in after.values())


def test_the_same_file_in_two_library_folders_stays_two_scores(db, tmp_path):
    a = make_pdf(tmp_path / "A" / "Sonata.pdf", "Sonata").parent
    b = tmp_path / "B"
    b.mkdir()
    shutil.copy(a / "Sonata.pdf", b / "Sonata.pdf")
    scan(db, a)
    assert scan(db, a, b).added == 1
    assert len(db.all_scores()) == 2


def test_forget_missing_removes_only_missing_scores(db, music):
    scan(db, music)
    ids = {Path(s.path).name: s.id for s in db.all_scores()}
    db.add_bookmark(Bookmark(0, ids["Sonata.pdf"], "Trio", page=0))
    (music / "Sonata.pdf").unlink()
    scan(db, music)

    assert db.forget_missing() == 1
    assert [Path(s.path).name for s in db.all_scores()] == ["Aria.pdf"]
    assert db.bookmarks_for(ids["Sonata.pdf"]) == []
    assert db.forget_missing() == 0
