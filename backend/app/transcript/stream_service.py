"""Transcript stream-end service (Step 4) + Step 6 Freeze/Readiness.

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

Step 6 (Transcript Freeze + Finalization Readiness):

  - The accepted `transcript_stream` row (status=ACCEPTED + last_segment_seq)
    IS the **durable transcript freeze boundary**. Reusing these fields keeps
    the freeze state persistable and restart-proof; no new columns / states.
  - The accepted STREAM_END already implies the integrity check passed, so a
    later readiness read is derived from that durable row (see
    `is_finalization_ready`), not from re-scanning every segment.
  - The lock-holder who persisted the freeze is the one who clears the
    **transient** PARTIAL cache — but ONLY AFTER the commit succeeds. If the
    integrity check fails (409) no cache is touched: the transcript is not
    frozen, so residual PARTIAL must survive for the retry path.

All checks + writes run under the same per-meeting lock as FINAL ingest
(lock_registry.meeting_lock) — shared sync boundary (Part 9).
"""

from sqlalchemy.orm import Session

from app.meeting.meeting_model import Meeting
from app.transcript.stream_model import TranscriptStream
from app.common.exceptions import (
    InvalidMeetingStateTransition,
    MeetingNotFoundError,
    TranscriptConflictError,
    TranscriptValidationError,
)
from app.transcript import repository as transcript_repository
from app.transcript import schemas as transcript_schemas
from app.transcript.live_cache import live_utterance_cache
from app.common.meeting_lock import meeting_lock


def stream_has_ended(meeting_id: str) -> bool:
    """Whether an accepted stream-end exists for the meeting (durable freeze).

    NOTE: callers must already hold meeting_lock(meeting_id) for consistency.
    This reads straight from the DB (durable) within that lock scope — survives
    process restarts (Step 6 readiness is derived from this row, not from
    in-memory state).
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return transcript_repository.get_stream_end(db, meeting_id) is not None
    finally:
        db.close()


def _get_stream_end(db: Session, meeting_id: str) -> TranscriptStream | None:
    """Durable stream-end row for a meeting (None until freeze)."""
    return transcript_repository.get_stream_end(db, meeting_id)


def _readiness_meeting(db: Session, meeting_id: str) -> Meeting:
    """Meeting existence for readiness helpers; raises 404 as elsewhere."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    return meeting


def get_finalization_readiness(db: Session, meeting_id: str) -> dict:
    """Derived finalization readiness (Step 6). NO persisted flag.

    Conditions (constraint 6) — the ORIGINAL initial-generation gate:
      - meeting exists
      - meeting.status == FINALIZING
      - accepted transcript_stream exists (durable freeze boundary)
      - last_segment_seq persisted

    The accepted STREAM_END already proves integrity passed at freeze time, so
    this is a cheap durable-row check — no full gap re-scan.
    """
    meeting = _readiness_meeting(db, meeting_id)
    stream = _get_stream_end(db, meeting_id)
    ready = (
        meeting.status == "FINALIZING"
        and stream is not None
        and stream.last_segment_seq is not None
        and stream.status == "ACCEPTED"
    )
    return {
        "meeting_id": meeting_id,
        "meeting_status": meeting.status,
        "stream_ended": stream is not None,
        "last_segment_seq": stream.last_segment_seq if stream else None,
        "finalization_ready": ready,
    }


def is_frozen(db: Session, meeting_id: str) -> bool:
    """Durable transcript-freeze check ONLY (status-agnostic, Step 7B §21).

    Regeneration on a COMPLETED meeting must still satisfy D6 ("only frozen
    transcript enters the LLM") without demanding FINALIZING — the transcript
    was frozen once at the first accepted STREAM_END and COMPLETED is a legal
    (actually the normal) regeneration host (§19). Whether the meeting is
    FINALIZING or COMPLETED does not change the freeze fact.
    """
    meeting = _readiness_meeting(db, meeting_id)
    stream = _get_stream_end(db, meeting_id)
    return (
        stream is not None
        and stream.last_segment_seq is not None
        and stream.status == "ACCEPTED"
    )


def is_finalization_ready(db: Session, meeting_id: str) -> bool:
    """Boolean form of readiness (for the future Step 7 Finalizer gate)."""
    return get_finalization_readiness(db, meeting_id)["finalization_ready"]


def _find_gaps(db: Session, meeting_id: str, last_seq: int) -> list[int]:
    """Explicit missing seqs in [1..last_seq] given the persisted FINAL set.

    `last_seq=0` (empty transcript) legitimately yields no gaps: range(1, 1)
    is empty — that is the transcript being complete-but-empty, not a failure.
    """
    segs = transcript_repository.list_sorted(db, meeting_id)
    present = {s.segment_seq for s in segs if s.segment_seq <= last_seq}
    return [seq for seq in range(1, last_seq + 1) if seq not in present]


def _resolve_terminal_seq(
    db: Session, meeting_id: str, declared: int | None
) -> int:
    """Effective terminal segment_seq for this stream-end (ASR-0B freeze).

    `segment_seq` is Transcript-owned, so the collaborator is NOT required to
    reverse-query the server's ordering (ASR-0B decision 2). When the caller
    omits `last_segment_seq` we derive the terminal point from what is actually
    persisted. An empty transcript ends at 0 — a legal terminal value that lets
    the meeting leave FINALIZING (previously impossible: `ge=1` + "required"
    made zero-segment meetings permanently stuck in FINALIZING).

    A caller that DOES supply the value still gets the full gap/overrun
    validation — the explicit field is retained purely as an extra
    consistency-checking capability (decision 2).
    """
    if declared is not None:
        return declared
    return transcript_repository.max_seq(db, meeting_id) or 0


def handle_stream_end(
    db: Session,
    meeting_id: str,
    payload: transcript_schemas.StreamEndEvent,
) -> tuple[Meeting, TranscriptStream, bool, bool]:
    """Validate, persist (idempotently), run integrity check.

    Returns (meeting, stream, complete, newly_accepted):
      - complete (bool): transcript is complete & frozen (mirrors transcript_complete).
      - newly_accepted (bool): True ONLY for the FIRST accepted stream-end of
        this meeting; False for an identical idempotent retry. The Finalizer
        triggers the initial Summary generation on newly_accepted=True and
        MUST NOT on retries (Step 7B §5 — a retry must not re-run DeepSeek).
    """
    if payload.event_type != "TRANSCRIPT_STREAM_END":
        raise TranscriptValidationError(
            f"event_type must be TRANSCRIPT_STREAM_END, got {payload.event_type}"
        )
    if payload.meeting_id != meeting_id:
        raise TranscriptValidationError(
            f"path meeting_id {meeting_id} != payload meeting_id {payload.meeting_id}"
        )

    with meeting_lock(meeting_id):
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        terminal_seq = _resolve_terminal_seq(db, meeting_id, payload.last_segment_seq)

        existing = transcript_repository.get_stream_end(db, meeting_id)

        # --- Idempotency (Part 7) ------------------------------------------
        if existing is not None:
            if existing.last_segment_seq == terminal_seq:
                # identical retry -> 200, no state change, keep first received_at
                return meeting, existing, True, False
            raise TranscriptConflictError(
                meeting_id,
                "",
                message=(
                    f"stream end already accepted with last_segment_seq={existing.last_segment_seq}, "
                    f"cannot replace with {terminal_seq} (one terminal point per stream)"
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
        if max_seq is not None and max_seq > terminal_seq:
            raise TranscriptConflictError(
                meeting_id,
                "",
                message=(
                    f"transcript overruns stream-end: DB max seq={max_seq} > "
                    f"last_segment_seq={terminal_seq}"
                ),
            )
        gaps = _find_gaps(db, meeting_id, terminal_seq)
        if gaps:
            raise TranscriptConflictError(
                meeting_id,
                "",
                message=f"transcript incomplete, missing segments: {gaps}",
            )

        # --- Persist (Part 5: durable across restart) -----------------------
        # insert_stream_end() commits here — the durable freeze boundary is now
        # durable (ACCEPTED + last_segment_seq). Only AFTER this commit succeeds
        # may we touch the transient PARTIAL cache (constraint 5).
        stream_row = transcript_repository.insert_stream_end(
            db, meeting_id, terminal_seq
        )
        # Durable freeze success -> clear this meeting's transient PARTIAL cache.
        # This is TEMPORARY-STATE cleanup only, NOT the source of truth for
        # finalization readiness (that is the committed transcript_stream row).
        live_utterance_cache.clear_meeting(meeting_id)
        return meeting, stream_row, True, True



