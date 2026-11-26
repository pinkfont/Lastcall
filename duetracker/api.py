"""REST API: the single backend that the Telegram bot and the web app both use.

Run locally:   python -m duetracker serve      (docs at http://127.0.0.1:8000/docs)

Every endpoint except /health needs the header  X-API-Key: <API_KEY from .env>
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from .config import Settings, load_settings
from .db import Store
from .fetcher import FeedError
from .service import run_sync_job

log = logging.getLogger("duetracker.api")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---- response / request models -------------------------------------------------

class DeliverableOut(BaseModel):
    id: int
    uid: str
    module_code: str
    module_name: str
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


class SubmissionsIn(BaseModel):
    uids: list[str]  # feed UIDs that POLITEMall shows as submitted


class SubmissionsOut(BaseModel):
    marked_done: list[str]


def _deliverable(row) -> DeliverableOut:
    return DeliverableOut(
        id=row["id"], uid=row["uid"], module_code=row["module_code"], module_name=row["module_name"],
        title=row["title"], kind=row["kind"], due_at=datetime.fromisoformat(row["due_at"]),
        link=row["link"], done=bool(row["done"]), done_source=row["done_source"],
    )


# ---- app factory -----------------------------------------------------------------

def create_app(settings: Settings | None = None, run_scheduler: bool = True) -> FastAPI:
    settings = settings or load_settings()
    if not settings.api_key:
        raise SystemExit("Set API_KEY in .env before starting the API (any long random string).")
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

    def sync_once() -> SyncOut:
        store = Store(settings.db_path)
        try:
            user_id = store.ensure_user(settings.user_name, settings.feed_source, settings.timezone)
            r = run_sync_job(store, user_id, settings.feed_source, settings.timezone)
            return SyncOut(in_feed=r.in_feed, new=r.new, rescheduled=r.rescheduled,
                           removed=r.removed, reminders=r.reminders)
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
        task = asyncio.create_task(scheduler()) if run_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="duetracker API", version="0.2.0", lifespan=lifespan,
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
        return [_deliverable(r) for r in store.upcoming(user_id, now, until, include_done)]

    @app.post("/deliverables/{item_id}/done", response_model=DeliverableOut, dependencies=protected)
    def mark_done(item_id: int, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        row = store.set_done(user_id, item_id, done=True, source="manual")
        if row is None:
            raise HTTPException(404, "No such deliverable")
        return _deliverable(row)

    @app.post("/deliverables/{item_id}/undo", response_model=DeliverableOut, dependencies=protected)
    def undo(item_id: int, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        row = store.set_done(user_id, item_id, done=False)
        if row is None:
            raise HTTPException(404, "No such deliverable")
        return _deliverable(row)

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
        for n in store.pending_notifications(user_id):
            item = store.get(user_id, n["deliverable_id"])
            if n["kind"].startswith("remind_") and item["done"]:
                store.ack_notification(user_id, n["id"])  # finished it already: don't nag
                continue
            out.append(NotificationOut(id=n["id"], kind=n["kind"], deliverable=_deliverable(item)))
        return out

    @app.post("/notifications/{notification_id}/ack", dependencies=protected)
    def ack(notification_id: int, store: Store = Depends(store_dep), user_id: int = Depends(user_dep)):
        if not store.ack_notification(user_id, notification_id):
            raise HTTPException(404, "No such pending notification")
        return {"acked": notification_id}

    return app
