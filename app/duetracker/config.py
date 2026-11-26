"""Settings come from environment variables (or a local .env file, never committed)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .paths import CONFIG_DIR, DATA_DIR, data_path


@dataclass(frozen=True)
class Settings:
    feed_source: str  # FEED_URL, or FEED_FILE for offline development
    db_path: str
    user_name: str
    timezone: str
    politemall_url: str  # e.g. https://rplms.polite.edu.sg (used for login + submissions)
    session_dir: str
    api_key: str = ""                # protects the REST API
    api_url: str = ""                # where the laptop pushes auto-done results (deployed API)
    sync_interval_minutes: int = 120
    host: str = "127.0.0.1"
    port: int = 8000
    telegram_bot_token: str = ""
    notify_interval_seconds: int = 60


def load_settings() -> Settings:
    # config/.env first, then a .env in the current folder. Real environment
    # variables (as set on a hosting platform) always win over both.
    load_dotenv(CONFIG_DIR / ".env")
    load_dotenv(Path.cwd() / ".env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    feed = os.getenv("FEED_URL") or os.getenv("FEED_FILE")
    if not feed:
        raise SystemExit(
            "No feed configured. Copy config/.env.example to config/.env and set FEED_URL "
            "(POLITEMall > Calendar > Subscribe > All Calendars and Tasks)."
        )
    return Settings(
        feed_source=feed.strip(),
        db_path=str(data_path(os.getenv("DB_PATH", "duetracker.db"))),
        user_name=os.getenv("USER_NAME", "me"),
        timezone=os.getenv("TIMEZONE", "Asia/Singapore"),
        politemall_url=os.getenv("POLITEMALL_URL") or _base_url(feed.strip()),
        session_dir=str(data_path(os.getenv("SESSION_DIR", ".session"))),
        api_key=os.getenv("API_KEY", "").strip(),
        api_url=os.getenv("API_URL", "").strip().rstrip("/"),
        sync_interval_minutes=int(os.getenv("SYNC_INTERVAL_MINUTES", "120")),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        notify_interval_seconds=int(os.getenv("NOTIFY_INTERVAL_SECONDS", "60")),
    )


def _base_url(feed: str) -> str:
    if feed.startswith(("http://", "https://")):
        from urllib.parse import urlsplit
        parts = urlsplit(feed)
        return f"{parts.scheme}://{parts.netloc}"
    return "https://rplms.polite.edu.sg"

