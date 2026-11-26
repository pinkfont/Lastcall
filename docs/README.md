# duetracker

A deadline tracker for Singapore polytechnic students. It pulls your deliverables
straight from **POLITEMall** (Brightspace) so you never have to check each module
page for what's due.

```
$ python -m duetracker due
3 due in the next 7 days:

[ ] #114  C207    Week 6 - Deliverables Submissions
           Assignment · Sun 29 Nov, 11:59 PM (in 3d 12h)
[ ] #115  C207    Week 6 Deliverables
           Assignment · Sun 29 Nov, 11:59 PM (in 3d 12h)
[ ] #116  C210    Week 06 Deliverables
           Assignment · Sun 29 Nov, 11:59 PM (in 3d 12h)
```

## Why

POLITEMall's homepage calendar doesn't show deliverables, its notifications only
carry admin news, and there's no email when work is assigned. Deadlines are spread
across every module's own calendar. This tool collects them in one place.

## How it works

```
POLITEMall calendar feed (.ics)
        │  fetcher.py   download (token never logged)
        ▼
   parser.py           filter noise, normalise, convert UTC → SGT
        ▼
     db.py             SQLite upsert, detect new / rescheduled / removed
        ▼
   reminders.py        3 days before · day before · morning of (08:00)
        ▼
  api.py (FastAPI)     REST API + background scheduler (sync every 2h)
        ▼
  telegram_bot.py      /due · /done · ✅ buttons · reminders     (web app: planned)
  cli.py               local use

 politemall_api.py     your own login session → "has this been submitted?"
```

### Automatic "done" detection

The calendar feed says what's due but not whether you've handed it in. Students
can't get official Brightspace API keys, but the POLITEMall site calls the same
REST API (`/d2l/api/...`) with your logged-in session, so duetracker does too:

- `login` opens a real browser; you sign in yourself (2FA included). Only the
  session cookies are saved locally (`.session/`, git-ignored). No password is stored.
- On each sync (the scheduler's, `/sync` in Telegram, or `due`), a headless browser asks
  `/dropbox/folders/{folderId}/submissions/mysubmissions/` for each upcoming
  assignment and ticks it off if a submission exists (`done_source = 'auto'`).
- Folder IDs come from the feed's submission links; for links without one, the
  assignment is matched by name within its course.
- If the session expires, deadlines keep syncing from the feed and you're told to
  log in again. Requests are GET-only, few per sync, and paced.

### Cleaning the feed

A real feed for one student had **549 events across 22 modules**, but only **148
were actual deliverables**. The parser deals with:

| Problem in the raw feed | Handling |
|---|---|
| Content-release noise ("– Available", "– Availability Ends") | Keep only "Due" items |
| Titles use a hyphen *or* an en dash before the status | Regex accepts both |
| Some tasks put the status first ("Hand in - …", "Give feedback as group - …") | Prefix rule |
| Times are UTC (`15:59:59Z`) | Converted to Asia/Singapore (`11:59 PM`) |
| Type is buried in the description | Read the `Assignments:` / `Quizzes:` / `Materials:` header |
| Old semesters stay in the feed | Only upcoming items are reported |
| Feed can't tell whether you've submitted | Checked via your own POLITEMall session (see below), plus manual `done` / `undo` |
| Lecturers change or delete deadlines | Detected by event UID and reported on sync |

## REST API

One backend for every client (Telegram bot, web app, CLI). Run it with
`python -m duetracker serve` (or `serve.bat`); interactive docs are at `/docs`.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness check (public) |
| GET | `/deliverables?days=7` | Upcoming deliverables (`all=true`, `include_done=true`) |
| POST | `/deliverables/{id}/done` · `/undo` | Mark manually |
| POST | `/sync` | Sync now (the scheduler also runs every `SYNC_INTERVAL_MINUTES`) |
| POST | `/submissions` | Laptop reports submitted assignments by feed UID |
| GET | `/notifications` | Pending alerts: new deadline, deadline moved, reminders |
| POST | `/notifications/{id}/ack` | Mark an alert as delivered |

All endpoints except `/health` require an `X-API-Key` header.

**Reminders** fire at 08:00 three days before, the day before, and on the day. Only
the latest applicable stage is sent (a deadline first seen on its due date gives one
message, not three), reminders stop once an item is done, and they're re-planned if a
lecturer moves the deadline. The first sync doesn't send "new deadline" alerts for
everything already in the feed.

When the API runs on the machine where you logged in, the scheduled sync checks
submissions too, and the bot warns you once if the POLITEMall login has expired
(`GET /status`).

**Hybrid deployment.** The API can run on a server 24/7, but POLITEMall's sign-in
(with 2FA) needs a real screen. So submission checks run on your laptop during `sync`
and are pushed to the server's `/submissions` endpoint when `API_URL` is set.

## Telegram bot

A client of the REST API, using Telegram's Bot API directly (long polling, no extra
framework). It starts automatically with `serve` when `TELEGRAM_BOT_TOKEN` is set.

- `/due`, `/due 30`: list with 🔗 open-submission and ✅ done buttons
- `/done 115`, `/undo 115`, `/sync`, `/help`
- Delivers reminders, "new / moved deadline" and "✅ submitted on POLITEMall" alerts from `/notifications`,
  acknowledging each one so nothing is sent twice
- **Private:** the first chat to send `/start` becomes the owner (stored in the DB);
  everyone else is refused

Create a bot with [@BotFather](https://t.me/BotFather) and put its token in `config/.env`.

## Project layout

```
setup.bat  login.bat  due.bat  serve.bat   double-click commands (Windows)
app/       duetracker/ (code) · tests/ · requirements.txt · .venv/
config/    .env (your settings, git-ignored) · .env.example
data/      database, saved POLITEMall login, debug logs (git-ignored)
docs/      this README
```

## Setup

```bash
pip install -r app/requirements.txt
cp config/.env.example config/.env
```

Get your feed link: **POLITEMall → Calendar → Subscribe → "All Calendars and Tasks"**,
then paste it into `config/.env` as `FEED_URL`.

> The link contains a private token. Anyone with it can read your calendar.
> Keep it in `config/.env` (git-ignored), and press **Reset** in POLITEMall if it ever leaks.

## Usage

On Windows you can just double-click `setup.bat` once, `login.bat` once, then `due.bat`.
`serve.bat` runs the API locally.

Run the Python commands from inside `app/`:

```bash
cd app
python -m duetracker login             # one-time sign-in for automatic "done" detection
python -m duetracker sync              # fetch the feed, show what's new or changed
python -m duetracker due               # next 7 days
python -m duetracker due --days 30
python -m duetracker due --all
python -m duetracker done 114          # mark as submitted
python -m duetracker undo 114
python -m duetracker serve             # REST API + scheduler
```

## Tests

```bash
cd app
python -m pytest
```

Tests use `tests/sample_feed.ics`, a made-up feed that reproduces each messy case
above. No real student data is committed.

## Roadmap

- [x] Feed parsing and cleaning
- [x] SQLite storage with change detection
- [x] CLI
- [x] Automatic "done" detection for assignments
- [ ] Same for quizzes and e-learning
- [x] REST API (FastAPI) with API-key auth
- [x] Scheduled sync + reminder planning (3 days, 1 day, morning of)
- [x] Telegram bot (`/due`, `/done`, buttons, reminders, new/moved-deadline alerts)
- [ ] Web app dashboard
- [ ] Deployment (API 24/7, laptop pushes submission checks)
- [ ] Multi-user onboarding (paste your own feed link)
- [ ] POLITEMall news add-on
