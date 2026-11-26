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

# Also check assignments that went overdue recently, in case of late submissions.
LOOKBACK = timedelta(days=7)


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
            result.marked.append(f"{row['module_code']} {row['title']}")
    return result
