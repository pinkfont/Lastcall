"""The functions the CLI, a scheduler, and later the Telegram bot all call."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .db import Store, SyncResult
from .fetcher import fetch_feed
from .parser import parse_feed
from .politemall_api import ids_from_link

# Check the same window History shows (30 days), so past work that was submitted
# isn't listed as incomplete just because it was never checked.
LOOKBACK = timedelta(days=30)


def sync_user(store: Store, user_id: int, feed_source: str, tz: str,
              now: datetime | None = None) -> SyncResult:
    now = now or datetime.now(ZoneInfo(tz))
    items = parse_feed(fetch_feed(feed_source), tz=tz)
    return store.sync(user_id, items, now)


class SubmissionSource(Protocol):
    def find_folder_id(self, ou: int, title: str) -> int | None: ...
    def has_submitted(self, ou: int, db: int) -> bool | None: ...


@dataclass
class AutoDoneResult:
    marked: list[str] = field(default_factory=list)  # "C207 Week 6 Deliverables"
    checked: int = 0
    unknown: int = 0  # couldn't tell (no ids found, or no access)
    status: str = "ok"  # ok | not_logged_in | expired | error
    error: str = ""


def check_submissions(store: Store, user_id: int, client: SubmissionSource,
                      now: datetime) -> AutoDoneResult:
    """Mark assignments done when POLITEMall shows a submission for them."""
    result = AutoDoneResult()
    for row in store.assignments_to_check(user_id, now - LOOKBACK):
        ou, db = ids_from_link(row["link"])
        if ou is not None and db is None:
            db = client.find_folder_id(ou, row["title"])
        if ou is None or db is None:
            result.unknown += 1
            continue

        submitted = client.has_submitted(ou, db)
        result.checked += 1
        if submitted is None:
            result.unknown += 1
        elif submitted:
            store.set_done(user_id, row["id"], done=True, source="auto")
            store.add_notification(user_id, row["id"], "submitted")
            result.marked.append(f"{row['module_code']} {row['title']}")
    return result


def auto_done(store: Store, user_id: int, base_url: str, session_dir: str,
              now: datetime) -> AutoDoneResult:
    """Run the POLITEMall submission check if a login session exists. Never raises."""
    from .politemall_api import NotLoggedIn, SessionExpired, SubmissionClient

    try:
        with SubmissionClient(base_url, session_dir) as client:
            return check_submissions(store, user_id, client, now)
    except NotLoggedIn:
        return AutoDoneResult(status="not_logged_in")
    except SessionExpired:
        return AutoDoneResult(status="expired")
    except Exception as exc:
        return AutoDoneResult(status="error", error=f"{exc.__class__.__name__}: {exc}")


@dataclass
class JobResult:
    new: int = 0
    rescheduled: int = 0
    removed: int = 0
    reminders: int = 0
    in_feed: int = 0


def run_sync_job(store: Store, user_id: int, feed_source: str, tz: str,
                 now: datetime | None = None) -> JobResult:
    """Feed sync + alerts for new/moved deadlines + reminder planning.

    This is what the scheduler runs every couple of hours.
    """
    from .reminders import plan_reminders

    now = now or datetime.now(ZoneInfo(tz))
    first_sync = store.count_deliverables(user_id) == 0
    result = sync_user(store, user_id, feed_source, tz, now)

    # Don't alert about all ~100 existing deadlines on the very first sync.
    if not first_sync:
        for item in result.new:
            row = store.get_by_uid(user_id, item.uid)
            store.add_notification(user_id, row["id"], "new")
        for item, _old in result.rescheduled:
            row = store.get_by_uid(user_id, item.uid)
            store.clear_reminders(row["id"])
            store.add_notification(user_id, row["id"], f"rescheduled:{item.due_at.isoformat()}")

    reminders = plan_reminders(store, user_id, now)
    return JobResult(new=len(result.new), rescheduled=len(result.rescheduled),
                     removed=len(result.removed), reminders=reminders,
                     in_feed=result.total_in_feed)
