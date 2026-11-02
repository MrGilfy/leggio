# SPDX-License-Identifier: GPL-3.0-or-later
"""The SQLite library index."""

from __future__ import annotations

import sqlite3

import pytest

from leggio.db import SCHEMA_VERSION, Database
from leggio.models import Annotation, CropRect


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "library.db")
    yield database
    database.close()


def scan(db, path, *, title="Title", composer=None, pages=4, key=None):
    return db.sync_from_scan(
        str(path), default_title=title, composer=composer, page_count=pages,
        file_size=1000, file_mtime=1.0, content_key=key,
    )


def ink(score_id, page, points=((0.1, 0.1), (0.2, 0.2))):
    return Annotation(
        id=0, score_id=score_id, page=page, kind=Annotation.INK,
        color="#ff0000", width=0.004, data={"points": [list(p) for p in points]},
        created=1.0,
    )


# ------------------------------------------------------------------ schema

def test_new_database_is_at_current_schema_version(tmp_path):
    path = tmp_path / "library.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_reopening_keeps_data(tmp_path):
    path = tmp_path / "library.db"
    first = Database(path)
    score_id, _ = scan(first, tmp_path / "a.pdf")
    first.close()
    second = Database(path)
    assert second.get_score(score_id).path == str(tmp_path / "a.pdf")
    second.close()


# ------------------------------------------------------------------ scores

def test_scan_inserts_then_updates(db, tmp_path):
    score_id, new = scan(db, tmp_path / "a.pdf", pages=4)
    assert new
    again, new = scan(db, tmp_path / "a.pdf", pages=7)
    assert (again, new) == (score_id, False)
    assert db.get_score(score_id).page_count == 7


def test_scan_never_overwrites_locked_title(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf", title="From file")
    db.update_score(score_id, title="Mine", title_locked=True)
    scan(db, tmp_path / "a.pdf", title="From file again")
    assert db.get_score(score_id).title == "Mine"


def test_scan_does_not_blank_a_known_composer(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf", composer="Bach")
    scan(db, tmp_path / "a.pdf", composer=None)
    assert db.get_score(score_id).composer == "Bach"


def test_moved_file_is_relinked_with_its_metadata(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "old.pdf", key="abc")
    db.update_score(score_id, last_page=5)
    moved = tmp_path / "new.pdf"        # old.pdf never existed on disk
    relinked, new = scan(db, moved, key="abc")
    assert (relinked, new) == (score_id, False)
    score = db.get_score(score_id)
    assert score.path == str(moved)
    assert score.last_page == 5


def test_copy_of_an_existing_file_gets_its_own_row(db, tmp_path):
    original = tmp_path / "a.pdf"
    original.write_bytes(b"%PDF")
    score_id, _ = scan(db, original, key="abc")
    copy_id, new = scan(db, tmp_path / "b.pdf", key="abc")
    assert new and copy_id != score_id


def test_mark_missing_except_flags_unseen_rows_once(db, tmp_path):
    seen, _ = scan(db, tmp_path / "a.pdf")
    gone, _ = scan(db, tmp_path / "b.pdf")
    assert db.mark_missing_except([seen]) == 1
    assert db.get_score(gone).missing
    assert not db.get_score(seen).missing
    assert db.mark_missing_except([seen]) == 0
    scan(db, tmp_path / "b.pdf")
    assert not db.get_score(gone).missing


def test_update_score_rejects_unknown_columns(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    with pytest.raises(ValueError):
        db.update_score(score_id, path="/etc/passwd")


def test_update_score_stores_bools_as_ints(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    db.update_score(score_id, missing=True)
    assert db.get_score(score_id).missing is True


def test_crop_round_trip_and_clear(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    db.set_crop(score_id, CropRect(0.1, 0.2, 0.9, 0.8), auto=False)
    score = db.get_score(score_id)
    assert score.crop == CropRect(0.1, 0.2, 0.9, 0.8)
    assert score.crop_auto is False
    db.set_crop(score_id, None, auto=True)
    assert db.get_score(score_id).crop is None


def test_forget_score_cascades(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    setlist = db.create_setlist("Gig")
    db.add_to_setlist(setlist, score_id)
    db.add_annotation(ink(score_id, 0))
    db.forget_score(score_id)
    assert db.get_score(score_id) is None
    assert db.setlist_scores(setlist) == []
    assert db.annotations_for(score_id, 0) == []


# -------------------------------------------------------------------- tags

def test_set_tags_strips_and_dedupes_case_insensitively(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    db.set_tags(score_id, [" Jazz ", "jazz", "", "Duet"])
    assert db.get_score(score_id).tags == ("Duet", "Jazz")


def test_unused_tags_are_dropped(db, tmp_path):
    a, _ = scan(db, tmp_path / "a.pdf")
    b, _ = scan(db, tmp_path / "b.pdf")
    db.set_tags(a, ["Jazz", "Duet"])
    db.set_tags(b, ["Jazz"])
    assert db.all_tags() == [("Duet", 1), ("Jazz", 2)]
    db.set_tags(a, [])
    assert db.all_tags() == [("Jazz", 1)]


# ---------------------------------------------------------------- setlists

def test_setlist_keeps_order(db, tmp_path):
    ids = [scan(db, tmp_path / f"{n}.pdf")[0] for n in "abc"]
    setlist = db.create_setlist("Gig")
    for score_id in ids:
        db.add_to_setlist(setlist, score_id)
    assert [s.id for s in db.setlist_scores(setlist)] == ids
    db.set_setlist_order(setlist, ids[::-1])
    assert [s.id for s in db.setlist_scores(setlist)] == ids[::-1]
    [listed] = db.all_setlists()
    assert (listed.name, listed.item_count) == ("Gig", 3)


def test_rename_and_delete_setlist(db):
    setlist = db.create_setlist("Gig")
    db.rename_setlist(setlist, "Concert")
    assert [s.name for s in db.all_setlists()] == ["Concert"]
    db.delete_setlist(setlist)
    assert db.all_setlists() == []


# ------------------------------------------------------------- annotations

def test_annotation_round_trip(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    annotation = ink(score_id, 2, points=((0.25, 0.5), (0.3, 0.55)))
    annotation.id = db.add_annotation(annotation)
    [stored] = db.annotations_for(score_id, 2)
    assert stored.id == annotation.id
    assert stored.points == [(0.25, 0.5), (0.3, 0.55)]
    assert db.annotated_pages(score_id) == {2}


def test_clear_annotations_returns_what_it_removed(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    for page in (0, 0, 1):
        db.add_annotation(ink(score_id, page))
    removed = db.clear_annotations(score_id, 0)
    assert len(removed) == 2
    assert db.annotations_for(score_id, 0) == []
    assert len(db.annotations_for(score_id, 1)) == 1


def test_delete_annotations(db, tmp_path):
    score_id, _ = scan(db, tmp_path / "a.pdf")
    keep = db.add_annotation(ink(score_id, 0))
    drop = db.add_annotation(ink(score_id, 0))
    db.delete_annotations([drop])
    db.delete_annotations([])
    assert [a.id for a in db.annotations_for(score_id, 0)] == [keep]
