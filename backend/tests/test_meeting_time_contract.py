from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.meeting.schemas import MeetingOut
from app.realtime.sse_service import build_meeting_status


def _meeting(**overrides):
    values = {
        "id": "meeting_time",
        "meeting_code": "123456789",
        "title": "time contract",
        "description": None,
        "status": "IN_PROGRESS",
        "started_at": datetime(2026, 9, 21, 7, 46, 39),
        "ended_at": None,
        "completed_at": None,
        "created_at": datetime(2026, 9, 21, 7, 45, 0),
        "updated_at": datetime(2026, 9, 21, 7, 46, 39),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_meeting_response_marks_sqlite_naive_datetimes_as_utc():
    payload = MeetingOut.model_validate(_meeting()).model_dump(mode="json")

    assert payload["started_at"] == "2026-09-21T07:46:39Z"
    assert payload["created_at"] == "2026-09-21T07:45:00Z"


def test_meeting_response_converts_explicit_offsets_to_utc():
    local = datetime(2026, 9, 21, 15, 46, 39, tzinfo=timezone(timedelta(hours=8)))
    payload = MeetingOut.model_validate(_meeting(started_at=local)).model_dump(mode="json")

    assert payload["started_at"] == "2026-09-21T07:46:39Z"


def test_meeting_status_event_uses_the_same_utc_contract():
    event = build_meeting_status(_meeting())

    assert event.payload["started_at"] == "2026-09-21T07:46:39Z"
