from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from duetracker.db import Store
from duetracker.parser import Deliverable
from duetracker.reminders import plan_reminders, stage_times

SGT = ZoneInfo("Asia/Singapore")
DUE = datetime(2026, 12, 6, 23, 59, 59, tzinfo=SGT)


def item(uid="a", due=DUE):
    return Deliverable(uid=uid, module_code="C999", module_name="Sample", title=f"Task {uid}",
                       kind="assignment", due_at=due, link=None)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def pending_kinds(store, uid):
    return [n["kind"] for n in store.pending_notifications(uid)]


def test_stage_order():
    kinds = [k for k, _ in stage_times(DUE)]
    assert kinds == ["remind_3d", "remind_1d", "remind_morning"]


def test_nothing_before_three_days(store):
    uid = store.ensure_user("me", "x")
    now = DUE - timedelta(days=4)
    store.sync(uid, [item()], now)
    assert plan_reminders(store, uid, now) == 0


def test_each_stage_fires_once(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [item()], DUE - timedelta(days=5))
    for days_before, expected in ((2.9, ["remind_3d"]), (0.9, ["remind_1d"])):
        now = DUE - timedelta(days=days_before)
        plan_reminders(store, uid, now)
        assert pending_kinds(store, uid) == expected
        for n in store.pending_notifications(uid):
            store.ack_notification(uid, n["id"])
        assert plan_reminders(store, uid, now) == 0  # no duplicates
    morning = DUE.replace(hour=8, minute=30)
    plan_reminders(store, uid, morning)
    assert pending_kinds(store, uid) == ["remind_morning"]


def test_late_discovery_sends_only_latest_stage(store):
    uid = store.ensure_user("me", "x")
    now = DUE.replace(hour=9)  # first seen on the morning it's due
    store.sync(uid, [item()], now)
    assert plan_reminders(store, uid, now) == 1
    assert pending_kinds(store, uid) == ["remind_morning"]


def test_done_items_get_no_reminders(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [item()], DUE - timedelta(days=5))
    row = store.get_by_uid(uid, "a")
    store.set_done(uid, row["id"])
    assert plan_reminders(store, uid, DUE - timedelta(hours=2)) == 0
