"""SQLite storage. Built with a users table from day one, so adding other
students later (onboarding) needs no schema change."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .parser import Deliverable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    feed_source TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'Asia/Singapore',
    telegram_chat_id INTEGER,           -- the only Telegram chat the bot answers
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliverables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    uid         TEXT NOT NULL,
    module_code TEXT NOT NULL,
    module_name TEXT NOT NULL,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    due_at      TEXT NOT NULL,          -- ISO 8601 with offset
    link        TEXT,
    done        INTEGER NOT NULL DEFAULT 0,
    done_at     TEXT,
    done_source TEXT,                   -- 'manual' or 'auto' (confirmed by POLITEMall)
    removed     INTEGER NOT NULL DEFAULT 0,  -- disappeared from the feed
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (user_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_deliverables_due ON deliverables (user_id, due_at);

-- Friendlier names for module codes, chosen by the user ("C207" -> "Database").
CREATE TABLE IF NOT EXISTS module_aliases (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    module_code TEXT NOT NULL,
    alias       TEXT NOT NULL,
    PRIMARY KEY (user_id, module_code)
);

-- Messages waiting to be delivered (by the Telegram bot, the web app, ...).
CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    deliverable_id INTEGER NOT NULL REFERENCES deliverables(id),
    kind           TEXT NOT NULL,      -- new | rescheduled:<due> | remind_3d | remind_1d | remind_morning
    created_at     TEXT NOT NULL,
    sent_at        TEXT,               -- NULL = pending; 'skipped' = superseded by a later reminder
    UNIQUE (deliverable_id, kind)
);
"""


@dataclass
class SyncResult:
    new: list[Deliverable] = field(default_factory=list)
    rescheduled: list[tuple[Deliverable, datetime]] = field(default_factory=list)  # (item, old due)
    removed: list[str] = field(default_factory=list)  # titles
    total_in_feed: int = 0


class Store:
    def __init__(self, path: str | Path = "duetracker.db"):
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Upgrade databases created by older versions in place."""
        user_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "telegram_chat_id" not in user_cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN telegram_chat_id INTEGER")
            self.conn.commit()
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(deliverables)")}
        if "done_source" not in cols:
            self.conn.execute("ALTER TABLE deliverables ADD COLUMN done_source TEXT")
            self.conn.execute("UPDATE deliverables SET done_source = 'manual' WHERE done = 1")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- users -------------------------------------------------------------

    def ensure_user(self, name: str, feed_source: str, tz: str = "Asia/Singapore") -> int:
        """Single-user mode: user 1 comes from .env and is kept in sync with it."""
        row = self.conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE users SET feed_source = ?, timezone = ? WHERE id = ?", (feed_source, tz, row["id"])
            )
            self.conn.commit()
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO users (name, feed_source, timezone, created_at) VALUES (?, ?, ?, ?)",
            (name, feed_source, tz, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def telegram_owner(self, user_id: int) -> int | None:
        row = self.conn.execute("SELECT telegram_chat_id FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["telegram_chat_id"] if row else None

    def claim_telegram_owner(self, user_id: int, chat_id: int) -> bool:
        """First come, first served: only succeeds if no owner is set yet."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE users SET telegram_chat_id = ? WHERE id = ? AND telegram_chat_id IS NULL",
                (chat_id, user_id),
            )
        return cur.rowcount == 1

    # ---- sync --------------------------------------------------------------

    def sync(self, user_id: int, items: list[Deliverable], now: datetime) -> SyncResult:
        """Upsert the feed's deliverables and report what changed since last time."""
        result = SyncResult(total_in_feed=len(items))
        stamp = now.isoformat()
        seen_uids = set()

        with self.conn:
            for item in items:
                seen_uids.add(item.uid)
                row = self.conn.execute(
                    "SELECT due_at, removed FROM deliverables WHERE user_id = ? AND uid = ?",
                    (user_id, item.uid),
                ).fetchone()

                if row is None:
                    self.conn.execute(
                        """INSERT INTO deliverables
                           (user_id, uid, module_code, module_name, title, kind, due_at, link,
                            first_seen, last_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, item.uid, item.module_code, item.module_name, item.title,
                         item.kind, item.due_at.isoformat(), item.link, stamp, stamp),
                    )
                    if item.due_at >= now:
                        result.new.append(item)
                    continue

                old_due = datetime.fromisoformat(row["due_at"])
                if old_due != item.due_at and item.due_at >= now:
                    result.rescheduled.append((item, old_due))
                self.conn.execute(
                    """UPDATE deliverables
                       SET module_code = ?, module_name = ?, title = ?, kind = ?, due_at = ?,
                           link = ?, removed = 0, last_seen = ?
                       WHERE user_id = ? AND uid = ?""",
                    (item.module_code, item.module_name, item.title, item.kind,
                     item.due_at.isoformat(), item.link, stamp, user_id, item.uid),
                )

            # Upcoming items that vanished from the feed (lecturer deleted/hid them).
            for row in self.conn.execute(
                "SELECT uid, title, due_at FROM deliverables WHERE user_id = ? AND removed = 0",
                (user_id,),
            ).fetchall():
                if row["uid"] not in seen_uids and datetime.fromisoformat(row["due_at"]) >= now:
                    self.conn.execute(
                        "UPDATE deliverables SET removed = 1 WHERE user_id = ? AND uid = ?",
                        (user_id, row["uid"]),
                    )
                    result.removed.append(row["title"])

        return result

    # ---- queries -----------------------------------------------------------

    def upcoming(self, user_id: int, now: datetime, until: datetime | None = None,
                 include_done: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM deliverables WHERE user_id = ? AND removed = 0 AND due_at >= ?"
        params: list = [user_id, now.isoformat()]
        if until is not None:
            sql += " AND due_at <= ?"
            params.append(until.isoformat())
        if not include_done:
            sql += " AND done = 0"
        sql += " ORDER BY due_at, module_code, title"
        return self.conn.execute(sql, params).fetchall()

    def set_done(self, user_id: int, item_id: int, done: bool = True,
                 source: str = "manual") -> sqlite3.Row | None:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE deliverables SET done = ?, done_at = ?, done_source = ? WHERE user_id = ? AND id = ?",
                (int(done), _now() if done else None, source if done else None, user_id, item_id),
            )
        if cur.rowcount == 0:
            return None
        return self.conn.execute("SELECT * FROM deliverables WHERE id = ?", (item_id,)).fetchone()


    def assignments_to_check(self, user_id: int, since: datetime) -> list[sqlite3.Row]:
        """Not-done assignments due after `since` (includes recently overdue ones)."""
        return self.conn.execute(
            """SELECT * FROM deliverables
               WHERE user_id = ? AND kind = 'assignment' AND done = 0 AND removed = 0
                 AND due_at >= ?
               ORDER BY due_at""",
            (user_id, since.isoformat()),
        ).fetchall()


    # ---- lookups / notifications ---------------------------------------------

    def count_deliverables(self, user_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM deliverables WHERE user_id = ?", (user_id,)
        ).fetchone()[0]

    def get_by_uid(self, user_id: int, uid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM deliverables WHERE user_id = ? AND uid = ?", (user_id, uid)
        ).fetchone()

    def mark_done_by_uids(self, user_id: int, uids: list[str], source: str = "auto") -> list[sqlite3.Row]:
        """Mark items done by feed UID (stable across machines). Returns newly-done rows."""
        changed = []
        with self.conn:
            for uid in uids:
                row = self.get_by_uid(user_id, uid)
                if row is None or row["done"]:
                    continue
                self.conn.execute(
                    "UPDATE deliverables SET done = 1, done_at = ?, done_source = ? WHERE id = ?",
                    (_now(), source, row["id"]),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO notifications (user_id, deliverable_id, kind, created_at)
                       VALUES (?, ?, 'submitted', ?)""", (user_id, row["id"], _now()))
                changed.append(row)
        return changed

    def auto_done_uids(self, user_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT uid FROM deliverables WHERE user_id = ? AND done = 1 AND done_source = 'auto'",
            (user_id,),
        )]

    def add_notification(self, user_id: int, deliverable_id: int, kind: str,
                         skipped: bool = False) -> bool:
        """Returns True if a new notification was created (False if it already existed)."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO notifications (user_id, deliverable_id, kind, created_at, sent_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, deliverable_id, kind, _now(), "skipped" if skipped else None),
            )
        return cur.rowcount == 1

    def clear_reminders(self, deliverable_id: int) -> None:
        """After a deadline moves, its reminders must be planned again."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM notifications WHERE deliverable_id = ? AND kind LIKE 'remind_%'",
                (deliverable_id,),
            )

    def pending_notifications(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT n.id, n.kind, n.created_at, d.id AS deliverable_id, d.module_code,
                      d.module_name, d.title, d.kind AS item_kind, d.due_at, d.link, d.done
               FROM notifications n JOIN deliverables d ON d.id = n.deliverable_id
               WHERE n.user_id = ? AND n.sent_at IS NULL
               ORDER BY n.id""",
            (user_id,),
        ).fetchall()

    def ack_notification(self, user_id: int, notification_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE notifications SET sent_at = ? WHERE user_id = ? AND id = ? AND sent_at IS NULL",
                (_now(), user_id, notification_id),
            )
        return cur.rowcount == 1

    # ---- history / pickers -----------------------------------------------------

    def history(self, user_id: int, since: datetime, now: datetime) -> list[sqlite3.Row]:
        """Completed since `since`, plus overdue items never marked done. Newest first."""
        return self.conn.execute(
            """SELECT * FROM deliverables
               WHERE user_id = ? AND removed = 0 AND (
                     (done = 1 AND julianday(done_at) >= julianday(?))
                  OR (done = 0 AND julianday(due_at) < julianday(?) AND julianday(due_at) >= julianday(?)))
               ORDER BY julianday(COALESCE(done_at, due_at)) DESC""",
            (user_id, since.isoformat(), now.isoformat(), since.isoformat()),
        ).fetchall()

    def recently_done(self, user_id: int, since: datetime, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM deliverables WHERE user_id = ? AND done = 1
                 AND julianday(done_at) >= julianday(?)
               ORDER BY julianday(done_at) DESC LIMIT ?""",
            (user_id, since.isoformat(), limit),
        ).fetchall()

    def open_items(self, user_id: int, since: datetime, until: datetime) -> list[sqlite3.Row]:
        """Not done, due between `since` (recently overdue) and `until`. For the picker."""
        return self.conn.execute(
            """SELECT * FROM deliverables WHERE user_id = ? AND done = 0 AND removed = 0
                 AND due_at >= ? AND due_at <= ? ORDER BY due_at""",
            (user_id, since.isoformat(), until.isoformat()),
        ).fetchall()

    # ---- module names ----------------------------------------------------------

    def aliases(self, user_id: int) -> dict[str, str]:
        return {r["module_code"]: r["alias"] for r in self.conn.execute(
            "SELECT module_code, alias FROM module_aliases WHERE user_id = ?", (user_id,))}

    def set_alias(self, user_id: int, module_code: str, alias: str | None) -> None:
        with self.conn:
            if alias:
                self.conn.execute(
                    """INSERT INTO module_aliases (user_id, module_code, alias) VALUES (?, ?, ?)
                       ON CONFLICT (user_id, module_code) DO UPDATE SET alias = excluded.alias""",
                    (user_id, module_code, alias))
            else:
                self.conn.execute("DELETE FROM module_aliases WHERE user_id = ? AND module_code = ?",
                                  (user_id, module_code))

    def modules(self, user_id: int, since: datetime) -> list[sqlite3.Row]:
        """Modules with something due since `since` (i.e. this semester's, not old ones)."""
        return self.conn.execute(
            """SELECT module_code, MAX(module_name) AS module_name FROM deliverables
               WHERE user_id = ? AND module_code != '' AND due_at >= ?
               GROUP BY module_code ORDER BY module_code""",
            (user_id, since.isoformat()),
        ).fetchall()

    def get(self, user_id: int, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM deliverables WHERE user_id = ? AND id = ?", (user_id, item_id)
        ).fetchone()


def _now() -> str:
    return datetime.now().astimezone().isoformat()
