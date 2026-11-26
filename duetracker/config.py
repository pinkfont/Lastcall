"""Settings come from environment variables (or a local .env file, never committed)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    feed_source: str  # FEED_URL, or FEED_FILE for offline development
    db_path: str
    user_name: str
    timezone: str
    politemall_url: str  # e.g. https://rplms.polite.edu.sg (used for login + submissions)
    session_dir: str


def load_settings() -> Settings:
    load_dotenv()
    feed = os.getenv("FEED_URL") or os.getenv("FEED_FILE")
    if not feed:
        raise SystemExit(
            "No feed configured. Copy .env.example to .env and set FEED_URL "
            "(POLITEMall > Calendar > Subscribe > All Calendars and Tasks)."
        )
    return Settings(
        feed_source=feed.strip(),
        db_path=os.getenv("DB_PATH", "duetracker.db"),
        user_name=os.getenv("USER_NAME", "me"),
        timezone=os.getenv("TIMEZONE", "Asia/Singapore"),
        politemall_url=os.getenv("POLITEMALL_URL") or _base_url(feed.strip()),
        session_dir=os.getenv("SESSION_DIR", ".session"),
    )


def _base_url(feed: str) -> str:
    if feed.startswith(("http://", "https://")):
        from urllib.parse import urlsplit
        parts = urlsplit(feed)
        return f"{parts.scheme}://{parts.netloc}"
    return "https://rplms.polite.edu.sg"

