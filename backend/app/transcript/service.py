"""Transcript ingress service.

Business rules (PRD §15-§24, ADR D4/D5, architecture §5):
  - Meeting must exist (404 via MeetingNotFoundError)
  - payload.meeting_id must equal path meeting_id (400, no silent override)
  - Meeting status must be IN_PROGRESS or FINALIZING to accept events (409 otherwise)
  - PARTIAL -> live_utterance_cache only (never the DB)
  - FINAL -> idempotency check -> persist -> commit ->
             then (future) SSE broadcast. Persist BEFORE broadcast.

This layer raises domain exceptions only; it never imports FastAPI.
"""

from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.meeting_model import Meeting
from app.transcript.segment_model import TranscriptSegment
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
from app.realtime import sse_service  # publish AFTER db.commit() only

# Meeting statuses that are allowed to receive transcript events (arch §4).
# IN_PROGRESS: normal receive. FINALIZING: ASR may still emit last few FINALs.
ACCEPTING_STATUSES = frozenset({"IN_PROGRESS", "FINALIZING"})


class FinalApplied(NamedTuple):
    """A FINAL that this call actually committed for the first time.

    Returned by `handle_event` so the HTTP application layer can react to the
    commit WITHOUT this domain layer knowing what reacts to it. `event_id` is
    carried for an exact idempotency check across a background hop (the seq
    alone cannot distinguish a re-delivery whose content conflicted).

    `segment_seq` is the Transcript-owned ordering (ASR-0B) — the only ordering
    any consumer is allowed to reason about.
    """

    segment_seq: int
    event_id: str


def _schedule_agent(background_tasks, meeting_id: str, applied: FinalApplied) -> None:
    """Hand a newly committed FINAL to the meeting-state consumer.

    Deliberately signalled from the HTTP application layer through the shared
    coalescing scheduler, and NOT via a callback inside the ASR adapter:

        ASR adapter  ->  Transcript Domain  ->  [application layer]  ->  agent

    The adapter never learns that an agent exists; the agent only ever reads
    persisted FINAL rows. `app.meeting_state.service` is imported lazily inside
    the function so the transcript package keeps NO import-time dependency on
    the agent package (and cannot grow an import cycle with it).

    A missing `background_tasks` still identifies direct legacy service callers
    that opted out of downstream scheduling. When present, no task is added to
    it; the shared scheduler owns coalescing and worker lifecycle.
    """
    if background_tasks is None:
        return
    from app.meeting_state.scheduler import meeting_state_scheduler

    meeting_state_scheduler.schedule(meeting_id)


def _canonical(seg: TranscriptSegment) -> tuple[str, str, int | None, int | None]:
    """Canonical content for idempotency comparison (ADR D5)."""
    return (seg.text, seg.speaker_id, seg.start_ms, seg.end_ms)


def _require_meeting_accepting(db: Session, meeting_id: str) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    if meeting.status not in ACCEPTING_STATUSES:
        raise InvalidMeetingStateTransition(
            f"meeting {meeting_id} status {meeting.status} cannot accept transcript "
            f"(only {sorted(ACCEPTING_STATUSES)})"
        )
    return meeting


def handle_event(
    db: Session,
    path_meeting_id: str,
    event: transcript_schemas.TranscriptEvent,
    *,
    background_tasks=None,
) -> FinalApplied | None:
    """Validate, gate, and apply a single transcript event.

    Returns a `FinalApplied` when this call committed a FINAL for the first time,
    else None (PARTIAL, or an idempotent FINAL retry). The return value is how a
    downstream consumer learns "a new FINAL landed, at seq N" without this layer
    knowing anything about that consumer.

    Both PARTIAL and FINAL run under the meeting's shared lock (lock_registry)
    so they are atomic w.r.t. the STREAM_END integrity check for the same
    meeting (Step 4 §9). PARTIAL has no DB side effects but we still serialize
    for determinism of the FINAL/STREAM_END boundary.

    Dual-dimension ingest gate (Step 6, constraint 2):
      - meeting.status       controls the MEETING lifecycle stage
        (IN_PROGRESS / FINALIZING accept; CREATED / COMPLETED -> 409)
      - persisted stream-end controls whether the TRANSCRIPT is frozen
        (accepted ACCEPTED row = durable freeze boundary; see below)
    The two responsibilities are distinct — not redundant state.
    """
    # --- Path/payload meeting_id consistency (PRD §49.1, arch §3) ---------
    if event.meeting_id != path_meeting_id:
        raise TranscriptValidationError(
            f"path meeting_id {path_meeting_id} != payload meeting_id {event.meeting_id}"
        )

    applied: FinalApplied | None = None
    with meeting_lock(path_meeting_id):
        if event.status == "PARTIAL":
            # --- Meeting existence + accepting state -------------------------
            _require_meeting_accepting(db, path_meeting_id)
            # Freeze hard gate FIRST: once the transcript is frozen, no PARTIAL
            # may even touch the live cache (constraint 3).
            _ensure_transcript_not_frozen(db, path_meeting_id)
            handle_partial(db, path_meeting_id, event)
        else:  # FINAL
            # Idempotent canonical retries bypass the accepting-state gate: a
            # client may replay an already-persisted FINAL AFTER the meeting has
            # been completed (Step 7B Finalizer auto-finalizes on first accepted
            # STREAM_END). A retry is the SAME content — not an append — so it
            # must still return 200, mirroring how STREAM_END retries allow a
            # COMPLETED meeting (stream_service). Only NEW FINAL is gated.
            if not _existing_final(db, path_meeting_id, event):
                _require_meeting_accepting(db, path_meeting_id)
            applied = handle_final(db, path_meeting_id, event)

    # Scheduled OUTSIDE the lock: `background_tasks.add_task` only registers a
    # callback (no business work happens here, and no lock is needed to do it).
    if applied is not None:
        _schedule_agent(background_tasks, path_meeting_id, applied)
    return applied


def _transcript_frozen(db: Session, meeting_id: str) -> bool:
    """True iff a durable ACCEPTED stream-end exists (freeze boundary)."""
    from app.transcript.stream_service import stream_has_ended

    return stream_has_ended(meeting_id)


def _ensure_transcript_not_frozen(db: Session, meeting_id: str) -> None:
    """Reject any NEW event once the transcript is frozen.

    NOTE: this is deliberately NOT applied to existing FINAL retries. A
    canonical existing FINAL concurrently retried while frozen must still get
    200 (constraint 4) — handled in handle_final via existing-by-event/seq.
    """
    if _transcript_frozen(db, meeting_id):
        raise TranscriptConflictError(
            meeting_id,
            "",
            message=f"transcript frozen for meeting {meeting_id}, cannot accept new transcript events",
        )


def _ensure_utterance_open(meeting_id: str, utterance_id: str) -> None:
    """Reject PARTIAL for an utterance that already has a persisted FINAL.

    ASR-0B freeze (decision 4): once an utterance reaches FINAL it is CLOSED.
    A later PARTIAL for the same utterance — the normal shape of an out-of-order
    or post-reconnect delivery — must NOT be accepted into the live cache:
    the frontend would render a permanent "识别中" line that can never be
    resolved, because the FINAL it waits for already arrived and will not be
    re-sent as a live frame.

    This follows the existing "contract drift must fail loudly" principle
    (no silent no-op): the caller learns the utterance is closed.

    Must be called under the meeting lock.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        closed = (
            transcript_repository.get_by_utterance_id(db, meeting_id, utterance_id)
            is not None
        )
    finally:
        db.close()
    if closed:
        raise TranscriptConflictError(
            meeting_id,
            "",
            message=(
                f"utterance {utterance_id} already reached FINAL and is closed; "
                f"PARTIAL for a finalized utterance is rejected"
            ),
        )


def handle_partial(
    db: Session,
    meeting_id: str,
    event: transcript_schemas.TranscriptEvent,
) -> None:
    """PARTIAL: upsert into live cache, never write the DB. PRD §22.

    Publish `transcript.partial` to SSE subscribers. PARTIAL is not a fact
    source (ADR D4/D10): the frame may be dropped if there is no subscriber,
    and is never replayed. This is a cache-only operation — no DB commit to
    wait on — so publishing from here is safe and non-blocking.
    """
    _ensure_utterance_open(meeting_id, event.utterance_id)
    live_utterance_cache.upsert(
        meeting_id,
        event.utterance_id,
        text=event.text,
        speaker_id=event.speaker_id,
        start_ms=event.start_ms,
        end_ms=event.end_ms,
        emitted_at=event.emitted_at,
    )
    # publish is non-blocking (bounded queue + evict-on-full); never gates ingest.
    sse_service.publish_partial(
        meeting_id,
        utterance_id=event.utterance_id,
        speaker_id=event.speaker_id,
        text=event.text,
        start_ms=event.start_ms,
        end_ms=event.end_ms,
    )


def _ensure_stream_not_ended(meeting_id: str, segment_seq: int):
    """Reject FINAL after an accepted STREAM_END (Part 8; no silent append).

    Must be called under the meeting lock (both FINAL and stream-end paths use
    meeting_lock), so this read is consistent with the STREAM_END write.
    """
    from app.transcript.stream_service import stream_has_ended

    if stream_has_ended(meeting_id):
        raise TranscriptConflictError(
            meeting_id,
            "",
            message=f"stream already ended for meeting {meeting_id}, cannot append FINAL seq={segment_seq}",
        )


def _existing_final(
    db: Session, meeting_id: str, event: transcript_schemas.TranscriptEvent
) -> bool:
    """True iff this FINAL event is a retry of an already-persisted segment.

    Matches the idempotency lookup inside handle_final (Case A: same event_id,
    Case C: same (meeting_id, segment_seq)). Used to let canonical retries pass
    the accepting-state gate after the meeting is COMPLETED.
    """
    if event.segment_seq is None:
        return False
    return (
        transcript_repository.get_by_event_id(db, meeting_id, event.event_id) is not None
        or transcript_repository.get_by_seq(db, meeting_id, event.segment_seq) is not None
    )


def handle_final(
    db: Session,
    meeting_id: str,
    event: transcript_schemas.TranscriptEvent,
) -> FinalApplied | None:
    """FINAL: idempotency -> persist -> commit -> broadcast. PRD §23, ADR D5.

    Returns a `FinalApplied` ONLY when a new row was committed; idempotent
    retries (Case A / Case C) return None so no downstream consumer is woken for
    a FINAL it has already seen.
    """
    if event.segment_seq is None:
        raise TranscriptValidationError(
            f"FINAL event {event.event_id} missing segment_seq"
        )

    # After an accepted STREAM_END, no new FINAL may be appended (Part 8).
    # Idempotent re-sends of an EXISTING FINAL are perfectly fine and handled
    # by the checks below, but a NEW segment must be rejected.
    existing_by_event = transcript_repository.get_by_event_id(db, meeting_id, event.event_id)
    existing_by_seq = transcript_repository.get_by_seq(db, meeting_id, event.segment_seq)
    is_existing = existing_by_event is not None or existing_by_seq is not None
    if not is_existing:
        _ensure_stream_not_ended(meeting_id, event.segment_seq)

    # Case A: same event_id already persisted.
    if existing_by_event is not None:
        _ensure_identical(existing_by_event, event, event_id_conflict=True)
        return None  # idempotent retry, no re-INSERT (PRD §24 / Case A)

    # Case C: same (meeting_id, segment_seq) already persisted (different event_id).
    if existing_by_seq is not None:
        _ensure_identical(existing_by_seq, event, event_id_conflict=False)
        return None  # same seq + same canonical content -> idempotent

    # New FINAL: create + persist + commit. Broadcast hook point below.
    segment = TranscriptSegment(
        meeting_id=meeting_id,
        segment_seq=event.segment_seq,
        utterance_id=event.utterance_id,
        event_id=event.event_id,
        speaker_id=event.speaker_id,
        start_ms=event.start_ms,
        end_ms=event.end_ms,
        text=event.text,
    )
    try:
        transcript_repository.insert_segment(db, segment)
    except IntegrityError:
        # event_id is globally unique (PRD §16). If the idempotency check above
        # missed it (e.g. committed concurrently / cross-meeting collision),
        # surface as a clean 409 rather than a 500.
        db.rollback()
        raise TranscriptConflictError(
            meeting_id,
            event.event_id,
            message=(
                f"transcript event_id already exists (global uniqueness, PRD §16): "
                f"{event.event_id}"
            ),
        )

    # FINAL 已固化：删除对应 PARTIAL cache（PRD §23，Part 8）
    live_utterance_cache.delete(meeting_id, event.utterance_id)

    # Commit-before-publish (ADR D4, Step 5 Part 4): insert_segment() above has
    # already committed. Only now do we broadcast. If a subscriber is slow/full
    # it is EVICTED (event_broker) — propagation never blocks this business path.
    sse_service.publish_final(meeting_id, segment)
    return FinalApplied(segment_seq=segment.segment_seq, event_id=segment.event_id)


def _ensure_identical(
    existing: TranscriptSegment,
    event: transcript_schemas.TranscriptEvent,
    *,
    event_id_conflict: bool,
) -> None:
    """Enforce idempotency vs conflict. PRD §24 (Case A/B/C).

    identical canonical content -> idempotent (ok)
    different content -> TranscriptConflictError (no silent overwrite)
    """
    existing_tuple = _canonical(existing)
    incoming_tuple = _canonical(
        TranscriptSegment(  # lightweight stand-in for comparison
            text=event.text,
            speaker_id=event.speaker_id,
            start_ms=event.start_ms,
            end_ms=event.end_ms,
            meeting_id=event.meeting_id,
            segment_seq=event.segment_seq if event.segment_seq is not None else -1,
            utterance_id=event.utterance_id,
            event_id=event.event_id,
        )
    )
    if existing_tuple != incoming_tuple:
        kind = "event_id" if event_id_conflict else "(meeting_id, segment_seq)"
        raise TranscriptConflictError(
            existing.meeting_id,
            event.event_id,
            message=(
                f"transcript conflict on {kind}: existing text={existing.text!r} "
                f"but incoming text={event.text!r}"
            ),
        )


def get_snapshot(
    db: Session, meeting_id: str
) -> tuple[Meeting, list[TranscriptSegment]]:
    """Snapshot: meeting (existence + status) + FINAL segments sorted by seq."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    segments = transcript_repository.list_sorted(db, meeting_id)
    return meeting, segments



