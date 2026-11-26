"""Decide which reminders are due. Delivery (Telegram, web) happens elsewhere.

Stages, all at 08:00 local time:
    remind_3d (3 days before)  ->  remind_1d (the day before)  ->  remind_morning (the day itself)

Only the latest stage that applies is sent. If an item is first seen 10 hours
before it's due, you get one "due today" message, not three at once.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .db import Store

MORNING = time(8, 0)


def stage_times(due_at: datetime) -> list[tuple[str, datetime]]:
    def at_8am(days_before: int) -> datetime:
        return datetime.combine(due_at.date() - timedelta(days=days_before), MORNING, tzinfo=due_at.tzinfo)

    stages = [("remind_3d", at_8am(3)), ("remind_1d", at_8am(1)), ("remind_morning", at_8am(0))]
    return [(kind, at) for kind, at in stages if at < due_at]


def plan_reminders(store: Store, user_id: int, now: datetime) -> int:
    """Create pending reminder notifications whose time has come. Returns how many."""
    created = 0
    for row in store.upcoming(user_id, now):  # not done, not removed, not past
        due_at = datetime.fromisoformat(row["due_at"])
        reached = [kind for kind, at in stage_times(due_at) if at <= now]
        if not reached:
            continue
        latest = reached[-1]
        for kind in reached:
            if store.add_notification(user_id, row["id"], kind, skipped=(kind != latest)):
                created += kind == latest
    return created
