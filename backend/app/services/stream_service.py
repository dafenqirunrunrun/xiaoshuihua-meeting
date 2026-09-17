"""Transcript stream-end service (Step 4).

Contract: POST /api/internal/meetings/{meeting_id}/stream-end

Rules (see api-contract §Stream End):
  - meeting must exist (404)
  - event_type must be TRANSCRIPT_STREAM_END (schemas enforce)
  - state gate (Part 4): only FINALIZING accepts the FIRST stream-end.
    CREATED / IN_PROGRESS -> 409. COMPLETED allowed only as idempotent retry
    of an already-persisted identical stream-end.
  - integrity check (Part 6): FINAL segments must be exactly 1..last_segment_seq;
    explicit gaps -> 409 (missing=[...]); max(seq) > last -> 409 (overrun).
  - persistence (Part 5): durable row in transcript_stream (survives restart).
  - idempotency (Part 7): same last_segment_seq already accepted -> 200 no-op
    (never changes received_at, never re-triggers future actions).
    Different last after accepted -> 409 (one terminal point per stream).
  - FINAL after stream-end: rejected in transcript_service (Part 8).

All checks + writes run under the same per-meeting lock as FINAL ingest
(lock_registry.meeting_lock) — shared sync boundary (Part 9).
"""

from sqlalchemy.orm import Session

from app import models
from app.exceptions import (
    InvalidMeetingStateTransition,
    MeetingNotFoundError,
    TranscriptConflictError,
    TranscriptValidationError,
)
from app.repositories import transcript_repository
from app.schemas import transcript as transcript_schemas
from app.services.lock_registry import meeting_lock


def stream_has_ended(meeting_id: str) -> bool:
    """Whether an accepted stream-end exists for the meeting.

    NOTE: callers must already hold meeting_lock(meeting_id) for consistency.
    This reads straight from the DB (durable) within that lock scope.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return transcript_repository.get_stream_end(db, meeting_id) is not None
    finally:
        db.close()


def _find_gaps(db: Session, meeting_id: str, last_seq: int) -> list[int]:
    """Explicit missing seqs in [1..last_seq] given the persisted FINAL set."""
    segs = transcript_repository.list_sorted(db, meeting_id)
    present = {s.segment_seq for s in segs if s.segment_seq <= last_seq}
    return [seq for seq in range(1, last_seq + 1) if seq not in present]


def handle_stream_end(
    db: Session,
    meeting_id: str,
    payload: transcript_schemas.StreamEndEvent,
) -> tuple[models.Meeting, models.TranscriptStream, bool]:
    """Validate, persist (idempotently), run integrity check. Returns (meeting, stream, complete)."""
    if payload.event_type != "TRANSCRIPT_STREAM_END":
        raise TranscriptValidationError(
            f"event_type must be TRANSCRIPT_STREAM_END, got {payload.event_type}"
        )
    if payload.last_segment_seq is None:
        raise TranscriptValidationError("last_segment_seq is required")
    if payload.meeting_id != meeting_id:
        raise TranscriptValidationError(
            f"path meeting_id {meeting_id} != payload meeting_id {payload.meeting_id}"
        )

    with meeting_lock(meeting_id):
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        existing = transcript_repository.get_stream_end(db, meeting_id)

        # --- Idempotency (Part 7) ------------------------------------------
        if existing is not None:
            if existing.last_segment_seq == payload.last_segment_seq:
                # identical retry -> 200, no state change, keep first received_at
                return meeting, existing, True
            raise TranscriptConflictError(
                meeting_id,
                "",
                message=(
                    f"stream end already accepted with last_segment_seq={existing.last_segment_seq}, "
                    f"cannot replace with {payload.last_segment_seq} (one terminal point per stream)"
                ),
            )

        # --- First stream end: state gate (Part 4) --------------------------
        if meeting.status not in {"FINALIZING", "COMPLETED"}:
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot accept stream-end "
                f"(must be FINALIZING)"
            )

        # --- Integrity check (Part 6) ---------------------------------------
        max_seq = transcript_repository.max_seq(db, meeting_id)
        # overrun: more FINAL in DB than the declared end
        if max_seq is not None and max_seq > payload.last_segment_seq:
            raise TranscriptConflictError(
                meeting_id,
                "",
                message=(
                    f"transcript overruns stream-end: DB max seq={max_seq} > "
                    f"last_segment_seq={payload.last_segment_seq}"
                ),
            )
        gaps = _find_gaps(db, meeting_id, payload.last_segment_seq)
        if gaps:
            raise TranscriptConflictError(
                meeting_id,
                "",
                message=f"transcript incomplete, missing segments: {gaps}",
            )

        # --- Persist (Part 5: durable across restart) -----------------------
        stream_row = transcript_repository.insert_stream_end(
            db, meeting_id, payload.last_segment_seq
        )
        return meeting, stream_row, True
