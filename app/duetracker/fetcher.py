"""Download the calendar feed (or read it from a local file during development)."""

from __future__ import annotations

import re
from pathlib import Path

import requests

TIMEOUT_SECONDS = 30


class FeedError(RuntimeError):
    pass


def mask_url(url: str) -> str:
    """Hide the private token so it never ends up in logs or error messages."""
    return re.sub(r"(token=)[^&]+", r"\1***", url)


def fetch_feed(source: str) -> str:
    """`source` is either an https:// feed URL or a path to a local .ics file."""
    if source.startswith(("http://", "https://")):
        try:
            resp = requests.get(
                source,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": "Lastcall/0.1 (personal deadline tracker)"},
            )
        except requests.RequestException as exc:
            raise FeedError(f"Could not reach {mask_url(source)}: {exc.__class__.__name__}") from None
        if resp.status_code != 200:
            raise FeedError(
                f"Feed returned HTTP {resp.status_code} for {mask_url(source)}. "
                "If you pressed Reset in POLITEMall, update FEED_URL in config/.env."
            )
        text = resp.text
    else:
        path = Path(source).expanduser()
        if not path.exists():
            raise FeedError(f"Feed file not found: {path}")
        text = path.read_text(encoding="utf-8")

    if "BEGIN:VCALENDAR" not in text[:500]:
        hint = ""
        if source.startswith(("http://", "https://")):
            _save_debug(resp, text)
            if "<html" in text[:2000].lower():
                hint = (" POLITEMall sent back a web page (usually the login page), which normally "
                        "means the feed token is no longer valid. Copy a fresh link from "
                        "Calendar > Subscribe into FEED_URL in config/.env.")
            hint += f" Details saved to data/{DEBUG_FILE.name}."
        raise FeedError("That doesn't look like a calendar feed (no BEGIN:VCALENDAR)." + hint)
    return text


from .paths import DATA_DIR  # noqa: E402

DEBUG_FILE = DATA_DIR / "last_response_debug.txt"


def _save_debug(resp, text: str) -> None:
    """Keep what the server returned (token masked) so a failed sync can be diagnosed."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        Path(DEBUG_FILE).write_text(
            f"final url: {mask_url(resp.url)}\n"
            f"status: {resp.status_code}\n"
            f"content-type: {resp.headers.get('Content-Type')}\n"
            f"redirects: {[mask_url(r.url) for r in resp.history]}\n\n"
            + text[:3000],
            encoding="utf-8",
        )
    except OSError:
        pass
