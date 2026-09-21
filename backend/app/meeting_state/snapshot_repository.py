"""Persistence helpers for Meeting State checkpoints and health."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.meeting_state.schemas import MeetingStateContent
from app.meeting_state.snapshot_model import MeetingMemorySnapshot, MeetingStateHealth


def record_attempt(db: Session, meeting_id: str) -> MeetingStateHealth:
    row = db.get(MeetingStateHealth, meeting_id)
    if row is None:
        row = MeetingStateHealth(meeting_id=meeting_id)
        db.add(row)
    row.last_attempt_at = datetime.now(timezone.utc)
    db.commit()
    return row


def record_failure(db: Session, meeting_id: str, reason: str) -> MeetingStateHealth:
    row = db.get(MeetingStateHealth, meeting_id)
    if row is None:
        row = MeetingStateHealth(meeting_id=meeting_id)
        db.add(row)
    row.last_attempt_at = datetime.now(timezone.utc)
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_error = reason[:2000]
    return row


def record_success(db: Session, meeting_id: str) -> MeetingStateHealth:
    row = db.get(MeetingStateHealth, meeting_id)
    if row is None:
        row = MeetingStateHealth(meeting_id=meeting_id)
        db.add(row)
    now = datetime.now(timezone.utc)
    row.last_attempt_at = now
    row.last_success_at = now
    row.consecutive_failures = 0
    row.last_error = None
    return row


def add_snapshot(
    db: Session,
    meeting_id: str,
    *,
    from_segment_seq: int,
    to_segment_seq: int,
    window_start_ms: int | None,
    window_end_ms: int | None,
    content: MeetingStateContent,
    model_name: str | None,
) -> MeetingMemorySnapshot:
    row = MeetingMemorySnapshot(
        meeting_id=meeting_id,
        from_segment_seq=from_segment_seq,
        to_segment_seq=to_segment_seq,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        state_json=json.dumps(content.model_dump(), ensure_ascii=False),
        model_name=model_name,
    )
    db.add(row)
    return row


def list_for_meeting(db: Session, meeting_id: str) -> list[MeetingMemorySnapshot]:
    stmt = (
        select(MeetingMemorySnapshot)
        .where(MeetingMemorySnapshot.meeting_id == meeting_id)
        .order_by(MeetingMemorySnapshot.to_segment_seq.asc())
    )
    return list(db.scalars(stmt).all())


def prompt_material(
    db: Session, meeting_id: str, *, limit: int = 8
) -> list[dict]:
    """Return recent validated checkpoints without exposing ORM objects."""
    rows = list_for_meeting(db, meeting_id)
    selected = rows[-max(1, limit):]
    material: list[dict] = []
    for row in selected:
        try:
            state = json.loads(row.state_json)
        except json.JSONDecodeError:
            continue
        material.append(
            {
                "from_segment_seq": row.from_segment_seq,
                "to_segment_seq": row.to_segment_seq,
                "window_start_ms": row.window_start_ms,
                "window_end_ms": row.window_end_ms,
                "state": state,
            }
        )
    return material


__all__ = [
    "add_snapshot",
    "list_for_meeting",
    "prompt_material",
    "record_attempt",
    "record_failure",
    "record_success",
]
