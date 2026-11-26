"""Bot tests: fake Telegram, real duetracker API running in-process."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from duetracker.api import create_app
from duetracker.config import Settings
from duetracker.telegram_bot import Bot, DuetrackerAPI, list_message
from tests.test_api import ics

KEY = "k"
OWNER, STRANGER = 111, 999


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return {"message_id": len(self.sent)}

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append({"chat_id": chat_id, "text": text, "buttons": buttons})

    async def answer(self, callback_id, text=""):
        self.answers.append(text)


def msg(chat_id, text, update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def button(chat_id, data, text):
    return {"update_id": 9, "callback_query": {"id": "cb", "data": data, "message": {
        "chat": {"id": chat_id}, "message_id": 1, "text": text}}}


@pytest.fixture
def world(tmp_path):
    now = datetime.now(timezone.utc)
    feed = tmp_path / "feed.ics"
    feed.write_text(ics([("u1", "Soon", now + timedelta(hours=20)),
                         ("u2", "Next week", now + timedelta(days=6))]), encoding="utf-8")
    settings = Settings(feed_source=str(feed), db_path=str(tmp_path / "t.db"), user_name="me",
                        timezone="Asia/Singapore", politemall_url="https://lms.example.edu",
                        session_dir=str(tmp_path / ".s"), api_key=KEY)
    app = create_app(settings, run_scheduler=False)

    async def make():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
        api = DuetrackerAPI("http://api", KEY, http)
        await api.sync()
        tg = FakeTelegram()
        bot = Bot(tg, api)
        await bot.load_owner()
        return bot, tg, http
    return make


def run(coro):
    return asyncio.run(coro)


def test_first_start_claims_ownership_and_strangers_are_refused(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        await bot.handle_update(msg(STRANGER, "/start"))
        await bot.handle_update(msg(STRANGER, "/due"))
        await http.aclose()
        return bot, tg
    bot, tg = run(scenario())
    assert bot.owner == OWNER
    assert "now the owner" in tg.sent[0]["text"]
    assert tg.sent[1] == {"chat_id": STRANGER, "text": "Sorry, this is a private bot.", "buttons": None}
    assert len(tg.sent) == 2  # stranger's /due is ignored


def test_owner_survives_restart(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        fresh = Bot(FakeTelegram(), bot.api)
        await fresh.load_owner()
        await http.aclose()
        return fresh
    assert run(scenario()).owner == OWNER


def test_due_lists_items_with_buttons(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        await bot.handle_update(msg(OWNER, "/due"))
        await http.aclose()
        return tg.sent[-1]
    out = run(scenario())
    assert "2 due in the next 7 days" in out["text"]
    assert "Soon" in out["text"] and "Next week" in out["text"]
    assert out["buttons"][0][0]["text"] == "🔗 1" and out["buttons"][0][1]["callback_data"].startswith("done:")


def test_done_button_marks_item_and_refreshes_list(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        await bot.handle_update(msg(OWNER, "/due"))
        first_id = tg.sent[-1]["buttons"][0][1]["callback_data"]
        await bot.handle_update(button(OWNER, first_id, "2 due in the next 7 days\n1. C999 Soon"))
        remaining = await bot.api.deliverables(7)
        await http.aclose()
        return tg, remaining
    tg, remaining = run(scenario())
    assert tg.answers == ["Marked done ✅"]
    assert "1 due in the next 7 days" in tg.edits[-1]["text"]
    assert [i["title"] for i in remaining] == ["Next week"]


def test_done_and_undo_commands(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        item = (await bot.api.deliverables(7))[0]
        await bot.handle_update(msg(OWNER, f"/done {item['id']}"))
        await bot.handle_update(msg(OWNER, f"/undo #{item['id']}"))
        await bot.handle_update(msg(OWNER, "/done 9999"))
        await bot.handle_update(msg(OWNER, "/done"))
        await http.aclose()
        return [s["text"] for s in tg.sent[1:]]
    texts = run(scenario())
    assert texts[0].startswith("✅ Marked done") and texts[1].startswith("↩️ Unmarked")
    assert texts[2] == "There's no deliverable #9999."
    assert texts[3].startswith("Usage:")


def test_notifications_are_sent_once_with_buttons(world):
    async def scenario():
        bot, tg, http = await world()
        assert await bot.deliver_notifications() == 0  # no owner yet: nothing sent
        await bot.handle_update(msg(OWNER, "/start"))
        first = await bot.deliver_notifications()
        second = await bot.deliver_notifications()
        await http.aclose()
        return tg, first, second
    tg, first, second = run(scenario())
    assert (first, second) == (1, 0)
    note = tg.sent[-1]
    assert note["chat_id"] == OWNER and "Soon" in note["text"] and note["text"].startswith("<b>")
    assert note["buttons"][0][-1]["callback_data"].startswith("done:")


def test_sync_command(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        await bot.handle_update(msg(OWNER, "/sync"))
        await http.aclose()
        return tg.sent[-1]["text"]
    assert run(scenario()).startswith("Done: 2 deliverables in feed")


def test_empty_list_message():
    from zoneinfo import ZoneInfo
    text, buttons = list_message([], 7, ZoneInfo("Asia/Singapore"), datetime.now(ZoneInfo("Asia/Singapore")))
    assert text.startswith("Nothing due") and buttons is None


def test_expired_login_warns_once(world, monkeypatch):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))

        async def expired():
            return {"last_sync": None, "submission_check": "expired"}
        bot.api.status = expired
        await bot.warn_if_login_expired()
        await bot.warn_if_login_expired()
        await http.aclose()
        return [s["text"] for s in tg.sent if "expired" in s["text"]]
    assert len(run(scenario())) == 1


def test_submitted_notification_heading(world):
    async def scenario():
        bot, tg, http = await world()
        await bot.handle_update(msg(OWNER, "/start"))
        await bot.api.done((await bot.api.deliverables(7))[0]["id"])  # manual: no alert
        await bot.api._req("POST", "/submissions", json={"uids": ["u2"]})  # laptop push: alert
        await bot.deliver_notifications()
        await http.aclose()
        return [s["text"] for s in tg.sent]
    texts = run(scenario())
    assert any(t.startswith("<b>✅ Submitted on POLITEMall</b>") and "Next week" in t for t in texts)
