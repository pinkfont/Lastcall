"""Turn a Brightspace (POLITEMall) calendar feed into a clean list of deliverables.

The raw feed mixes real deadlines with a lot of noise ("Available",
"Availability Ends" and other content-release events). This module keeps only
the events that are actual work to hand in, and normalises them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar

# Titles end with a status, separated by either a hyphen or an en dash,
# depending on how the lecturer created the item:
#   "Week 6 Deliverables - Due"   /   "W2 - Online Quiz – Due"
SUFFIX_RE = re.compile(
    r"\s+[-–]\s+(Due|Available|Availability Ends|Start Date|End Date)\s*$",
    re.IGNORECASE,
)

# A few task types put the status at the front instead:
#   "Hand in - Learning Memo 2"   /   "Give feedback as group - CA2 Peer evaluation"
PREFIX_RE = re.compile(
    r"^(Hand in|Give feedback as group|Give feedback)\s+[-–]\s+",
    re.IGNORECASE,
)

# Descriptions contain a header line naming the Brightspace tool the item lives in.
TOOL_RE = re.compile(r"^(Assignments|Quizzes|Materials|Modules|Discussions):", re.MULTILINE)
TOOL_TO_KIND = {
    "assignments": "assignment",
    "quizzes": "quiz",
    "materials": "elearning",
    "modules": "elearning",
    "discussions": "discussion",
}

# Module code at the start of LOCATION, e.g. "C207 Database Systems", "M0001F English ..."
MODULE_RE = re.compile(r"^([A-Z]{1,2}\d{3,4}[A-Z]?)\s+(.*)$")

URL_RE = re.compile(r"https?://\S+")
# Prefer links that go straight to the work, over the generic "view event" link.
PREFERRED_LINK_PARTS = ("/dropbox/", "/quizzing/", "/content/", "/discussions/")


@dataclass(frozen=True)
class Deliverable:
    uid: str
    module_code: str
    module_name: str
    title: str
    kind: str  # assignment | quiz | elearning | peer_evaluation | discussion | other
    due_at: datetime  # timezone-aware, in the local timezone
    link: str | None


def parse_feed(ics_text: str, tz: str = "Asia/Singapore") -> list[Deliverable]:
    """Parse the whole feed and return only real deliverables, sorted by due date."""
    local_tz = ZoneInfo(tz)
    cal = Calendar.from_ical(ics_text)
    items: list[Deliverable] = []
    for event in cal.walk("VEVENT"):
        item = _parse_event(event, local_tz)
        if item is not None:
            items.append(item)
    items.sort(key=lambda d: (d.due_at, d.module_code, d.title))
    return items


def _parse_event(event, local_tz: ZoneInfo) -> Deliverable | None:
    summary = _clean(str(event.get("SUMMARY", "")))
    description = str(event.get("DESCRIPTION", ""))
    location = _clean(str(event.get("LOCATION", "")))
    uid = str(event.get("UID", "")).strip()
    if not summary or not uid or event.get("DTSTART") is None:
        return None

    title, kind = _classify(summary, description)
    if title is None:
        return None  # noise: "Available", "Availability Ends", etc.

    module_code, module_name = _split_module(location)
    return Deliverable(
        uid=uid,
        module_code=module_code,
        module_name=module_name,
        title=title,
        kind=kind,
        due_at=_to_local(event.get("DTSTART").dt, local_tz),
        link=_pick_link(description),
    )


def _classify(summary: str, description: str) -> tuple[str | None, str]:
    """Return (clean title, kind) for a deliverable, or (None, "") for noise."""
    prefix = PREFIX_RE.match(summary)
    if prefix:
        title = summary[prefix.end():].strip()
        kind = "peer_evaluation" if prefix.group(1).lower().startswith("give feedback") else "assignment"
        return title, kind

    suffix = SUFFIX_RE.search(summary)
    if suffix and suffix.group(1).lower() == "due":
        title = summary[: suffix.start()].strip()
        return title, _kind_from_description(description, title)

    return None, ""


def _kind_from_description(description: str, title: str) -> str:
    tool = TOOL_RE.search(description)
    if tool:
        return TOOL_TO_KIND[tool.group(1).lower()]
    if "scorm" in title.lower() or "e-learning" in title.lower():
        return "elearning"
    if "quiz" in title.lower():
        return "quiz"
    return "other"


def _split_module(location: str) -> tuple[str, str]:
    match = MODULE_RE.match(location)
    if match:
        return match.group(1), match.group(2).strip()
    return "", location


def _pick_link(description: str) -> str | None:
    urls = [u.rstrip(").,") for u in URL_RE.findall(description)]
    for url in urls:
        if any(part in url for part in PREFERRED_LINK_PARTS):
            return url
    return urls[0] if urls else None


def _to_local(value: datetime | date, local_tz: ZoneInfo) -> datetime:
    """Feed times are UTC (e.g. 15:59:59Z = 23:59:59 in Singapore)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(local_tz)
    # All-day event: treat as end of that day locally.
    return datetime.combine(value, time(23, 59, 59), tzinfo=local_tz)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
