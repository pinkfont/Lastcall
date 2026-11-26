"""Regression tests for the reliability fixes."""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from duetracker.db import Store
from duetracker.parser import Deliverable, _split_module
from duetracker.service import check_submissions
from duetracker.telegram_bot import (MAX_MESSAGE, Bot, TelegramRejected, fmt_when, history_list,
                                     split_message)
from tests.test_auto_done import FakePolitemall

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 11, 26, 19, 0, tzinfo=SGT)


def item(uid, title, days, kind="assignment", db=10):
    return Deliverable(uid=uid, module_code="C999", module_name="Sample", title=title, kind=kind,
                       due_at=NOW + timedelta(days=days),
                       link=f"https://x/d2l/lms/dropbox/user/folder_submit_files.d2l?ou=1&db={db}")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# 1. Submissions are checked for the full 30 days History shows
def test_old_submitted_work_is_ticked_off(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [item("old", "Week 2", -25, db=20)], NOW - timedelta(days=30))
    result = check_submissions(store, uid, FakePolitemall({(1, 20): True}), NOW)
    assert result.marked == ["C999 Week 2"]


# 2. Only assignments can be ❌; quizzes etc. are "not tracked"
def test_history_labels():
    rows = [{"module": "C1", "title": "A", "kind": "assignment", "done": False,
             "due_at": "2026-11-20T23:59:59+08:00"},
            {"module": "C1", "title": "Q", "kind": "quiz", "done": False,
             "due_at": "2026-11-20T23:59:59+08:00"}]
    text = history_list(rows, SGT)
    assert "❌ <b>C1</b> · A (incomplete)" in text
    assert "⚪ <b>C1</b> · Q (not tracked)" in text


# 3. A slow command doesn't block the next one
def test_slow_command_does_not_freeze_bot():
    order, release = [], asyncio.Event()

    class SlowBot(Bot):
        async def handle_update(self, update):
            if update["update_id"] == 1:
                await release.wait()          # e.g. a long Sync
                order.append("slow")
            else:
                order.append("fast")
                release.set()

    class Updates:
        def __init__(self):
            self.calls = 0

        async def get_updates(self, offset):
            self.calls += 1
            if self.calls == 1:
                return [{"update_id": 1}, {"update_id": 2}]
            await asyncio.sleep(3600)

    async def scenario():
        bot = SlowBot(Updates(), api=None)
        task = asyncio.create_task(bot.poll_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        return order
    assert asyncio.run(scenario()) == ["fast", "slow"]


# 4. One bad alert doesn't block the others; network errors are retried later
class FlakyTelegram:
    def __init__(self, mode):
        self.mode, self.sent = mode, []

    async def send(self, chat_id, text, markup=None):
        if self.mode == "reject_button" and markup:
            raise TelegramRejected("Wrong HTTP URL")
        if self.mode == "reject_all" and "First" in text:
            raise TelegramRejected("Bad Request")
        if self.mode == "network":
            raise httpx.ConnectError("offline")
        self.sent.append((text, markup))


class FakeApi:
    def __init__(self):
        self.acked = []

    async def notifications(self):
        def note(i, title):
            return {"id": i, "kind": "remind_1d", "deliverable": {
                "module": "C1", "title": title, "due_at": "2026-11-27T23:59:59+08:00",
                "link": "https://x/item"}}
        return [note(1, "First"), note(2, "Second")]

    async def ack(self, i):
        self.acked.append(i)


@pytest.mark.parametrize("mode,sent_titles,acked", [
    ("reject_button", ["First", "Second"], [1, 2]),   # resent without the button
    ("reject_all", ["Second"], [1, 2]),                # first skipped, second still sent
])
def test_bad_alert_does_not_block_queue(mode, sent_titles, acked):
    tg, api = FlakyTelegram(mode), FakeApi()
    bot = Bot(tg, api)
    bot.owner = 1
    asyncio.run(bot.deliver_notifications())
    assert [t for t, _ in tg.sent if any(s in t for s in sent_titles)] and api.acked == acked
    assert all(any(s in t for s in sent_titles) for t, _ in tg.sent)


def test_network_error_keeps_alerts_for_later():
    tg, api = FlakyTelegram("network"), FakeApi()
    bot = Bot(tg, api)
    bot.owner = 1
    with pytest.raises(httpx.ConnectError):
        asyncio.run(bot.deliver_notifications())
    assert api.acked == []


# 5. Undoing an auto-ticked item sticks
def test_undo_is_not_reticked(store):
    uid = store.ensure_user("me", "x")
    store.sync(uid, [item("a", "Week 6", 3, db=30)], NOW)
    fake = FakePolitemall({(1, 30): True})
    check_submissions(store, uid, fake, NOW)
    row = store.get_by_uid(uid, "a")
    assert row["done"] and row["done_source"] == "auto"
    store.set_done(uid, row["id"], done=False)          # user: "that's wrong"
    assert check_submissions(store, uid, fake, NOW).marked == []
    assert store.mark_done_by_uids(uid, ["a"]) == []    # laptop push respects it too
    store.set_done(uid, row["id"], done=True)           # but manual marking still works
    assert store.get_by_uid(uid, "a")["done_source"] == "manual"


# 6. Overlapping syncs run once
def test_overlapping_syncs_run_once(tmp_path, monkeypatch):
    import duetracker.api as api_mod
    from duetracker.config import Settings
    from duetracker.service import JobResult
    from fastapi.testclient import TestClient

    calls = []

    def slow_job(store, user_id, feed, tz, now=None):
        calls.append(1)
        time.sleep(0.5)
        return JobResult(in_feed=1)

    monkeypatch.setattr(api_mod, "run_sync_job", slow_job)
    settings = Settings(feed_source="x", db_path=str(tmp_path / "t.db"), user_name="me",
                        timezone="Asia/Singapore", politemall_url="https://x",
                        session_dir=str(tmp_path / ".s"), api_key="k")
    client = TestClient(api_mod.create_app(settings, run_scheduler=False, check_submissions=False))
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        client.post("/sync", headers={"X-API-Key": "k"}).json())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len(results) == 2 and results[0] == results[1]


# 7. Long lists are split without breaking items
def test_split_message_keeps_items_whole():
    blocks = [f"<b>C{i}</b> · Item {i}\nSun 29 Nov, 11:59 PM · in 3d" for i in range(200)]
    chunks = split_message("\n\n".join(blocks))
    assert len(chunks) > 1 and all(len(c) <= MAX_MESSAGE for c in chunks)
    assert sum(c.count("<b>") for c in chunks) == 200
    assert all(c.count("<b>") == c.count("</b>") for c in chunks)


# 8. Times read naturally
def test_time_format():
    assert fmt_when(datetime(2026, 11, 27, 14, 49, tzinfo=SGT)) == "Fri 27 Nov, 2:49 PM"
    assert fmt_when(datetime(2026, 11, 29, 23, 59, tzinfo=SGT)) == "Sun 29 Nov, 11:59 PM"
    assert fmt_when(datetime(2026, 11, 30, 0, 5, tzinfo=SGT)) == "Mon 30 Nov, 12:05 AM"


# 10. Other polytechnics' module codes
@pytest.mark.parametrize("location,code", [
    ("C207 Database Systems", "C207"),
    ("M0001F English Language and Communication", "M0001F"),
    ("IT1001 Programming Essentials", "IT1001"),
    ("CDM1C01 Design Basics", "CDM1C01"),
    ("Student Life Workshop Series 2026", "Student Life Worksho"),
    ("", "Other"),
])
def test_module_codes(location, code):
    assert _split_module(location)[0] == code


# ---- server (remote) mode ---------------------------------------------------------

def _remote_client(tmp_path):
    from fastapi.testclient import TestClient
    from duetracker.api import create_app
    from duetracker.config import Settings
    from tests.test_api import ics
    now = datetime.now(timezone.utc)
    feed = tmp_path / "feed.ics"
    feed.write_text(ics([("u1", "Soon", now + timedelta(days=2))]), encoding="utf-8")
    s = Settings(feed_source=str(feed), db_path=str(tmp_path / "t.db"), user_name="me",
                 timezone="Asia/Singapore", politemall_url="https://x", session_dir=str(tmp_path / ".s"),
                 api_key="k", submission_check="remote")
    return TestClient(create_app(s, run_scheduler=False))


def test_server_mode_waits_for_laptop_then_uses_its_results(tmp_path, monkeypatch):
    import duetracker.api as api_mod
    monkeypatch.setattr(api_mod, "auto_done", lambda *a, **k: pytest.fail("server must not open POLITEMall"))
    c, H = _remote_client(tmp_path), {"X-API-Key": "k"}
    assert c.post("/sync", headers=H).json()["submission_check"] == "waiting_for_laptop"
    st = c.get("/status", headers=H).json()
    assert st["mode"] == "remote" and st["last_push"] is None
    assert c.post("/submissions", headers=H, json={"uids": ["u1"], "status": "expired"}).json()["marked_done"]
    st = c.get("/status", headers=H).json()
    assert st["submission_check"] == "expired" and st["last_push"] is not None
    assert c.post("/sync", headers=H).json()["submission_check"] == "expired"


def test_bot_warns_once_when_laptop_goes_quiet():
    sent = []

    class Tg:
        async def send(self, chat_id, text, markup=None):
            sent.append(text)

    class Api:
        def __init__(self):
            self.last = (datetime.now(SGT) - timedelta(hours=60)).isoformat()

        async def status(self):
            return {"submission_check": "ok", "mode": "remote", "last_push": self.last}

    api = Api()
    bot = Bot(Tg(), api)
    bot.owner = 1
    asyncio.run(bot.warn_if_login_expired())
    asyncio.run(bot.warn_if_login_expired())
    assert sum("haven't heard from your laptop" in t for t in sent) == 1
    api.last = datetime.now(SGT).isoformat()          # laptop back
    asyncio.run(bot.warn_if_login_expired())
    assert bot.warned_quiet is False
