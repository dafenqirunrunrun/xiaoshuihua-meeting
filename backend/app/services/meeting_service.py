"""Meeting state machine and lifecycle.

States (PRD §11): CREATED → IN_PROGRESS → FINALIZING → COMPLETED.
This step implements the first two business transitions:
  CREATED --start--> IN_PROGRESS
  IN_PROGRESS --finish--> FINALIZING
FINALIZING → COMPLETED is done by the Finalizer / Summary pipeline (later step).

Controller contract (from Step 2 spec):
  start:
    CREATED → IN_PROGRESS (record started_at, idempotent)
    already IN_PROGRESS → return current meeting, do NOT touch started_at
    FINALIZING / COMPLETED → 409
    missing meeting → 404
  finish:
    IN_PROGRESS → FINALIZING (record ended_at, idempotent)
    already FINALIZING → return current meeting, do NOT touch ended_at
    already COMPLETED → do NOT re-enter FINALIZING (idempotent: return current)
    CREATED → 409
    missing meeting → 404
"""

import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.exceptions import InvalidMeetingStateTransition, MeetingNotFoundError
from app.schemas import meeting as meeting_schemas
from app.services import sse_service  # publish AFTER db.commit() only


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Concurrency model ---------------------------------------------------
# MVP: single process, single uvicorn worker, SQLite (which serializes writes
# application-wide and has no real row locks). An in-process RLock makes
# concurrent start/finish on the same meeting deterministic and free of
# lost-update / illegal-transition races within this process.
#
# PostgreSQL / company data platform hardening (not done now, noted for later):
#   - Replace this RLock with row-level locking: `SELECT ... FOR UPDATE` (or
#     FOR NO KEY UPDATE) on the meeting row inside the same transaction that
#     performs the guarded state check + UPDATE. That removes the need for the
#     app-level lock and works across multiple processes/instances.
#   - Do NOT rely on last-write-wins `meeting.status = x`; always re-read under
#     the row lock and re-validate the current state before mutating.
#   - Keep the unique constraints (event_id, meeting+seq) for idempotency.
_MEETING_LOCK = threading.RLock()


def create_meeting(db: Session, payload: meeting_schemas.MeetingCreate) -> models.Meeting:
    meeting = models.Meeting(
        id=f"meeting_{uuid.uuid4().hex[:8]}",
        title=payload.title,
        description=payload.description,
        status="CREATED",
    )
    db.add(meeting)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    db.refresh(meeting)
    return meeting


def get_meeting(db: Session, meeting_id: str) -> models.Meeting:
    meeting = db.get(models.Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    return meeting


def start_meeting(db: Session, meeting_id: str) -> models.Meeting:
    with _MEETING_LOCK:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "IN_PROGRESS":
            return meeting  # idempotent: keep original started_at

        if meeting.status != "CREATED":
            # FINALIZING / COMPLETED (or anything else) cannot start
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot start (only CREATED)"
            )

        meeting.status = "IN_PROGRESS"
        if meeting.started_at is None:
            meeting.started_at = _now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(meeting)
        # Commit-before-publish (Step 5 Part 6): only broadcast on the real
        # transition (the idempotent branch above returned early). Non-blocking.
        sse_service.publish_meeting_status(meeting)
        return meeting


def finish_meeting(db: Session, meeting_id: str) -> models.Meeting:
    with _MEETING_LOCK:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "FINALIZING":
            return meeting  # idempotent: keep original ended_at
        if meeting.status == "COMPLETED":
            return meeting  # already finalized, never re-enter FINALIZING

        if meeting.status != "IN_PROGRESS":
            # CREATED or anything unexpected
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot finish (need IN_PROGRESS)"
            )

        meeting.status = "FINALIZING"
        if meeting.ended_at is None:
            meeting.ended_at = _now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(meeting)
        # Commit-before-publish (Step 5 Part 6). Real transition only.
        sse_service.publish_meeting_status(meeting)
        return meeting


def complete_after_summary(db: Session, meeting_id: str) -> models.Meeting:
    """FINALIZING → COMPLETED — THE ONLY Meeting-completion path (Step 7B §8).

    Called exclusively by the Finalizer AFTER the Summary has reached a
    terminal state (COMPLETED or FAILED). It is idempotent: an already
    COMPLETED meeting returns unchanged (no state regression, no re-touch of
    completed_at). CREATED / IN_PROGRESS are illegal transitions (409) — a
    meeting cannot be completed before its transcript is frozen + summary
    terminal.

    Precondition (checked by the caller): summary terminal. This method does
    not verify the summary itself — the Finalizer owns that gate.
    """
    with _MEETING_LOCK:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "COMPLETED":
            return meeting  # idempotent no-op

        if meeting.status != "FINALIZING":
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot be completed "
                f"(must be FINALIZING; only the Finalizer transitions to COMPLETED)"
            )

        meeting.status = "COMPLETED"
        if meeting.completed_at is None:
            meeting.completed_at = _now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(meeting)
        # Commit-before-publish (Step 5 Part 6 / Step 7B §11).
        sse_service.publish_meeting_status(meeting)
        return meeting
