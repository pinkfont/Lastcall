"""Auto-done logic, tested with a fake POLITEMall so no network or login is needed."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from duetracker.db import Store
from duetracker.parser import Deliverable
from duetracker.politemall_api import ids_from_link
from duetracker.service import check_submissions

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=SGT)
BASE = "https://lms.example.edu/d2l"


def item(uid, title, link, kind="assignment", day=29):
    return Deliverable(uid=uid, module_code="C999", module_name="Sample", title=title,
                       kind=kind, due_at=datetime(2026, 11, day, 23, 59, 59, tzinfo=SGT), link=link)


class FakePolitemall:
    def __init__(self, submitted, folders=None):
        self.submitted = submitted          # {(ou, db): True/False/None}
        self.folders = folders or {}        # {(ou, title): db}
        self.calls = []

    def find_folder_id(self, ou, title):
        return self.folders.get((ou, title))

    def has_submitted(self, ou, db):
        self.calls.append((ou, db))
        return self.submitted.get((ou, db), False)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_ids_from_link():
    assert ids_from_link(f"{BASE}/lms/dropbox/user/folder_submit_files.d2l?ou=111&db=222") == (111, 222)
    assert ids_from_link(f"{BASE}/le/calendar/111/event/5/detailsview?ou=111#5") == (111, None)
    assert ids_from_link(None) == (None, None)


def test_marks_submitted_assignment_as_auto_done(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [
        item("a", "Week 6", f"{BASE}/lms/dropbox/user/folder_submit_files.d2l?ou=1&db=10"),
        item("b", "Week 7", f"{BASE}/lms/dropbox/user/folder_submit_files.d2l?ou=1&db=11"),
    ], NOW)
    result = check_submissions(store, uid, FakePolitemall({(1, 10): True}), NOW)
    assert result.marked == ["C999 Week 6"]
    remaining = store.upcoming(uid, NOW)
    assert [r["title"] for r in remaining] == ["Week 7"]
    done = store.upcoming(uid, NOW, include_done=True)[0]
    assert done["done_source"] == "auto"


def test_falls_back_to_matching_folder_by_name(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [item("c", "Poster Submission", f"{BASE}/le/calendar/1/event/9/detailsview?ou=1#9")], NOW)
    fake = FakePolitemall({(1, 42): True}, folders={(1, "Poster Submission"): 42})
    result = check_submissions(store, uid, fake, NOW)
    assert result.marked == ["C999 Poster Submission"]


def test_skips_quizzes_already_done_and_unknowns(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [
        item("q", "Quiz 1", f"{BASE}/lms/quizzing/quizzing.d2l?ou=1&qi=5", kind="quiz"),
        item("m", "Manual one", f"{BASE}/lms/dropbox/user/folder_submit_files.d2l?ou=1&db=20"),
        item("n", "No access", f"{BASE}/lms/dropbox/user/folder_submit_files.d2l?ou=1&db=30"),
        item("x", "No ids", None),
    ], NOW)
    manual = next(r for r in store.upcoming(uid, NOW) if r["title"] == "Manual one")
    store.set_done(uid, manual["id"])
    fake = FakePolitemall({(1, 30): None})
    result = check_submissions(store, uid, fake, NOW)
    assert fake.calls == [(1, 30)]          # quiz and already-done item not checked
    assert result.unknown == 2               # "No access" + "No ids"
    assert result.marked == []


def test_old_database_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, feed_source TEXT, timezone TEXT, created_at TEXT);
        CREATE TABLE deliverables (id INTEGER PRIMARY KEY, user_id INTEGER, uid TEXT, module_code TEXT,
            module_name TEXT, title TEXT, kind TEXT, due_at TEXT, link TEXT, done INTEGER DEFAULT 0,
            done_at TEXT, removed INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT, UNIQUE(user_id, uid));
        INSERT INTO deliverables (user_id, uid, module_code, module_name, title, kind, due_at, done,
            first_seen, last_seen) VALUES (1, 'u', 'C1', 'M', 'T', 'assignment', '2026-12-01T00:00:00+08:00', 1, '', '');
    """)
    conn.close()
    s = Store(path)
    row = s.conn.execute("SELECT done_source FROM deliverables").fetchone()
    assert row["done_source"] == "manual"
    s.close()
