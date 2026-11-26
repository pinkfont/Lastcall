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
"""


@dataclass
class SyncResult:
    new: list[Deliverable] = field(default_factory=list)
    rescheduled: list[tuple[Deliverable, datetime]] = field(default_factory=list)  # (item, old due)
    removed: list[str] = field(default_factory=list)  # titles
    total_in_feed: int = 0


class Store:
    def __init__(self, path: str | Path = "duetracker.db"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Upgrade databases created by older versions in place."""
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


def _now() -> str:
    return datetime.now().astimezone().isoformat()
