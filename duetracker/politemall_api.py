"""Read your own submissions from POLITEMall (Brightspace) using your own login.

Students can't get official Brightspace API keys, but the POLITEMall website calls
the same API (/d2l/api/...) with your logged-in session. We do the same:

  1. `login` opens a real browser window; you sign in yourself (2FA included).
     The session cookies are saved to SESSION_DIR. Your password is never stored.
  2. During `sync`, a headless browser reuses that session to ask the API whether
     you've submitted to each upcoming assignment.

Only GET requests are made, a few per sync, and only for your own data.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # time you have to finish signing in
REQUEST_PAUSE_SECONDS = 0.4       # be gentle with the school's server
BROWSER_CHANNELS = ("msedge", "chrome", None)  # use an installed browser if possible


class SessionExpired(RuntimeError):
    pass


class NotLoggedIn(RuntimeError):
    pass


def base_url_from(feed_url: str) -> str:
    parts = urlsplit(feed_url)
    return f"{parts.scheme}://{parts.netloc}"


def ids_from_link(link: str | None) -> tuple[int | None, int | None]:
    """Return (course id `ou`, assignment folder id `db`) found in a POLITEMall link."""
    if not link:
        return None, None
    ou = re.search(r"[?&]ou=(\d+)", link)
    db = re.search(r"[?&]db=(\d+)", link)
    return (int(ou.group(1)) if ou else None, int(db.group(1)) if db else None)


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _launch(playwright, **kwargs):
    last_error = None
    for channel in BROWSER_CHANNELS:
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, **kwargs)
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # browser not installed: try the next one
            last_error = exc
    raise RuntimeError(
        "Couldn't start a browser. Install Microsoft Edge or Chrome, or run "
        "`python -m playwright install chromium`."
    ) from last_error


def login(base_url: str, session_dir: str | Path) -> None:
    """Open a visible browser, let the user sign in, then save the session."""
    from playwright.sync_api import sync_playwright

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch(p, headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{base_url}/d2l/home")
        print("A browser window has opened. Sign in to POLITEMall there.")
        print("It will close by itself once you're signed in.")
        # Don't rely on the page URL or on the page going quiet (Brightspace keeps
        # loading in the background). Ask the API directly whether we're signed in.
        deadline = time.monotonic() + LOGIN_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            if _signed_in(context, base_url):
                context.storage_state(path=str(session_dir / "state.json"))
                browser.close()
                return
            page.wait_for_timeout(2000)
        browser.close()
        raise TimeoutError("Didn't detect a sign-in within 5 minutes. Run login again.")


def _signed_in(context, base_url: str) -> bool:
    try:
        resp = context.request.get(f"{base_url}/d2l/api/versions/", max_redirects=0)
        if not resp.ok:
            return False
        lp = next(v["LatestVersion"] for v in json.loads(resp.text()) if v.get("ProductCode") == "lp")
        who = context.request.get(f"{base_url}/d2l/api/lp/{lp}/users/whoami", max_redirects=0)
        return who.ok and "json" in who.headers.get("content-type", "")
    except Exception:
        return False


class SubmissionClient:
    """Headless client that reuses the saved session. Use as a context manager."""

    def __init__(self, base_url: str, session_dir: str | Path):
        self.base_url = base_url
        self.state_file = Path(session_dir) / "state.json"
        self._folders_cache: dict[int, list[dict]] = {}
        self._le_version: str | None = None

    def __enter__(self) -> "SubmissionClient":
        if not self.state_file.exists():
            raise NotLoggedIn("Not logged in yet. Run login.bat (python -m duetracker login).")
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = _launch(self._pw, headless=True)
        self._context = self._browser.new_context(storage_state=str(self.state_file))
        try:
            self._check_session()
        except Exception:
            self._browser.close()
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            # Save refreshed cookies so the session lasts as long as possible.
            self._context.storage_state(path=str(self.state_file))
        except Exception:
            pass
        self._browser.close()
        self._pw.stop()

    # ---- API helpers -------------------------------------------------------

    def _get(self, path: str):
        time.sleep(REQUEST_PAUSE_SECONDS)
        resp = self._context.request.get(f"{self.base_url}{path}", max_redirects=0)
        content_type = resp.headers.get("content-type", "")
        if resp.status in (301, 302, 401) or "text/html" in content_type:
            raise SessionExpired("POLITEMall session expired. Run login.bat again.")
        if resp.status == 403 or resp.status == 404:
            return None  # not visible to you / doesn't exist
        if not resp.ok:
            raise RuntimeError(f"POLITEMall API returned HTTP {resp.status} for {path}")
        return json.loads(resp.text())

    def _check_session(self) -> None:
        versions = self._get("/d2l/api/versions/") or []
        latest = {v.get("ProductCode"): v.get("LatestVersion") for v in versions}
        if not latest.get("le") or not latest.get("lp"):
            raise RuntimeError("POLITEMall did not report its API versions.")
        self._le_version = latest["le"]
        # The versions list is public; whoami only works with a valid session.
        if self._get(f"/d2l/api/lp/{latest['lp']}/users/whoami") is None:
            raise SessionExpired("POLITEMall session expired. Run login.bat again.")

    def _folders(self, ou: int) -> list[dict]:
        if ou not in self._folders_cache:
            self._folders_cache[ou] = self._get(f"/d2l/api/le/{self._le_version}/{ou}/dropbox/folders/") or []
        return self._folders_cache[ou]

    # ---- public ------------------------------------------------------------

    def find_folder_id(self, ou: int, title: str) -> int | None:
        """For links without a folder id: match the assignment by name within the course."""
        wanted = _normalise(title)
        for folder in self._folders(ou):
            if _normalise(folder.get("Name", "")) == wanted:
                return folder.get("Id")
        return None

    def has_submitted(self, ou: int, db: int) -> bool | None:
        """True/False if known, None if POLITEMall won't say (e.g. no access)."""
        data = self._get(f"/d2l/api/le/{self._le_version}/{ou}/dropbox/folders/{db}/submissions/mysubmissions/")
        if data is None:
            return None
        entries = data if isinstance(data, list) else [data]
        return any(entry.get("Submissions") for entry in entries if isinstance(entry, dict))
