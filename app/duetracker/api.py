"""REST API: the single backend that the Telegram bot and the web app both use.

Run locally:   python -m duetracker serve      (docs at http://127.0.0.1:8000/docs)

Every endpoint except /health needs the header  X-API-Key: <API_KEY from .env>
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .db import Store
from .fetcher import FeedError
from .service import auto_done, run_sync_job

log = logging.getLogger("duetracker.api")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---- response / request models -------------------------------------------------

class DeliverableOut(BaseModel):
    id: int
    uid: str
    module_code: str
    module_name: str
    module: str  # what to show: the user's name for the module, or its code
    title: str
    kind: str
    due_at: datetime
    link: str | None
    done: bool
    done_source: str | None


class NotificationOut(BaseModel):
    id: int
    kind: str
    deliverable: DeliverableOut


class SyncOut(BaseModel):
    in_feed: int
    new: int
    rescheduled: int
    removed: int
    reminders: int
    submitted: int = 0             # assignments newly ticked off from POLITEMall
    submission_check: str = "off"  # ok | not_logged_in | expired | error | off


class StatusOut(BaseModel):
    last_sync: datetime | None
    submission_check: str


class ModuleOut(BaseModel):
    code: str
    name: str
    alias: str | None


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=30)


class OwnerIn(BaseModel):
    chat_id: int


class SubmissionsIn(BaseModel):
    uids: list[str]  # feed UIDs that POLITEMall shows as submitted


class SubmissionsOut(BaseModel):
    marked_done: list[str]


def _deliverable(row, aliases: dict[str, str] | None = None) -> DeliverableOut:
    return DeliverableOut(
        id=row["id"], uid=row["uid"], module_code=row["module_code"], module_name=row["module_name"],
        module=(aliases or {}).get(row["module_code"]) or row["module_code"],
        title=row["title"], kind=row["kind"], due_at=datetime.fromisoformat(row["due_at"]),
        link=row["link"], done=bool(row["done"]), done_source=row["done_source"],
    )


# ---- app factory -----------------------------------------------------------------

def create_app(settings: Settings | None = None, run_scheduler: bool = True,
               check_submissions: bool = True) -> FastAPI:
    settings = settings or load_settings()
    if not settings.api_key:
        raise SystemExit("Set API_KEY in config/.env before starting the API (any long random string).")
    tz = ZoneInfo(settings.timezone)

    def store_dep():
        store = Store(settings.db_path)
        try:
            yield store
        finally:
            store.close()

    def user_dep(store: Store = Depends(store_dep)) -> int:
        return store.ensure_user(settings.user_name, settings.feed_source, settings.timezone)

    def auth(key: str | None = Security(api_key_header)) -> None:
        if not key or not secrets.compare_digest(key, settings.api_key):
            raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key")

    status = {"last_sync": None, "submission_check": "off"}
    sync_lock = threading.Lock()
    last_result: dict[str, SyncOut | None] = {"value": None}

    def sync_once() -> SyncOut:
        """Run one sync. If one is already running (e.g. the scheduler's), wait for it
        and return its result instead of checking POLITEMall twice at the same time."""
        if not sync_lock.acquire(blocking=False):
            with sync_lock:
                if last_result["value"] is not None:
                    return last_result["value"]
            sync_lock.acquire()
        try:
            last_result["value"] = _sync_now()
            return last_result["value"]
        finally:
            sync_lock.release()

    def _sync_now() -> SyncOut:
        """Feed sync, then (if logged in on this machine) the POLITEMall submission check."""
        store = Store(settings.db_path)
        try:
            user_id = store.ensure_user(settings.user_name, settings.feed_source, settings.timezone)
            now = datetime.now(tz)
            r = run_sync_job(store, user_id, settings.feed_source, settings.timezone, now)
            out = SyncOut(in_feed=r.in_feed, new=r.new, rescheduled=r.rescheduled,
                          removed=r.removed, reminders=r.reminders)
            if check_submissions:
                a = auto_done(store, user_id, settings.politemall_url, settings.session_dir, now)
                out.submitted, out.submission_check = len(a.marked), a.status
                if a.status == "error":
                    log.warning("submission check failed: %s", a.error)
            status["last_sync"], status["submission_check"] = now, out.submission_check
            return out
        finally:
            store.close()

    async def scheduler() -> None:
        """Sync on start, then every SYNC_INTERVAL_MINUTES. Errors are logged, never fatal."""
        while True:
            try:
                result = await asyncio.to_thread(sync_once)
                log.info("scheduled sync: %s", result.model_dump())
            except Exception as exc:
                log.warning("scheduled sync failed: %s", exc)
            await asyncio.sleep(settings.sync_interval_minutes * 60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if run_scheduler:
            tasks.append(asyncio.create_task(scheduler()))
            if settings.telegram_bot_token:
                from .telegram_bot import run_bot
                tasks.append(asyncio.create_task(run_bot(
                    settings.telegram_bot_token, f"http://127.0.0.1:{settings.port}",
                    settings.api_key, settings.timezone, settings.notify_interval_seconds)))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="Lastcall API", version="0.2.0", lifespan=lifespan,
                  description="Deadlines from POLITEMall, for the Telegram bot and web app.")
    protected = [Depends(auth)]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/deliverables", response_model=list[DeliverableOut], dependencies=protected)
    def list_deliverables(days: int | None = Query(7, ge=1, le=365, description="Omit with all=true"),
                          all: bool = False, include_done: bool = False,
                          store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        now = datetime.now(tz)
        until = None if all else now + timedelta(days=days or 7)
        aliases = store.aliases(user_id)
        return [_deliverable(r, aliases) for r in store.upcoming(user_id, now, until, include_done)]

    @app.post("/deliverables/{item_id}/done", response_model=DeliverableOut, dependencies=protected)
    def mark_done(item_id: int, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        row = store.set_done(user_id, item_id, done=True, source="manual")
        if row is None:
            raise HTTPException(404, "No such deliverable")
        return _deliverable(row, store.aliases(user_id))

    @app.post("/deliverables/{item_id}/undo", response_model=DeliverableOut, dependencies=protected)
    def undo(item_id: int, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        row = store.set_done(user_id, item_id, done=False)
        if row is None:
            raise HTTPException(404, "No such deliverable")
        return _deliverable(row, store.aliases(user_id))

    @app.post("/sync", response_model=SyncOut, dependencies=protected)
    async def sync_now():
        try:
            return await asyncio.to_thread(sync_once)
        except FeedError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/submissions", response_model=SubmissionsOut, dependencies=protected)
    def submissions(body: SubmissionsIn, store: Store = Depends(store_dep),
                    user_id: int = Depends(user_dep)):
        """The laptop reports which assignments POLITEMall shows as submitted."""
        rows = store.mark_done_by_uids(user_id, body.uids, source="auto")
        return SubmissionsOut(marked_done=[f"{r['module_code']} {r['title']}" for r in rows])

    @app.get("/notifications", response_model=list[NotificationOut], dependencies=protected)
    def notifications(store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        """Pending messages for the bot to send. Acknowledge each one after sending."""
        out = []
        aliases = store.aliases(user_id)
        for n in store.pending_notifications(user_id):
            item = store.get(user_id, n["deliverable_id"])
            if n["kind"].startswith("remind_") and item["done"]:
                store.ack_notification(user_id, n["id"])  # finished it already: don't nag
                continue
            out.append(NotificationOut(id=n["id"], kind=n["kind"], deliverable=_deliverable(item, aliases)))
        return out

    @app.post("/notifications/{notification_id}/ack", dependencies=protected)
    def ack(notification_id: int, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        if not store.ack_notification(user_id, notification_id):
            raise HTTPException(404, "No such pending notification")
        return {"acked": notification_id}

    @app.get("/history", response_model=list[DeliverableOut], dependencies=protected)
    def history(days: int = Query(30, ge=1, le=365), store: Store = Depends(store_dep),
                user_id: int = Depends(user_dep)):
        """Completed in the last `days` days, plus overdue items that were never done."""
        now = datetime.now(tz)
        aliases = store.aliases(user_id)
        return [_deliverable(r, aliases) for r in store.history(user_id, now - timedelta(days=days), now)]

    @app.get("/pickers/complete", response_model=list[DeliverableOut], dependencies=protected)
    def complete_choices(store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        """What can be marked complete: open items from the last 7 days to 30 days ahead."""
        now = datetime.now(tz)
        aliases = store.aliases(user_id)
        rows = store.open_items(user_id, now - timedelta(days=7), now + timedelta(days=30))
        return [_deliverable(r, aliases) for r in rows]

    @app.get("/pickers/undo", response_model=list[DeliverableOut], dependencies=protected)
    def undo_choices(store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        """Recently completed items (last 30 days), newest first."""
        aliases = store.aliases(user_id)
        rows = store.recently_done(user_id, datetime.now(tz) - timedelta(days=30))
        return [_deliverable(r, aliases) for r in rows]

    @app.get("/modules", response_model=list[ModuleOut], dependencies=protected)
    def modules(store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        """This semester's modules (anything due in the last 60 days or later)."""
        aliases = store.aliases(user_id)
        rows = store.modules(user_id, datetime.now(tz) - timedelta(days=60))
        return [ModuleOut(code=r["module_code"], name=r["module_name"], alias=aliases.get(r["module_code"]))
                for r in rows]

    @app.put("/modules/{code}/alias", response_model=ModuleOut, dependencies=protected)
    def rename_module(code: str, body: AliasIn, store: Store = Depends(store_dep),
                      user_id: int = Depends(user_dep)):
        alias = " ".join(body.alias.split())
        if not alias:
            raise HTTPException(422, "Name can't be empty")
        store.set_alias(user_id, code, alias)
        return ModuleOut(code=code, name="", alias=alias)

    @app.delete("/modules/{code}/alias", dependencies=protected)
    def reset_module(code: str, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        store.set_alias(user_id, code, None)
        return {"code": code, "alias": None}

    @app.get("/status", response_model=StatusOut, dependencies=protected)
    def get_status():
        """Last sync time and whether the POLITEMall submission check is working."""
        return StatusOut(**status)

    @app.get("/telegram/owner", dependencies=protected)
    def telegram_owner(store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        return {"chat_id": store.telegram_owner(user_id)}

    @app.post("/telegram/owner", dependencies=protected)
    def claim_owner(body: OwnerIn, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        """Lock the bot to the first chat that sends /start."""
        if store.claim_telegram_owner(user_id, body.chat_id):
            return {"chat_id": body.chat_id}
        if store.telegram_owner(user_id) == body.chat_id:
            return {"chat_id": body.chat_id}
        raise HTTPException(409, "This bot already belongs to someone else")

    return app
