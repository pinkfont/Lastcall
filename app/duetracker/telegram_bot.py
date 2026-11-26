"""Telegram bot: a client of the duetracker REST API.

It talks to Telegram's Bot API directly (long polling) and to the duetracker API
over HTTP, exactly like any other client would, so it can later run on its own.

Commands:  /start  /due [days]  /done <id>  /undo <id>  /sync  /help
Buttons:   🔗 open the submission page · ✅ mark done
Alerts:    reminders, new deadlines and moved deadlines from /notifications

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

LIST_LIMIT = 25
HEADINGS = {
    "remind_3d": "⏰ Due in 3 days",
    "remind_1d": "⏰ Due tomorrow",
    "remind_morning": "🔴 Due today",
    "new": "🆕 New deliverable",
    "submitted": "✅ Submitted on POLITEMall",
}
EXPIRED_WARNING = ("⚠️ Your POLITEMall login has expired, so I can't see your submissions. "
                   "On your laptop, double-click <b>login</b> and sign in again.")
HELP = (
    "<b>duetracker</b>: your POLITEMall deadlines\n\n"
    "/due: what's due in the next 7 days\n"
    "/due 30: the next 30 days\n"
    "/done 115: mark #115 as submitted (or tap ✅)\n"
    "/undo 115: unmark it\n"
    "/sync: fetch the latest from POLITEMall now\n\n"
    "I'll also message you 3 days before, the day before, and on the morning "
    "something is due, and whenever a new deadline appears or one is moved."
)


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

    async def send(self, chat_id: int, text: str, buttons=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **params)

    async def edit(self, chat_id: int, message_id: int, text: str, buttons=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True,
                  "reply_markup": {"inline_keyboard": buttons or []}}
        return await self.call("editMessageText", **params)

    async def answer(self, callback_id: str, text: str = ""):
        return await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)


class DuetrackerAPI:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient):
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.http = http

    async def _req(self, method: str, path: str, **kw):
        resp = await self.http.request(method, f"{self.base}{path}", headers=self.headers,
                                       timeout=120, **kw)
        resp.raise_for_status()
        return resp.json()

    async def deliverables(self, days: int = 7):
        return await self._req("GET", "/deliverables", params={"days": days})

    async def done(self, item_id: int):
        return await self._req("POST", f"/deliverables/{item_id}/done")

    async def undo(self, item_id: int):
        return await self._req("POST", f"/deliverables/{item_id}/undo")

    async def sync(self):
        return await self._req("POST", "/sync")

    async def status(self):
        return await self._req("GET", "/status")

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


def fmt_due(item: dict, tz: ZoneInfo, now: datetime) -> str:
    due = datetime.fromisoformat(item["due_at"]).astimezone(tz)
    return f"{due:%a %d %b, %I:%M %p} ({time_left(due, now)})"


def item_buttons(item: dict) -> list[list[dict]]:
    row = []
    if item.get("link"):
        row.append({"text": "🔗 Open", "url": item["link"]})
    if item.get("done"):
        row.append({"text": "↩️ Undo", "callback_data": f"undo:{item['id']}"})
    else:
        row.append({"text": "✅ Done", "callback_data": f"done:{item['id']}"})
    return [row]


def item_text(item: dict, tz: ZoneInfo, now: datetime, heading: str | None = None) -> str:
    lines = [f"<b>{html.escape(heading)}</b>"] if heading else []
    tick = "✅ " if item.get("done") else ""
    lines.append(f"{tick}<b>{html.escape(item['module_code'])}</b> {html.escape(item['title'])}")
    lines.append(f"{fmt_due(item, tz, now)} · #{item['id']}")
    return "\n".join(lines)


def list_message(items: list[dict], days: int, tz: ZoneInfo, now: datetime):
    if not items:
        return f"Nothing due in the next {days} days 🎉", None
    shown = items[:LIST_LIMIT]
    lines = [f"<b>{len(items)} due in the next {days} days</b>\n"]
    buttons = []
    for n, item in enumerate(shown, 1):
        lines.append(f"{n}. <b>{html.escape(item['module_code'])}</b> {html.escape(item['title'])}\n"
                     f"    {fmt_due(item, tz, now)}")
        row = [{"text": f"✅ {n}", "callback_data": f"done:{item['id']}"}]
        if item.get("link"):
            row.insert(0, {"text": f"🔗 {n}", "url": item["link"]})
        buttons.append(row)
    if len(items) > LIST_LIMIT:
        lines.append(f"\n…and {len(items) - LIST_LIMIT} more.")
    return "\n".join(lines), buttons


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
        command, *args = text.split()
        command = command.split("@")[0].lower()  # "/due@my_bot" in groups

        if command == "/start":
            if self.owner is None and await self.api.claim(chat_id):
                self.owner = chat_id
                await self.tg.send(chat_id, "👋 You're now the owner of this bot.\n\n" + HELP)
            elif chat_id == self.owner:
                await self.tg.send(chat_id, HELP)
            else:
                await self.tg.send(chat_id, "Sorry, this is a private bot.")
            return

        if chat_id != self.owner:
            return  # ignore strangers silently

        if command == "/help":
            await self.tg.send(chat_id, HELP)
        elif command == "/due":
            days = int(args[0]) if args and args[0].isdigit() else 7
            days = max(1, min(days, 365))
            text, buttons = list_message(await self.api.deliverables(days), days, self.tz, self.now())
            await self.tg.send(chat_id, text, buttons)
        elif command in ("/done", "/undo"):
            if not args or not args[0].lstrip("#").isdigit():
                await self.tg.send(chat_id, f"Usage: {command} 115 (the #number from /due)")
                return
            await self._set_done(chat_id, int(args[0].lstrip("#")), command == "/done")
        elif command == "/sync":
            await self.tg.send(chat_id, "🔄 Syncing with POLITEMall…")
            try:
                r = await self.api.sync()
                text = (f"Done: {r['in_feed']} deliverables in feed, {r['new']} new, "
                        f"{r['rescheduled']} moved, {r['removed']} removed.")
                check = r.get("submission_check", "off")
                if check == "ok":
                    text += f"\nPOLITEMall submissions checked: {r.get('submitted', 0)} newly ticked off."
                elif check == "expired":
                    text += "\n" + EXPIRED_WARNING
                elif check == "not_logged_in":
                    text += "\nSubmissions aren't checked yet: double-click <b>login</b> on your laptop."
                await self.tg.send(chat_id, text)
            except httpx.HTTPStatusError as exc:
                await self.tg.send(chat_id, f"Sync failed: {html.escape(exc.response.text[:300])}")
        else:
            await self.tg.send(chat_id, "I didn't get that. Try /help")

    async def _set_done(self, chat_id: int, item_id: int, done: bool) -> None:
        try:
            item = await (self.api.done(item_id) if done else self.api.undo(item_id))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                await self.tg.send(chat_id, f"There's no deliverable #{item_id}.")
                return
            raise
        verb = "✅ Marked done" if done else "↩️ Unmarked"
        await self.tg.send(chat_id, f"{verb}: <b>{html.escape(item['module_code'])}</b> "
                                    f"{html.escape(item['title'])}")

    async def _on_button(self, query: dict) -> None:
        chat_id = query["message"]["chat"]["id"]
        if chat_id != self.owner:
            await self.tg.answer(query["id"])
            return
        action, _, raw_id = query.get("data", "").partition(":")
        if action not in ("done", "undo") or not raw_id.isdigit():
            await self.tg.answer(query["id"])
            return
        try:
            item = await (self.api.done(int(raw_id)) if action == "done" else self.api.undo(int(raw_id)))
        except httpx.HTTPStatusError:
            await self.tg.answer(query["id"], "That deliverable no longer exists.")
            return
        await self.tg.answer(query["id"], "Marked done ✅" if action == "done" else "Unmarked")

        # Refresh the message the button was on.
        message_id = query["message"]["message_id"]
        original = query["message"].get("text", "")
        if original and original[0].isdigit() and " due in the next " in original.split("\n")[0]:
            days = int(original.split("next ")[1].split(" ")[0])
            text, buttons = list_message(await self.api.deliverables(days), days, self.tz, self.now())
        else:
            heading = original.split("\n")[0] if original.startswith(("⏰", "🔴", "🆕", "📅", "📌")) else None
            text, buttons = item_text(item, self.tz, self.now(), heading), item_buttons(item)
        await self.tg.edit(chat_id, message_id, text, buttons)

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
            await self.tg.send(self.owner, item_text(item, self.tz, self.now(),
                                                     notification_heading(note["kind"])),
                               item_buttons(item))
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
        except Exception as exc:
            log.error("Telegram bot not started: %s. Check TELEGRAM_BOT_TOKEN in config/.env.", exc)
            return
        log.info("Telegram bot @%s running (owner: %s). Send it /start.",
                 me.get("username"), bot.owner or "none yet")
        await asyncio.gather(bot.poll_loop(), bot.notify_loop(notify_interval_seconds))
