from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from duetracker.db import Store
from duetracker.parser import parse_feed

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=SGT)
SAMPLE = (Path(__file__).parent / "sample_feed.ics").read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_first_sync_reports_only_upcoming_as_new(store):
    uid = store.ensure_user("me", "sample")
    result = store.sync(uid, parse_feed(SAMPLE), NOW)
    titles = {d.title for d in result.new}
    assert "Week 6 Deliverables" in titles
    assert "Week 2 Deliverables" not in titles  # already past


def test_resync_is_idempotent(store):
    uid = store.ensure_user("me", "sample")
    items = parse_feed(SAMPLE)
    store.sync(uid, items, NOW)
    again = store.sync(uid, items, NOW)
    assert not again.new and not again.rescheduled and not again.removed


def test_detects_rescheduled_deadline(store):
    uid = store.ensure_user("me", "sample")
    items = parse_feed(SAMPLE)
    store.sync(uid, items, NOW)
    moved = [replace(d, due_at=d.due_at + timedelta(days=2)) if d.title == "Week 6 Deliverables" else d
             for d in items]
    result = store.sync(uid, moved, NOW)
    assert [d.title for d, _ in result.rescheduled] == ["Week 6 Deliverables"]


def test_detects_removed_item(store):
    uid = store.ensure_user("me", "sample")
    items = parse_feed(SAMPLE)
    store.sync(uid, items, NOW)
    result = store.sync(uid, [d for d in items if d.title != "Learning Memo 2"], NOW)
    assert result.removed == ["Learning Memo 2"]
    assert "Learning Memo 2" not in {r["title"] for r in store.upcoming(uid, NOW)}


def test_done_survives_resync_and_hides_item(store):
    uid = store.ensure_user("me", "sample")
    store.sync(uid, parse_feed(SAMPLE), NOW)
    first = store.upcoming(uid, NOW)[0]
    store.set_done(uid, first["id"])
    store.sync(uid, parse_feed(SAMPLE), NOW)
    assert first["id"] not in {r["id"] for r in store.upcoming(uid, NOW)}
    assert first["id"] in {r["id"] for r in store.upcoming(uid, NOW, include_done=True)}


def test_window_filter(store):
    uid = store.ensure_user("me", "sample")
    store.sync(uid, parse_feed(SAMPLE), NOW)
    week = {r["title"] for r in store.upcoming(uid, NOW, NOW + timedelta(days=8))}
    assert week == {"Week 6 Deliverables", "Lesson 25 Async eLearning Quiz"}
