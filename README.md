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
     cli.py            sync · due · done · undo      (Telegram bot: planned)
        ▲
 politemall_api.py     your own login session → "has this been submitted?"
```

### Automatic "done" detection

The calendar feed says what's due but not whether you've handed it in. Students
can't get official Brightspace API keys, but the POLITEMall site calls the same
REST API (`/d2l/api/...`) with your logged-in session, so duetracker does too:

- `login` opens a real browser; you sign in yourself (2FA included). Only the
  session cookies are saved locally (`.session/`, git-ignored). No password is stored.
- On each `sync`, a headless browser asks
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

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Get your feed link: **POLITEMall → Calendar → Subscribe → "All Calendars and Tasks"**,
then paste it into `.env` as `FEED_URL`.

> The link contains a private token. Anyone with it can read your calendar.
> Keep it in `.env` (git-ignored), and press **Reset** in POLITEMall if it ever leaks.

## Usage

On Windows you can just double-click `setup.bat` once, `login.bat` once, then `due.bat`.

```bash
python -m duetracker login             # one-time sign-in for automatic "done" detection
python -m duetracker sync              # fetch the feed, show what's new or changed
python -m duetracker due               # next 7 days
python -m duetracker due --days 30
python -m duetracker due --all
python -m duetracker done 114          # mark as submitted
python -m duetracker undo 114
```

## Tests

```bash
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
- [ ] Scheduled sync + reminders (3 days, 1 day, morning of)
- [ ] Telegram bot (`/due`, `/done`, new-deliverable alerts)
- [ ] Multi-user onboarding (paste your own feed link)
- [ ] POLITEMall news add-on
