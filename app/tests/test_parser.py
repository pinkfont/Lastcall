from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from duetracker.parser import parse_feed

SGT = ZoneInfo("Asia/Singapore")
SAMPLE = (Path(__file__).parent / "sample_feed.ics").read_text(encoding="utf-8")


def by_uid():
    return {d.uid: d for d in parse_feed(SAMPLE)}


def test_noise_is_filtered_out():
    uids = set(by_uid())
    assert "6666-2@lms.example.edu" not in uids  # Availability Ends
    assert "6666-3@lms.example.edu" not in uids  # Available (hyphen)
    assert "6666-8@lms.example.edu" not in uids  # Available (en dash)


def test_keeps_due_with_hyphen_and_en_dash():
    items = by_uid()
    assert items["6666-1@lms.example.edu"].title == "Week 6 Deliverables"
    assert items["6666-4@lms.example.edu"].title == "Lesson 25 Async eLearning Quiz"


def test_prefix_style_tasks_are_kept():
    items = by_uid()
    assert items["6666-5@lms.example.edu"].title == "CA2 Peer evaluation"
    assert items["6666-5@lms.example.edu"].kind == "peer_evaluation"
    assert items["6666-6@lms.example.edu"].title == "Learning Memo 2"
    assert items["6666-6@lms.example.edu"].kind == "assignment"


def test_utc_converted_to_singapore_time():
    due = by_uid()["6666-1@lms.example.edu"].due_at
    assert due == datetime(2026, 11, 29, 23, 59, 59, tzinfo=SGT)


def test_kind_detected_from_description():
    items = by_uid()
    assert items["6666-1@lms.example.edu"].kind == "assignment"
    assert items["6666-4@lms.example.edu"].kind == "quiz"  # tool header is not on the first line
    assert items["6666-7@lms.example.edu"].kind == "elearning"


def test_module_split_and_link_preference():
    item = by_uid()["6666-1@lms.example.edu"]
    assert (item.module_code, item.module_name) == ("C999", "Sample Database Module")
    assert "/dropbox/" in item.link  # submission link wins over "view event"


def test_sorted_by_due_date():
    dues = [d.due_at for d in parse_feed(SAMPLE)]
    assert dues == sorted(dues)
