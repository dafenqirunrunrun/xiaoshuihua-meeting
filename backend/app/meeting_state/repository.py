"""Meeting State repository: SQLAlchemy queries, no business rules.

Insert / read / update only. Version allocation, idempotency and failure
decisions live in `meeting_state.service`.
"""

from __future__ import annotations

import json

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.meeting_state.model import MeetingState
from app.meeting_state.schemas import MeetingStateContent


class MeetingStateCorruptError(Exception):
    """A persisted state_json cannot be read back into MeetingStateContent.

    Deliberately loud and separate from a provider/parse failure: the stored
    state is OUR assertion, so not being able to decode it is a data-integrity
    problem, not a model problem. The updater reports it and refuses to
    overwrite the row with a fresh state — losing a state we cannot even read
    is unacceptable, and a human should look at the row.
    """

    def __init__(self, meeting_id: str, reason: str) -> None:
        self.meeting_id = meeting_id
        super().__init__(f"meeting state for {meeting_id} is unreadable: {reason}")


def get(db: Session, meeting_id: str) -> MeetingState | None:
    return db.get(MeetingState, meeting_id)


def get_content(db: Session, meeting_id: str) -> MeetingStateContent:
    """Decode the persisted state payload (empty content when nothing stored).

    Raises MeetingStateCorruptError rather than silently returning an empty
    state — an unreadable row must never be laundered into "we know nothing".
    """
    row = get(db, meeting_id)
    if row is None or not row.state_json:
        return MeetingStateContent()
    try:
        data = json.loads(row.state_json)
    except json.JSONDecodeError as exc:
        raise MeetingStateCorruptError(meeting_id, f"invalid JSON ({exc.msg})") from exc
    try:
        return MeetingStateContent.model_validate(data)
    except ValidationError as exc:
        raise MeetingStateCorruptError(
            meeting_id, f"schema mismatch ({exc.errors()[:2]})"
        ) from exc


def upsert(
    db: Session,
    meeting_id: str,
    *,
    content: MeetingStateContent,
    version: int,
    last_processed_segment_seq: int,
    status: str,
    model_name: str | None = None,
    last_error: str | None = None,
) -> MeetingState:
    """Replace the meeting's current state with `content` at `version`.

    A whole-row replace, not a merge: the pipeline passes the state it intends
    to persist, and the cursor is written in the SAME commit as the payload so
    the two can never disagree. Callers hold the meeting lock.
    """
    row = get(db, meeting_id)
    if row is None:
        row = MeetingState(meeting_id=meeting_id)
        db.add(row)

    row.version = version
    row.last_processed_segment_seq = last_processed_segment_seq
    row.state_json = json.dumps(content.model_dump(), ensure_ascii=False)
    row.status = status
    row.model_name = model_name
    row.last_error = last_error
    db.commit()
    db.refresh(row)
    return row


def mark_failed(
    db: Session,
    meeting_id: str,
    *,
    reason: str,
    model_name: str | None = None,
) -> MeetingState:
    """Record a failed update WITHOUT touching the state payload or the cursor.

    This is the "do not pollute the existing state" rule made structural: only
    `status` and `last_error` move, so a failed LLM call can never damage what
    we already know. `version` is intentionally NOT incremented — it counts
    successful updates.
    """
    row = get(db, meeting_id)
    if row is None:
        row = MeetingState(meeting_id=meeting_id)
        db.add(row)
    row.status = "FAILED"
    row.last_error = reason[:1000]
    if model_name is not None:
        row.model_name = model_name
    db.commit()
    db.refresh(row)
    return row


__all__ = ["MeetingStateCorruptError", "get", "get_content", "upsert", "mark_failed"]
