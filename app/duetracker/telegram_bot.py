"""Telegram bot: a client of the duetracker REST API.

It talks to Telegram's Bot API directly (long polling) and to the duetracker API
over HTTP, exactly like any other client would, so it can later run on its own.

Menu (buttons pinned under the chat, also in the "/" menu):
    📋 Due · 📅 Due 30 · 🔄 Sync · 🕘 History · ✅ Mark complete · ↩️ Undo mark · ✏️ Rename module

Lists are plain text. Buttons only appear where you're choosing something
(the Mark complete / Undo mark / Rename pickers) and as 🔗 Open on reminders.

Only one chat can use the bot: the first one to send /start becomes the owner.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("duetracker.bot")

PICKER_LIMIT = 20

# Menu button text -> action. Slash commands map to the same actions.
BTN_DUE, BTN_DUE30, BTN_SYNC = "📋 Due", "📅 Due 30", "🔄 Sync"
BTN_HISTORY, BTN_COMPLETE, BTN_UNDO, BTN_RENAME = "🕘 History", "✅ Mark complete", "↩️ Undo mark", "✏️ Rename module"
MENU = {"keyboard": [[{"text": BTN_DUE}, {"text": BTN_DUE30}],
                     [{"text": BTN_SYNC}, {"text": BTN_HISTORY}],
                     [{"text": BTN_COMPLETE}, {"text": BTN_UNDO}],
                     [{"text": BTN_RENAME}]],
        "resize_keyboard": True, "is_persistent": True}
ACTIONS = {
    BTN_DUE: "due", "/due": "due",
    BTN_DUE30: "due30", "/due30": "due30",
    BTN_SYNC: "sync", "/sync": "sync",
    BTN_HISTORY: "history", "/history": "history",
    BTN_COMPLETE: "complete", "/complete": "complete",
    BTN_UNDO: "undo", "/undo": "undo",
    BTN_RENAME: "rename", "/rename": "rename",
    "/help": "help", "/menu": "help",
}
COMMANDS = [  # shown in Telegram's "/" menu
    {"command": "due", "description": "What's due in the next 7 days"},
    {"command": "due30", "description": "What's due in the next 30 days"},
    {"command": "sync", "description": "Fetch the latest from POLITEMall"},
    {"command": "history", "description": "Completed and missed, last 30 days"},
    {"command": "complete", "description": "Mark something as complete"},
    {"command": "undo", "description": "Undo a completed mark"},
    {"command": "rename", "description": "Give a module a friendlier name"},
]

HEADINGS = {
    "remind_3d": "⏰ Due in 3 days",
    "remind_1d": "⏰ Due tomorrow",
    "remind_morning": "🔴 Due today",
    "new": "🆕 New deliverable",
    "submitted": "✅ Submitted on POLITEMall",
}
HELP = (
    "<b>duetracker</b>: your POLITEMall deadlines\n\n"
    "📋 <b>Due</b>: next 7 days  ·  📅 <b>Due 30</b>: next 30 days\n"
    "🔄 <b>Sync</b>: fetch from POLITEMall now, then show what's due\n"
    "🕘 <b>History</b>: completed and missed in the last 30 days\n"
    "✅ <b>Mark complete</b> / ↩️ <b>Undo mark</b>: pick an item\n"
    "✏️ <b>Rename module</b>: e.g. C207 → Database\n\n"
    "I'll also message you at 8 AM 3 days before, the day before and on the day "
    "something is due, and when a deadline is added, moved or submitted."
)
EXPIRED_WARNING = ("⚠️ Your POLITEMall login has expired, so I can't see your submissions. "
                   "On your laptop, double-click <b>login</b> and sign in again.")


# ---- thin HTTP clients ---------------------------------------------------------

class TelegramAPI:
    def __init__(self, token: str, http: httpx.AsyncClient):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = http

    async def call(self, method: str, **params):
        resp = await self.http.post(f"{self.base}/{method}", json=params, timeout=60)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    async def get_updates(self, offset: int | None, timeout: int = 30):
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"])

    async def send(self, chat_id: int, text: str, markup=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = markup
        return await self.call("sendMessage", **params)

    async def edit(self, chat_id: int, message_id: int, text: str, markup=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = markup
        return await self.call("editMessageText", **params)

    async def answer(self, callback_id: str, text: str = ""):
        return await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def set_commands(self, commands: list[dict]):
        return await self.call("setMyCommands", commands=commands)


class DuetrackerAPI:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient):
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.http = http

    async def _req(self, method: str, path: str, **kw):
        resp = await self.http.request(method, f"{self.base}{path}", headers=self.headers,
                                       timeout=180, **kw)
        resp.raise_for_status()
        return resp.json()

    async def deliverables(self, days: int = 7):
        return await self._req("GET", "/deliverables", params={"days": days})

    async def history(self, days: int = 30):
        return await self._req("GET", "/history", params={"days": days})

    async def complete_choices(self):
        return await self._req("GET", "/pickers/complete")

    async def undo_choices(self):
        return await self._req("GET", "/pickers/undo")

    async def done(self, item_id: int):
        return await self._req("POST", f"/deliverables/{item_id}/done")

    async def undo(self, item_id: int):
        return await self._req("POST", f"/deliverables/{item_id}/undo")

    async def sync(self):
        return await self._req("POST", "/sync")

    async def status(self):
        return await self._req("GET", "/status")

    async def modules(self):
        return await self._req("GET", "/modules")

    async def rename(self, code: str, alias: str):
        return await self._req("PUT", f"/modules/{code}/alias", json={"alias": alias})

    async def reset_name(self, code: str):
        return await self._req("DELETE", f"/modules/{code}/alias")

    async def notifications(self):
        return await self._req("GET", "/notifications")

    async def ack(self, notification_id: int):
        return await self._req("POST", f"/notifications/{notification_id}/ack")

    async def owner(self) -> int | None:
        return (await self._req("GET", "/telegram/owner"))["chat_id"]

    async def claim(self, chat_id: int) -> bool:
        try:
            await self._req("POST", "/telegram/owner", json={"chat_id": chat_id})
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return False
            raise


# ---- formatting ------------------------------------------------------------------

def time_left(due_at: datetime, now: datetime) -> str:
    secs = (due_at - now).total_seconds()
    if secs < 0:
        return "overdue"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"in {days}d {hours}h"
    return f"in {hours}h {rem // 60}m"


def _due(item: dict, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(item["due_at"]).astimezone(tz)


def _name(item: dict) -> str:
    return f"<b>{html.escape(item['module'])}</b> · {html.escape(item['title'])}"


def due_list(items: list[dict], days: int, tz: ZoneInfo, now: datetime) -> str:
    if not items:
        return f"📋 Nothing due in the next {days} days 🎉"
    blocks = [f"📋 <b>Due in the next {days} days ({len(items)})</b>"]
    for item in items:
        due = _due(item, tz)
        blocks.append(f"{_name(item)}\n{due:%a %d %b, %I:%M %p} · {time_left(due, now)}")
    return "\n\n".join(blocks)


def history_list(items: list[dict], tz: ZoneInfo) -> str:
    if not items:
        return "🕘 Nothing completed or missed in the last 30 days."
    lines = ["🕘 <b>Last 30 days</b>"]
    for item in items:
        due = _due(item, tz)
        if item["done"]:
            how = "auto" if item.get("done_source") == "auto" else "manual"
            lines.append(f"✅ {_name(item)} ({how})\n      due {due:%a %d %b}")
        else:
            lines.append(f"❌ {_name(item)} (incomplete)\n      was due {due:%a %d %b}")
    return "\n\n".join(lines)


def reminder_text(heading: str, item: dict, tz: ZoneInfo, now: datetime) -> str:
    due = _due(item, tz)
    return (f"<b>{html.escape(heading)}</b>\n{_name(item)}\n"
            f"{due:%a %d %b, %I:%M %p} · {time_left(due, now)}")


def link_button(item: dict):
    if not item.get("link"):
        return None
    return {"inline_keyboard": [[{"text": "🔗 Open in POLITEMall", "url": item["link"]}]]}


def picker(items: list[dict], action: str, tz: ZoneInfo):
    """One button per item: 'Database · Week 6 Deliverables · 29 Nov'."""
    rows = []
    for item in items[:PICKER_LIMIT]:
        label = f"{item['module']} · {item['title']}"
        label = label if len(label) <= 48 else label[:47] + "…"
        rows.append([{"text": f"{label} · {_due(item, tz):%d %b}",
                      "callback_data": f"{action}:{item['id']}"}])
    rows.append([{"text": "✖️ Cancel", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def notification_heading(kind: str) -> str:
    if kind.startswith("rescheduled"):
        return "📅 Deadline moved"
    return HEADINGS.get(kind, "📌 Update")


# ---- the bot ---------------------------------------------------------------------

class Bot:
    def __init__(self, tg: TelegramAPI, api: DuetrackerAPI, tz: str = "Asia/Singapore"):
        self.tg, self.api, self.tz = tg, api, ZoneInfo(tz)
        self.owner: int | None = None
        self.warned_expired = False
        self.renaming: str | None = None  # module code waiting for a new name

    def now(self) -> datetime:
        return datetime.now(self.tz)

    async def load_owner(self) -> None:
        self.owner = await self.api.owner()

    # -- incoming --

    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._on_button(update["callback_query"])
        elif "message" in update and "text" in update["message"]:
            await self._on_message(update["message"])

    async def _on_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        first = text.split()[0].split("@")[0].lower() if text.startswith("/") else text

        if first == "/start":
            if self.owner is None and await self.api.claim(chat_id):
                self.owner = chat_id
                await self.tg.send(chat_id, "👋 You're now the owner of this bot.\n\n" + HELP, MENU)
                await self.tg.send(
                    chat_id, "Want friendlier module names, like <b>Database</b> instead of <b>C207</b>?",
                    {"inline_keyboard": [[{"text": "✏️ Rename modules", "callback_data": "rename"},
                                          {"text": "Maybe later", "callback_data": "cancel"}]]})
            elif chat_id == self.owner:
                await self.tg.send(chat_id, HELP, MENU)
            else:
                await self.tg.send(chat_id, "Sorry, this is a private bot.")
            return

        if chat_id != self.owner:
            return  # ignore strangers silently

        action = ACTIONS.get(first)
        if first == "/due" and text.split()[1:2] == ["30"]:
            action = "due30"
        if action is None and self.renaming:
            await self._finish_rename(chat_id, text)
            return
        self.renaming = None  # any menu action cancels a pending rename

        if action == "due":
            await self.tg.send(chat_id, due_list(await self.api.deliverables(7), 7, self.tz, self.now()), MENU)
        elif action == "due30":
            await self.tg.send(chat_id, due_list(await self.api.deliverables(30), 30, self.tz, self.now()), MENU)
        elif action == "sync":
            await self._sync(chat_id)
        elif action == "history":
            await self.tg.send(chat_id, history_list(await self.api.history(30), self.tz), MENU)
        elif action == "complete":
            await self._show_picker(chat_id, "complete")
        elif action == "undo":
            await self._show_picker(chat_id, "undo")
        elif action == "rename":
            await self._show_modules(chat_id)
        elif action == "help":
            await self.tg.send(chat_id, HELP, MENU)
        else:
            await self.tg.send(chat_id, "Use the buttons below 👇", MENU)

    async def _sync(self, chat_id: int) -> None:
        status_msg = await self.tg.send(chat_id, "🔄 Syncing with POLITEMall… this can take a minute.")
        try:
            r = await self.api.sync()
        except httpx.HTTPStatusError as exc:
            await self.tg.edit(chat_id, status_msg["message_id"],
                               f"⚠️ Sync failed: {html.escape(exc.response.text[:300])}")
            return
        notes = []
        if r.get("submitted"):
            notes.append(f"✅ {r['submitted']} submission(s) detected on POLITEMall.")
        if r.get("new"):
            notes.append(f"🆕 {r['new']} new deliverable(s).")
        if r.get("submission_check") == "expired":
            notes.append(EXPIRED_WARNING)
        elif r.get("submission_check") == "not_logged_in":
            notes.append("Submissions aren't checked yet: double-click <b>login</b> on your laptop.")
        header = "✔️ Synced." + ("\n" + "\n".join(notes) if notes else "")
        await self.tg.edit(chat_id, status_msg["message_id"], header)
        await self.tg.send(chat_id, due_list(await self.api.deliverables(7), 7, self.tz, self.now()), MENU)

    async def _show_picker(self, chat_id: int, kind: str) -> None:
        if kind == "complete":
            items = await self.api.complete_choices()
            empty, prompt, action = ("Nothing to mark: you're all caught up 🎉",
                                     "✅ <b>Which one did you complete?</b>", "done")
        else:
            items = await self.api.undo_choices()
            empty, prompt, action = ("Nothing completed in the last 30 days.",
                                     "↩️ <b>Which one should be unmarked?</b>", "undo")
        if not items:
            await self.tg.send(chat_id, empty, MENU)
            return
        await self.tg.send(chat_id, prompt, picker(items, action, self.tz))

    async def _show_modules(self, chat_id: int) -> None:
        mods = await self.api.modules()
        if not mods:
            await self.tg.send(chat_id, "No modules found yet. Try 🔄 Sync first.", MENU)
            return
        rows = []
        for m in mods:
            label = f"{m['alias']} ({m['code']})" if m["alias"] else f"{m['code']} · {m['name']}"
            rows.append([{"text": label[:60], "callback_data": f"rename:{m['code']}"}])
        rows.append([{"text": "✖️ Cancel", "callback_data": "cancel"}])
        await self.tg.send(chat_id, "✏️ <b>Which module do you want to rename?</b>", {"inline_keyboard": rows})

    async def _finish_rename(self, chat_id: int, text: str) -> None:
        code, self.renaming = self.renaming, None
        if text == "-":
            await self.api.reset_name(code)
            await self.tg.send(chat_id, f"↩️ <b>{html.escape(code)}</b> is back to its original code.", MENU)
            return
        name = " ".join(text.split())[:30]
        await self.api.rename(code, name)
        await self.tg.send(chat_id, f"✏️ <b>{html.escape(code)}</b> will now show as "
                                    f"<b>{html.escape(name)}</b>.", MENU)

    async def _on_button(self, query: dict) -> None:
        chat_id = query["message"]["chat"]["id"]
        message_id = query["message"]["message_id"]
        if chat_id != self.owner:
            await self.tg.answer(query["id"])
            return
        action, _, value = query.get("data", "").partition(":")

        if action == "cancel":
            self.renaming = None
            await self.tg.answer(query["id"])
            await self.tg.edit(chat_id, message_id, "Okay, cancelled.")
            return

        if action == "rename" and not value:  # "Rename modules" from onboarding
            await self.tg.answer(query["id"])
            await self.tg.edit(chat_id, message_id, "Let's do it 👇")
            await self._show_modules(chat_id)
            return

        if action == "rename":
            self.renaming = value
            await self.tg.answer(query["id"])
            await self.tg.edit(chat_id, message_id,
                               f"✏️ Send the new name for <b>{html.escape(value)}</b> (up to 30 characters).\n"
                               f"Send <b>-</b> to go back to the original code.")
            return

        if action in ("done", "undo") and value.isdigit():
            try:
                item = await (self.api.done(int(value)) if action == "done" else self.api.undo(int(value)))
            except httpx.HTTPStatusError:
                await self.tg.answer(query["id"], "That deliverable no longer exists.")
                return
            await self.tg.answer(query["id"], "Marked complete ✅" if action == "done" else "Unmarked")
            verb = "✅ Marked complete" if action == "done" else "↩️ Unmarked"
            await self.tg.edit(chat_id, message_id, f"{verb}: {_name(item)}")
            return

        await self.tg.answer(query["id"])

    # -- outgoing --

    async def warn_if_login_expired(self) -> None:
        """Tell the owner once when the POLITEMall session runs out."""
        if self.owner is None:
            return
        check = (await self.api.status())["submission_check"]
        if check == "expired" and not self.warned_expired:
            await self.tg.send(self.owner, EXPIRED_WARNING)
            self.warned_expired = True
        elif check == "ok":
            self.warned_expired = False

    async def deliver_notifications(self) -> int:
        if self.owner is None:
            return 0
        sent = 0
        for note in await self.api.notifications():
            item = note["deliverable"]
            await self.tg.send(self.owner, reminder_text(notification_heading(note["kind"]), item,
                                                         self.tz, self.now()),
                               link_button(item))
            await self.api.ack(note["id"])
            sent += 1
        return sent

    # -- loops --

    async def poll_loop(self) -> None:
        offset = None
        while True:
            try:
                for update in await self.tg.get_updates(offset):
                    offset = update["update_id"] + 1
                    try:
                        await self.handle_update(update)
                    except Exception as exc:
                        log.warning("error handling update: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("polling Telegram failed: %s", exc)
                await asyncio.sleep(5)

    async def notify_loop(self, interval_seconds: int) -> None:
        while True:
            try:
                if self.owner is None:
                    await self.load_owner()
                sent = await self.deliver_notifications()
                await self.warn_if_login_expired()
                if sent:
                    log.info("sent %d notification(s)", sent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("delivering notifications failed: %s", exc)
            await asyncio.sleep(interval_seconds)


async def run_bot(token: str, api_url: str, api_key: str, tz: str,
                  notify_interval_seconds: int = 60) -> None:
    """Run the bot until cancelled. Waits for the API to be reachable first."""
    async with httpx.AsyncClient() as http:
        api = DuetrackerAPI(api_url, api_key, http)
        bot = Bot(TelegramAPI(token, http), api, tz)
        while True:  # the API may still be starting up
            try:
                await bot.load_owner()
                break
            except Exception:
                await asyncio.sleep(2)
        try:
            me = await bot.tg.call("getMe")
            await bot.tg.set_commands(COMMANDS)
        except Exception as exc:
            log.error("Telegram bot not started: %s. Check TELEGRAM_BOT_TOKEN in config/.env.", exc)
            return
        log.info("Telegram bot @%s running (owner: %s). Send it /start.",
                 me.get("username"), bot.owner or "none yet")
        await asyncio.gather(bot.poll_loop(), bot.notify_loop(notify_interval_seconds))
