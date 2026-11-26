"""API tests. The feed is generated relative to *now*, so these never go stale."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from duetracker.api import create_app
from duetracker.config import Settings

KEY = "test-key"


def ics(events):
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    for uid, title, due in events:
        stamp = due.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body += ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{title} - Due",
                 "LOCATION:C999 Sample Module",
                 f"DESCRIPTION:Assignments:\\n{title} - https://lms.example.edu/d2l/lms/dropbox/"
                 f"user/folder_submit_files.d2l?ou=1&db={abs(hash(uid)) % 1000}",
                 f"DTSTART:{stamp}", f"DTEND:{stamp}", "END:VEVENT"]
    return "\r\n".join(body + ["END:VCALENDAR"]) + "\r\n"


@pytest.fixture
def env(tmp_path):
    now = datetime.now(timezone.utc)
    feed = tmp_path / "feed.ics"
    feed.write_text(ics([("u1", "Soon", now + timedelta(hours=20)),
                         ("u2", "Next week", now + timedelta(days=6)),
                         ("u3", "Far", now + timedelta(days=40))]), encoding="utf-8")
    settings = Settings(feed_source=str(feed), db_path=str(tmp_path / "t.db"), user_name="me",
                        timezone="Asia/Singapore", politemall_url="https://lms.example.edu",
                        session_dir=str(tmp_path / ".session"), api_key=KEY)
    client = TestClient(create_app(settings, run_scheduler=False))
    return client, feed, now


H = {"X-API-Key": KEY}


def test_health_is_public(env):
    client, *_ = env
    assert client.get("/health").json() == {"status": "ok"}


def test_requires_api_key(env):
    client, *_ = env
    assert client.get("/deliverables").status_code == 401
    assert client.get("/deliverables", headers={"X-API-Key": "wrong"}).status_code == 401


def test_sync_then_list(env):
    client, *_ = env
    r = client.post("/sync", headers=H).json()
    assert r["in_feed"] == 3 and r["new"] == 3
    week = client.get("/deliverables?days=7", headers=H).json()
    assert [d["title"] for d in week] == ["Soon", "Next week"]
    assert len(client.get("/deliverables?all=true", headers=H).json()) == 3


def test_first_sync_sends_reminders_not_new_alerts(env):
    client, *_ = env
    client.post("/sync", headers=H)
    notes = client.get("/notifications", headers=H).json()
    # "Soon" is due within a day, so exactly one reminder (which stage depends on the
    # time of day the test runs); no "new" alerts on the very first sync.
    assert [n["deliverable"]["title"] for n in notes] == ["Soon"]
    assert notes[0]["kind"].startswith("remind_")


def test_new_deliverable_alert_on_later_sync(env):
    client, feed, now = env
    client.post("/sync", headers=H)
    feed.write_text(ics([("u1", "Soon", now + timedelta(hours=20)),
                         ("u2", "Next week", now + timedelta(days=6)),
                         ("u3", "Far", now + timedelta(days=40)),
                         ("u4", "Surprise CA", now + timedelta(days=10))]), encoding="utf-8")
    assert client.post("/sync", headers=H).json()["new"] == 1
    kinds = {n["kind"]: n["deliverable"]["title"] for n in client.get("/notifications", headers=H).json()}
    assert kinds["new"] == "Surprise CA"


def test_ack_removes_notification(env):
    client, *_ = env
    client.post("/sync", headers=H)
    n = client.get("/notifications", headers=H).json()[0]
    assert client.post(f"/notifications/{n['id']}/ack", headers=H).status_code == 200
    assert client.get("/notifications", headers=H).json() == []
    assert client.post(f"/notifications/{n['id']}/ack", headers=H).status_code == 404


def test_done_undo_and_reminder_suppressed(env):
    client, *_ = env
    client.post("/sync", headers=H)
    soon = client.get("/deliverables", headers=H).json()[0]
    done = client.post(f"/deliverables/{soon['id']}/done", headers=H).json()
    assert done["done"] and done["done_source"] == "manual"
    assert client.get("/notifications", headers=H).json() == []  # no nagging once done
    assert [d["title"] for d in client.get("/deliverables", headers=H).json()] == ["Next week"]
    assert client.post(f"/deliverables/{soon['id']}/undo", headers=H).json()["done"] is False
    assert client.post("/deliverables/9999/done", headers=H).status_code == 404


def test_laptop_pushes_submissions(env):
    client, *_ = env
    client.post("/sync", headers=H)
    r = client.post("/submissions", headers=H, json={"uids": ["u2", "unknown"]}).json()
    assert r["marked_done"] == ["C999 Next week"]
    again = client.post("/submissions", headers=H, json={"uids": ["u2"]}).json()
    assert again["marked_done"] == []  # idempotent
    item = [d for d in client.get("/deliverables?all=true&include_done=true", headers=H).json()
            if d["uid"] == "u2"][0]
    assert item["done_source"] == "auto"


def test_refuses_to_start_without_api_key(tmp_path):
    s = Settings(feed_source="x", db_path=str(tmp_path / "t.db"), user_name="me", timezone="Asia/Singapore",
                 politemall_url="https://x", session_dir=".s", api_key="")
    with pytest.raises(SystemExit):
        create_app(s, run_scheduler=False)


def test_sync_reports_submission_check_when_not_logged_in(env):
    client, *_ = env
    r = client.post("/sync", headers=H).json()
    assert r["submission_check"] == "not_logged_in" and r["submitted"] == 0
    assert client.get("/status", headers=H).json()["submission_check"] == "not_logged_in"


def test_sync_ticks_off_submitted_work_and_alerts(env, monkeypatch):
    """Simulate POLITEMall saying 'Next week' (u2) was submitted."""
    import duetracker.api as api_mod
    from duetracker.service import AutoDoneResult

    def fake_auto_done(store, user_id, base_url, session_dir, now):
        row = store.get_by_uid(user_id, "u2")
        store.mark_done_by_uids(user_id, ["u2"])
        return AutoDoneResult(marked=[f"{row['module_code']} {row['title']}"], checked=2)

    monkeypatch.setattr(api_mod, "auto_done", fake_auto_done)
    client, *_ = env
    r = client.post("/sync", headers=H).json()
    assert (r["submitted"], r["submission_check"]) == (1, "ok")
    titles = [d["title"] for d in client.get("/deliverables?days=30", headers=H).json()]
    assert "Next week" not in titles
    kinds = {(n["kind"], n["deliverable"]["title"]) for n in client.get("/notifications", headers=H).json()}
    assert ("submitted", "Next week") in kinds
