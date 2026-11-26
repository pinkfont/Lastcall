"""Command-line interface.

    python -m duetracker login           sign in once so submissions can be detected
    python -m duetracker sync            pull the feed, report changes, auto-tick submitted work
    python -m duetracker due             what's due in the next 7 days
    python -m duetracker due --days 30   ...or any window (--all for everything upcoming)
    python -m duetracker done 12         mark item 12 as submitted
    python -m duetracker undo 12         unmark it
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import load_settings
from .db import Store
from .fetcher import FeedError
from .politemall_api import NotLoggedIn, SessionExpired, SubmissionClient, login
from .service import check_submissions, sync_user

KIND_LABEL = {
    "assignment": "Assignment",
    "quiz": "Quiz",
    "elearning": "E-learning",
    "peer_evaluation": "Peer eval",
    "discussion": "Discussion",
    "other": "Other",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="duetracker", description="POLITEMall deadline tracker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="sign in to POLITEMall so submissions can be detected")
    sync = sub.add_parser("sync", help="fetch the feed and update the database")
    sync.add_argument("--no-submissions", action="store_true", help="skip the auto-done check")
    due = sub.add_parser("due", help="list upcoming deliverables")
    due.add_argument("--days", type=int, default=7)
    due.add_argument("--all", action="store_true", help="show everything upcoming")
    due.add_argument("--include-done", action="store_true")
    for name in ("done", "undo"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
    args = parser.parse_args(argv)

    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    store = Store(settings.db_path)
    try:
        user_id = store.ensure_user(settings.user_name, settings.feed_source, settings.timezone)

        if args.command == "login":
            try:
                login(settings.politemall_url, settings.session_dir)
            except Exception as exc:
                print(f"Login failed: {exc}")
                return 1
            print("Logged in. Submitted assignments will now be ticked off on each sync.")

        elif args.command == "sync":
            try:
                result = sync_user(store, user_id, settings.feed_source, settings.timezone, now)
            except FeedError as exc:
                print(f"Sync failed: {exc}")
                return 1
            print(f"Synced: {result.total_in_feed} deliverables in feed.")
            for item in result.new:
                print(f"  NEW        {item.module_code:<7} {item.title}  (due {_fmt(item.due_at, now)})")
            for item, old in result.rescheduled:
                print(f"  RESCHEDULED {item.module_code:<7} {item.title}: "
                      f"{old:%d %b %H:%M} -> {item.due_at:%d %b %H:%M}")
            for title in result.removed:
                print(f"  REMOVED    {title}")
            if not (result.new or result.rescheduled or result.removed):
                print("  No changes.")
            if not args.no_submissions:
                _auto_done(store, user_id, settings, now)

        elif args.command == "due":
            until = None if args.all else now + timedelta(days=args.days)
            rows = store.upcoming(user_id, now, until, include_done=args.include_done)
            window = "upcoming" if args.all else f"in the next {args.days} days"
            if not rows:
                print(f"Nothing due {window}. (Run `sync` first if this looks wrong.)")
                return 0
            print(f"{len(rows)} due {window}:\n")
            for r in rows:
                due_at = datetime.fromisoformat(r["due_at"])
                tick = ("[a]" if r["done_source"] == "auto" else "[x]") if r["done"] else "[ ]"
                print(f"{tick} #{r['id']:<4} {r['module_code']:<7} {r['title']}")
                print(f"           {KIND_LABEL.get(r['kind'], r['kind'])} · {_fmt(due_at, now)}")

        elif args.command in ("done", "undo"):
            row = store.set_done(user_id, args.id, done=(args.command == "done"))
            if row is None:
                print(f"No deliverable with id {args.id}.")
                return 1
            verb = "Marked done" if args.command == "done" else "Unmarked"
            print(f"{verb}: {row['module_code']} {row['title']}")
            if args.command == "undo" and row["kind"] == "assignment":
                print("(If POLITEMall shows a submission, the next sync will tick it again.)")
        return 0
    finally:
        store.close()


def _auto_done(store, user_id, settings, now) -> None:
    try:
        with SubmissionClient(settings.politemall_url, settings.session_dir) as client:
            result = check_submissions(store, user_id, client, now)
    except NotLoggedIn:
        print("\nTip: run login.bat once to tick off submitted assignments automatically.")
        return
    except SessionExpired:
        print("\nPOLITEMall login expired: run login.bat again to keep auto-done working.")
        return
    except Exception as exc:  # never let this break the deadline list
        print(f"\nCouldn't check submissions this time ({exc.__class__.__name__}: {exc}).")
        return
    print(f"\nChecked {result.checked} assignment(s) on POLITEMall.")
    for title in result.marked:
        print(f"  SUBMITTED  {title}  (ticked off automatically)")


def _fmt(due_at: datetime, now: datetime) -> str:
    left = due_at - now
    if left.total_seconds() < 0:
        rel = "overdue"
    elif left.days >= 1:
        rel = f"in {left.days}d {left.seconds // 3600}h"
    else:
        rel = f"in {left.seconds // 3600}h {(left.seconds % 3600) // 60}m"
    return f"{due_at:%a %d %b, %I:%M %p} ({rel})"
