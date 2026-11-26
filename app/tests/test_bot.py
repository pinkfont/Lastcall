"""Bot tests: fake Telegram, real duetracker API running in-process."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from duetracker.api import create_app
from duetracker.config import Settings
from duetracker.telegram_bot import (BTN_COMPLETE, BTN_DUE, BTN_DUE30, BTN_HISTORY, BTN_RENAME,
                                     BTN_SYNC, BTN_UNDO, MENU, Bot, DuetrackerAPI, due_list)
from tests.test_api import ics

KEY = "k"
OWNER, STRANGER = 111, 999
SGT = ZoneInfo("Asia/Singapore")


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    async def send(self, chat_id, text, markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": markup})
        return {"message_id": len(self.sent)}

    async def edit(self, chat_id, message_id, text, markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "markup": markup})

    async def answer(self, callback_id, text=""):
        self.answers.append(text)


def msg(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def tap(chat_id, data, message_id=1):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "message": {
        "chat": {"id": chat_id}, "message_id": message_id, "text": ""}}}


@pytest.fixture
def world(tmp_path):
    now = datetime.now(timezone.utc)
    feed = tmp_path / "feed.ics"
    feed.write_text(ics([("u1", "Soon", now + timedelta(hours=20)),
                         ("u2", "Next week", now + timedelta(days=6)),
                         ("u3", "Later", now + timedelta(days=20)),
                         ("u0", "Missed", now - timedelta(days=3))]), encoding="utf-8")
    settings = Settings(feed_source=str(feed), db_path=str(tmp_path / "t.db"), user_name="me",
                        timezone="Asia/Singapore", politemall_url="https://lms.example.edu",
                        session_dir=str(tmp_path / ".s"), api_key=KEY)
    app = create_app(settings, run_scheduler=False)

    async def make(start=True):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
        api = DuetrackerAPI("http://api", KEY, http)
        await api.sync()
        tg = FakeTelegram()
        bot = Bot(tg, api)
        await bot.load_owner()
        if start:
            await bot.handle_update(msg(OWNER, "/start"))
        return bot, tg, http
    return make


def run(scenario, world, start=True):
    async def wrapper():
        bot, tg, http = await world(start)
        try:
            return await scenario(bot, tg)
        finally:
            await http.aclose()
    return asyncio.run(wrapper())


# ---- ownership & onboarding -------------------------------------------------------

def test_first_start_claims_ownership_shows_menu_and_offers_rename(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, "/start"))
        await bot.handle_update(msg(STRANGER, "/start"))
        await bot.handle_update(msg(STRANGER, BTN_DUE))
        return bot, tg
    bot, tg = run(s, world, start=False)
    assert bot.owner == OWNER
    assert "now the owner" in tg.sent[0]["text"] and tg.sent[0]["markup"] == MENU
    assert tg.sent[1]["markup"]["inline_keyboard"][0][0]["callback_data"] == "rename"
    assert tg.sent[2]["text"] == "Sorry, this is a private bot."
    assert len(tg.sent) == 3  # the stranger's Due tap is ignored


# ---- lists --------------------------------------------------------------------------

def test_due_is_a_clean_list_without_item_buttons(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_DUE))
        return tg.sent[-1]
    out = run(s, world)
    assert "Due in the next 7 days (2)" in out["text"]
    assert "Soon" in out["text"] and "Next week" in out["text"] and "Later" not in out["text"]
    assert "#" not in out["text"]          # no ids
    assert out["markup"] == MENU           # only the menu, no ✅/🔗 per item


def test_due_30_and_slash_commands(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_DUE30))
        await bot.handle_update(msg(OWNER, "/due 30"))
        await bot.handle_update(msg(OWNER, "/due30"))
        return [m["text"] for m in tg.sent[-3:]]
    for text in run(s, world):
        assert "Due in the next 30 days (3)" in text


def test_sync_shows_status_then_fresh_due_list(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_SYNC))
        return tg
    tg = run(s, world)
    assert tg.sent[-2]["text"].startswith("🔄 Syncing")
    assert tg.edits[-1]["text"].startswith("✔️ Synced.")
    assert "Submissions aren't checked yet" in tg.edits[-1]["text"]  # not logged in, in tests
    assert "Due in the next 7 days" in tg.sent[-1]["text"]


def test_history_shows_completed_and_incomplete(world):
    async def s(bot, tg):
        soon = (await bot.api.deliverables(7))[0]
        await bot.api.done(soon["id"])
        await bot.api._req("POST", "/submissions", json={"uids": ["u2"]})
        await bot.handle_update(msg(OWNER, BTN_HISTORY))
        return tg.sent[-1]["text"]
    text = run(s, world)
    assert "✅ <b>C999</b> · Soon (manual)" in text
    assert "✅ <b>C999</b> · Next week (auto)" in text
    assert "❌ <b>C999</b> · Missed (incomplete)" in text


# ---- pickers ------------------------------------------------------------------------

def test_mark_complete_picker(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_COMPLETE))
        rows = tg.sent[-1]["markup"]["inline_keyboard"]
        soon = next(r[0] for r in rows if "Soon" in r[0]["text"])
        await bot.handle_update(tap(OWNER, soon["callback_data"]))
        remaining = [i["title"] for i in await bot.api.deliverables(7)]
        return rows, tg, remaining
    rows, tg, remaining = run(s, world)
    labels = [r[0]["text"] for r in rows]
    assert any(label.startswith("C999 · Missed") for label in labels)  # recently overdue included
    assert labels[-1] == "✖️ Cancel"
    assert tg.edits[-1]["text"].startswith("✅ Marked complete: <b>C999</b> · Soon")
    assert tg.edits[-1]["markup"] is None  # picker buttons removed
    assert remaining == ["Next week"]


def test_undo_picker(world):
    async def s(bot, tg):
        soon = (await bot.api.deliverables(7))[0]
        await bot.api.done(soon["id"])
        await bot.handle_update(msg(OWNER, BTN_UNDO))
        rows = tg.sent[-1]["markup"]["inline_keyboard"]
        await bot.handle_update(tap(OWNER, rows[0][0]["callback_data"]))
        return tg, [i["title"] for i in await bot.api.deliverables(7)]
    tg, remaining = run(s, world)
    assert tg.edits[-1]["text"].startswith("↩️ Unmarked: <b>C999</b> · Soon")
    assert remaining == ["Soon", "Next week"]


def test_empty_undo_picker(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_UNDO))
        return tg.sent[-1]["text"]
    assert run(s, world) == "Nothing completed in the last 30 days."


def test_cancel_removes_picker(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_COMPLETE))
        await bot.handle_update(tap(OWNER, "cancel"))
        return tg.edits[-1]
    assert run(s, world)["text"] == "Okay, cancelled."


# ---- renaming -----------------------------------------------------------------------

def test_rename_module_applies_everywhere_and_can_reset(world):
    async def s(bot, tg):
        await bot.handle_update(msg(OWNER, BTN_RENAME))
        rows = tg.sent[-1]["markup"]["inline_keyboard"]
        await bot.handle_update(tap(OWNER, rows[0][0]["callback_data"]))  # rename:C999
        await bot.handle_update(msg(OWNER, "  Database   Systems "))
        confirm = tg.sent[-1]["text"]
        await bot.handle_update(msg(OWNER, BTN_DUE))
        due = tg.sent[-1]["text"]
        await bot.handle_update(msg(OWNER, BTN_COMPLETE))
        picker_label = tg.sent[-1]["markup"]["inline_keyboard"][0][0]["text"]
        notes = await bot.api.notifications()
        # reset
        await bot.handle_update(msg(OWNER, "/rename"))
        await bot.handle_update(tap(OWNER, "rename:C999"))
        await bot.handle_update(msg(OWNER, "-"))
        after = [m["module"] for m in await bot.api.deliverables(30)]
        return rows, confirm, due, picker_label, notes, after
    rows, confirm, due, picker_label, notes, after = run(s, world)
    assert rows[0][0]["text"] == "C999 · Sample Module"
    assert confirm == "✏️ <b>C999</b> will now show as <b>Database Systems</b>."
    assert "<b>Database Systems</b> · Soon" in due and "C999" not in due
    assert picker_label.startswith("Database Systems · ")
    assert all(n["deliverable"]["module"] == "Database Systems" for n in notes)
    assert set(after) == {"C999"}


def test_menu_button_cancels_pending_rename(world):
    async def s(bot, tg):
        await bot.handle_update(tap(OWNER, "rename:C999"))
        await bot.handle_update(msg(OWNER, BTN_DUE))  # changed their mind
        return bot.renaming, (await bot.api.modules())[0]["alias"]
    assert run(s, world) == (None, None)


def test_onboarding_rename_button_opens_module_picker(world):
    async def s(bot, tg):
        await bot.handle_update(tap(OWNER, "rename"))
        return tg.sent[-1]
    out = run(s, world)
    assert "Which module" in out["text"]


# ---- notifications ------------------------------------------------------------------

def test_reminders_have_only_an_open_link(world):
    async def s(bot, tg):
        first = await bot.deliver_notifications()
        second = await bot.deliver_notifications()
        return tg, first, second
    tg, first, second = run(s, world)
    assert (first, second) == (1, 0)
    note = tg.sent[-1]
    assert note["text"].startswith("<b>⏰") or note["text"].startswith("<b>🔴")
    buttons = note["markup"]["inline_keyboard"]
    assert buttons == [[{"text": "🔗 Open in POLITEMall", "url": buttons[0][0]["url"]}]]


def test_no_notifications_before_owner(world):
    async def s(bot, tg):
        return await bot.deliver_notifications()
    assert run(s, world, start=False) == 0


def test_expired_login_warns_once(world):
    async def s(bot, tg):
        async def expired():
            return {"last_sync": None, "submission_check": "expired"}
        bot.api.status = expired
        await bot.warn_if_login_expired()
        await bot.warn_if_login_expired()
        return [m["text"] for m in tg.sent if "expired" in m["text"]]
    assert len(run(s, world)) == 1


def test_submitted_notification(world):
    async def s(bot, tg):
        await bot.deliver_notifications()  # clear the initial reminder
        await bot.api._req("POST", "/submissions", json={"uids": ["u2"]})
        await bot.deliver_notifications()
        return tg.sent[-1]["text"]
    assert run(s, world).startswith("<b>✅ Submitted on POLITEMall</b>\n<b>C999</b> · Next week")


def test_empty_due_list():
    now = datetime.now(SGT)
    assert due_list([], 7, SGT, now) == "📋 Nothing due in the next 7 days 🎉"
