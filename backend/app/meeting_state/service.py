"""Meeting State Agent service — the consumer side of the FINAL stream.

Boundary (Step AGENT-1)
-----------------------
::

    FINAL committed (Transcript Domain)
        -> HTTP application layer schedules this service   (no MQ, no callback
        -> Batch buffer + drain pass                        into the adapter)
        -> LLM incremental update (previous state + new batch)
        -> Pydantic validation
        -> Meeting State repository (durable)

The agent NEVER talks to the ASR adapter and never sees an ASR-specific field.
Its only input is persisted FINAL rows: `segment_seq` + `text` + `speaker_id`.
Ordering is Transcript-owned (ASR-0B) and it only ever reads `segment_seq` ASC.
The adapter does not know this module exists, and this module does not know the
adapter exists.

Three properties this module is built around
--------------------------------------------

**Idempotent.** `last_processed_segment_seq` is the cursor: a FINAL at or below
it has already been folded in and is never processed twice. Duplicate delivery,
replay and crash-retry all collapse into the same no-op.

**Non-blocking.** The agent is a DOWNSTREAM consumer. `process_pending` is only
ever called from a background task, and it swallows every failure into a logged
status: a broken LLM must never fail a transcript write, break SSE, or hold the
meeting lock while a provider call is in flight. FINAL persistence is complete
before this module is ever scheduled.

**Non-destructive on failure.** A failed update moves ONLY `status`/`last_error`.
The persisted `state_json` and the cursor are left byte-for-byte intact, so the
next trigger replays exactly the segments that were not folded in.

Batch / flush policy
--------------------
The batch is NOT flushed per segment (one LLM call per FINAL would be ruinous in
cost and latency). A batch is flushed when it reaches `batch_max_segments`
SEGMENTS or `batch_max_chars` CHARACTERS — whichever comes first. No timers, no
scheduler thread: flushes are driven by arriving FINALs and, at the end of the
meeting, by the forced flush on freeze (see `process_pending(frozen=True)`),
which guarantees the trailing sentences are never lost.

Phase A / B / C
---------------
Same discipline as the Summary pipeline: the LLM call is made with NO lock and
NO open transaction, and the state is written back in a short critical section
that re-validates the cursor first. Holding the meeting lock across a provider
call would stall FINAL ingest for the same meeting — unacceptable.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.meeting.meeting_model import Meeting
from app.meeting_state import repository as state_repository
from app.meeting_state import updater
from app.meeting_state.prompts import build_repair_prompt, build_update_prompt
from app.meeting_state.schemas import MeetingState, MeetingStateContent
from app.common.meeting_lock import meeting_lock
from app.transcript import repository as transcript_repository
from app.transcript import stream_service
from app.transcript.segment_model import TranscriptSegment

logger = logging.getLogger(__name__)

# MeetingState.status values (NOT a meeting lifecycle state — the meeting state
# machine stays four-state; this is the agent's own working status).
STATUS_EMPTY = "EMPTY"
STATUS_ACTIVE = "ACTIVE"
STATUS_FAILED = "FAILED"
STATUS_FROZEN = "FROZEN"

# run outcome codes (logged, and returned for tests)
OUTCOME_OK = "OK"
OUTCOME_DISABLED = "DISABLED"
OUTCOME_NO_WORK = "NO_WORK"
OUTCOME_FAILED = "FAILED"
OUTCOME_SUPERSEDED = "SUPERSEDED"
OUTCOME_TOO_LARGE = "BATCH_TOO_LARGE"
OUTCOME_LIMIT_REACHED = "BATCH_LIMIT_REACHED"
OUTCOME_NOT_FOUND = "MEETING_NOT_FOUND"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# =============================================================================
# Batch buffering
# =============================================================================

def select_batches(
    segments: list[TranscriptSegment],
    *,
    max_segments: int,
    max_chars: int,
) -> tuple[list[list[TranscriptSegment]], list[TranscriptSegment]]:
    """Split pending segments into FLUSH-READY batches plus a trailing remainder.

    Returns `(ready, remainder)`. This distinction is the whole cost model:

      - `ready`      batches that REACHED a threshold — worth an LLM call now.
      - `remainder`  what is left over (below both thresholds). It is NOT
                     flushed; it stays buffered until more FINALs arrive or the
                     meeting ends (the forced flush). Flushing it eagerly would
                     mean one LLM call per segment, which is exactly what the
                     batching exists to prevent.

    Both bounds are needed: a burst of short utterances would never reach the
    char bound, and one long utterance would blow past a segment-only bound.

    A single segment larger than `max_chars` still forms its own ready batch
    (it is never split — splitting would corrupt `segment_seq` evidence); the
    caller's size guard then handles it explicitly rather than truncating.
    """
    ready: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    chars = 0

    for seg in segments:
        seg_chars = len(seg.text or "")
        if current and (len(current) >= max_segments or chars + seg_chars > max_chars):
            ready.append(current)
            current, chars = [], 0
        current.append(seg)
        chars += seg_chars
        if len(current) >= max_segments or chars >= max_chars:
            ready.append(current)
            current, chars = [], 0

    return ready, current


def _pending_segments(
    db: Session, meeting_id: str, after_seq: int
) -> list[TranscriptSegment]:
    """Persisted FINAL with segment_seq > after_seq, ASC (the unprocessed tail)."""
    return [
        seg
        for seg in transcript_repository.list_sorted(db, meeting_id)
        if seg.segment_seq > after_seq
    ]


# =============================================================================
# Merge
# =============================================================================

def merge_content(
    previous: MeetingStateContent, update: MeetingStateContent
) -> MeetingStateContent:
    """Fold one LLM update into the previous state.

    List semantics are REPLACE-WHEN-PRESENT, lenient-on-absent: the model is
    asked for the FULL updated list (it can read the previous state in the
    prompt), so a non-empty list replaces the old one and an omitted/empty list
    preserves it. That leniency is the safe direction — an empty list is much
    more likely to mean "no change" than "the meeting retracted every decision",
    and treating it as a retraction would silently destroy evidence-backed
    content on one bad model turn.

    Consequence, documented as a limitation: the model cannot INTENTIONALLY
    remove or edit an existing item in this step. Adding explicit removal
    semantics is AGENT-2 work.
    """
    return MeetingStateContent(
        current_topic=(
            update.current_topic if update.current_topic is not None else previous.current_topic
        ),
        topics=update.topics or previous.topics,
        decisions=update.decisions or previous.decisions,
        action_items=update.action_items or previous.action_items,
        open_questions=update.open_questions or previous.open_questions,
        risks=update.risks or previous.risks,
    )


# =============================================================================
# Prompt size guards (no silent truncation, same stance as the Summary pipeline)
# =============================================================================

def _guard_prompt_sizes(state_text: str, batch_text: str) -> str | None:
    """Return a reason string when the prompt material exceeds a hard bound."""
    if len(batch_text) > settings.meeting_agent_max_batch_chars:
        return (
            f"batch too large ({len(batch_text)} chars > "
            f"{settings.meeting_agent_max_batch_chars}); refusing to silently truncate"
        )
    if len(state_text) > settings.meeting_agent_max_state_chars:
        return (
            f"meeting state too large ({len(state_text)} chars > "
            f"{settings.meeting_agent_max_state_chars}); refusing to silently truncate"
        )
    return None


# =============================================================================
# The drain pass
# =============================================================================

def process_pending(
    db: Session,
    meeting_id: str,
    *,
    provider=None,
    force_flush: bool = False,
) -> dict:
    """Fold every pending FINAL into the meeting state, at most N batches.

    `force_flush` relaxes the batch bounds: at freeze time the remaining tail is
    sent as ONE final batch even if it is under the thresholds, so a meeting that
    ends mid-batch still folds in its last sentences (§ eleven of the step spec).

    Returns a small run report (also the thing tests assert on). Never raises for
    business reasons: a failure is recorded on the state row and reported here,
    because this runs detached from the request that triggered it.
    """
    run_id = uuid.uuid4().hex[:8]
    started = time.monotonic()

    if not settings.meeting_agent_enabled:
        return {"outcome": OUTCOME_DISABLED, "agent_run_id": run_id}

    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        return {"outcome": OUTCOME_NOT_FOUND, "agent_run_id": run_id}

    # --- Phase A: read state + batch under the lock (short, no LLM) ---------
    with meeting_lock(meeting_id):
        row = state_repository.get(db, meeting_id)
        cursor = row.last_processed_segment_seq if row is not None else 0
        version = row.version if row is not None else 0
        try:
            previous = state_repository.get_content(db, meeting_id)
        except state_repository.MeetingStateCorruptError as exc:
            # Refuse to overwrite a state we cannot read: that would discard
            # content silently. Report and stop; a human looks at the row.
            logger.error(
                "meeting state unreadable; refusing to update",
                extra={"meeting_id": meeting_id, "agent_run_id": run_id},
            )
            return {"outcome": OUTCOME_FAILED, "agent_run_id": run_id,
                    "reason": str(exc), "from_segment_seq": cursor, "to_segment_seq": cursor}
        frozen = stream_service.is_frozen(db, meeting_id)
        pending = _pending_segments(db, meeting_id, cursor)

    if not pending:
        # Nothing new. If the transcript is frozen, this is the moment to close
        # the state out (idempotent: FROZEN -> FROZEN is a no-op write).
        #
        # A FAILED row is left alone: it means the last update did not land, and
        # the pending segments that failed are still pending — they cannot be
        # zero here. If they ARE zero, the failure was on a batch that has since
        # been superseded and there is nothing left to do; FROZEN would then be
        # an unearned claim, so we report NO_WORK and leave the failure visible.
        if frozen and row is not None and row.status == STATUS_ACTIVE:
            with meeting_lock(meeting_id):
                state_repository.upsert(
                    db, meeting_id,
                    content=previous,
                    version=version,
                    last_processed_segment_seq=cursor,
                    status=STATUS_FROZEN,
                    model_name=row.model_name,
                )
            return {"outcome": OUTCOME_OK, "agent_run_id": run_id, "status": STATUS_FROZEN,
                    "from_segment_seq": cursor, "to_segment_seq": cursor,
                    "previous_state_version": version, "new_state_version": version,
                    "batches": 0}
        return {"outcome": OUTCOME_NO_WORK, "agent_run_id": run_id}

    # --- batching -----------------------------------------------------------
    # Normal runs flush only threshold-reaching batches and LEAVE the remainder
    # buffered (no timer scheduler: it is picked up by the next FINAL, or by the
    # forced flush at finish time — §5/§11).
    #
    # A forced flush drains the remainder too, but still through the SAME
    # batching bounds: the bounds keep one prompt bounded, so exceeding them
    # just means "more calls". Merging everything into one oversized call would
    # be rejected by the size guard below — losing exactly the trailing
    # sentences the forced flush exists to save.
    if force_flush:
        batches = _chunk(pending, max_segments=settings.meeting_agent_batch_max_segments,
                         max_chars=settings.meeting_agent_batch_max_chars)
    else:
        batches, _remainder = select_batches(
            pending,
            max_segments=settings.meeting_agent_batch_max_segments,
            max_chars=settings.meeting_agent_batch_max_chars,
        )

    if not batches:
        # Everything pending is below the thresholds: buffer it and wait. The
        # state (and its cursor) is untouched, so nothing is lost or re-read.
        return {"outcome": OUTCOME_NO_WORK, "agent_run_id": run_id,
                "buffered_segment_seqs": [s.segment_seq for s in pending],
                "from_segment_seq": cursor, "to_segment_seq": cursor,
                "previous_state_version": version, "new_state_version": version,
                "batches": 0}

    limit = len(batches) if force_flush else max(1, settings.meeting_agent_max_batches_per_run)
    truncated = len(batches) > limit
    batches = batches[:limit]
    processed_count = sum(len(b) for b in batches)

    if provider is None:
        from app.agent.llm.factory import get_provider

        provider = get_provider()

    processed_batches = 0
    from_seq = cursor
    to_seq = cursor

    for batch in batches:
        batch_end = batch[-1].segment_seq
        batch_text = "\n".join(f"[{s.segment_seq}] <{s.speaker_id}> {s.text}" for s in batch)
        state_text = _state_text_for_guard(previous)

        reason = _guard_prompt_sizes(state_text, batch_text)
        if reason is not None:
            # No silent truncation: skip the batch, leave the state untouched,
            # and report. A later trigger retries (the segments stay pending).
            state_repository.mark_failed(db, meeting_id, reason=reason,
                                         model_name=getattr(provider, "name", None))
            _log_run(meeting_id, run_id, from_seq, batch_end, version, version, 0,
                     OUTCOME_TOO_LARGE, reason)
            return {"outcome": OUTCOME_TOO_LARGE, "agent_run_id": run_id, "reason": reason,
                    "from_segment_seq": from_seq, "to_segment_seq": batch_end,
                    "previous_state_version": version, "new_state_version": version,
                    "batches": processed_batches}

        prompt = build_update_prompt(
            meeting.title, previous, batch, last_processed_segment_seq=cursor
        )

        # --- Phase B: LLM call — NO lock, NO open transaction ---------------
        llm_started = time.monotonic()
        result = updater.run_update(
            provider,
            prompt,
            build_repair_prompt=build_repair_prompt,
            max_attempts=settings.meeting_agent_max_attempts,
            repair_enabled=settings.meeting_agent_parse_repair,
        )
        latency_ms = int((time.monotonic() - llm_started) * 1000)

        if not result.ok:
            # Failure policy: record, do NOT advance the cursor, do NOT touch
            # state_json. The next trigger replays exactly these segments.
            with meeting_lock(meeting_id):
                state_repository.mark_failed(
                    db, meeting_id, reason=result.reason or "unknown error",
                    model_name=getattr(provider, "name", None),
                )
            _log_run(meeting_id, run_id, from_seq, batch_end, version, version,
                     latency_ms, OUTCOME_FAILED, result.reason)
            return {"outcome": OUTCOME_FAILED, "agent_run_id": run_id,
                    "reason": result.reason, "from_segment_seq": from_seq,
                    "to_segment_seq": batch_end, "previous_state_version": version,
                    "new_state_version": version, "batches": processed_batches}

        merged = merge_content(previous, result.parsed)

        # --- Phase C: write back under the lock, re-validating the cursor ----
        with meeting_lock(meeting_id):
            fresh = state_repository.get(db, meeting_id)
            fresh_cursor = fresh.last_processed_segment_seq if fresh is not None else 0
            fresh_version = fresh.version if fresh is not None else 0
            if fresh_cursor != cursor:
                # Another run advanced the state while we were in the LLM call.
                # Our update was derived from a now-stale state: discard it
                # (never persist) and let the next trigger replay what is left.
                _log_run(meeting_id, run_id, from_seq, batch_end, version, fresh_version,
                         latency_ms, OUTCOME_SUPERSEDED, None)
                return {"outcome": OUTCOME_SUPERSEDED, "agent_run_id": run_id,
                        "from_segment_seq": from_seq, "to_segment_seq": batch_end,
                        "previous_state_version": version,
                        "new_state_version": fresh_version, "batches": processed_batches}

            new_version = fresh_version + 1
            status = STATUS_FROZEN if (frozen and batch_end == pending[-1].segment_seq) else STATUS_ACTIVE
            state_repository.upsert(
                db, meeting_id,
                content=merged,
                version=new_version,
                last_processed_segment_seq=batch_end,
                status=status,
                model_name=getattr(provider, "name", None),
                last_error=None,
            )

        _log_run(meeting_id, run_id, from_seq, batch_end, version, new_version,
                 latency_ms, OUTCOME_OK, None)
        # Advance our own view of the state WITH the commit. `cursor` is what the
        # next iteration's Phase C staleness check compares against, and what the
        # next prompt reports as already-processed — leaving it at the value from
        # Phase A would make every batch after the first look superseded and
        # silently abandon the rest of the run.
        previous = merged
        version = new_version
        cursor = batch_end
        to_seq = batch_end
        processed_batches += 1

    outcome = OUTCOME_LIMIT_REACHED if truncated else OUTCOME_OK
    if truncated:
        logger.info(
            "meeting state batch limit reached; remaining segments left for the next trigger",
            extra={"meeting_id": meeting_id, "agent_run_id": run_id},
        )
    return {
        "outcome": outcome,
        "agent_run_id": run_id,
        "from_segment_seq": from_seq,
        "to_segment_seq": to_seq,
        "previous_state_version": version - processed_batches,
        "new_state_version": version,
        "batches": processed_batches,
        "remaining_segment_seqs": [s.segment_seq for s in pending[processed_count:]],
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _chunk(
    segments: list[TranscriptSegment], *, max_segments: int, max_chars: int
) -> list[list[TranscriptSegment]]:
    """Split into ALL batches including the trailing one (forced-flush path)."""
    ready, remainder = select_batches(
        segments, max_segments=max_segments, max_chars=max_chars
    )
    if remainder:
        ready.append(remainder)
    return ready


def _state_text_for_guard(content: MeetingStateContent) -> str:
    import json

    return json.dumps(content.model_dump(), ensure_ascii=False)


def _log_run(
    meeting_id: str,
    run_id: str,
    from_seq: int,
    to_seq: int,
    prev_version: int,
    new_version: int,
    latency_ms: int,
    status: str,
    reason: str | None,
) -> None:
    """One structured log line per update attempt.

    Fields required by the step spec: meeting_id, agent_run_id,
    from/to_segment_seq, previous/new_state_version, llm_latency_ms, status.
    Transcript TEXT is deliberately never logged (only seq numbers), and no
    prompt / raw model output / API key ever reaches the log.
    """
    logger.info(
        "meeting_state_update meeting_id=%s agent_run_id=%s from_segment_seq=%s "
        "to_segment_seq=%s previous_state_version=%s new_state_version=%s "
        "llm_latency_ms=%s status=%s%s",
        meeting_id, run_id, from_seq, to_seq, prev_version, new_version,
        latency_ms, status, f" reason={reason}" if reason else "",
    )


# =============================================================================
# Read / write entry points
# =============================================================================

def get_state(db: Session, meeting_id: str) -> MeetingState:
    """Current meeting state (empty, version 0, when nothing processed yet)."""
    from app.common.exceptions import MeetingNotFoundError

    if db.get(Meeting, meeting_id) is None:
        raise MeetingNotFoundError(meeting_id)

    row = state_repository.get(db, meeting_id)
    if row is None:
        return MeetingState(meeting_id=meeting_id, version=0,
                            last_processed_segment_seq=0, status=STATUS_EMPTY)

    content = state_repository.get_content(db, meeting_id)
    return MeetingState(
        meeting_id=meeting_id,
        version=row.version,
        last_processed_segment_seq=row.last_processed_segment_seq,
        status=row.status,
        model_name=row.model_name,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        **content.model_dump(),
    )


def run_agent_background(
    meeting_id: str,
    *,
    force_flush: bool = False,
    provider=None,
) -> None:
    """BackgroundTasks entry point — owns its own DB session.

    Mirrors `finalizer_service.run_finalizer_background`: the request-scoped
    session is never used after the response returns. EVERY exception is caught
    and logged, because this is downstream work: an agent failure must never
    surface as a transcript failure.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        process_pending(db, meeting_id, provider=provider, force_flush=force_flush)
    except Exception:  # pragma: no cover - defensive; transcript is already durable
        logger.exception(
            "meeting state agent failed for meeting %s (transcript is already durable; "
            "state update is retryable)", meeting_id,
        )
    finally:
        db.close()


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_FROZEN",
    "merge_content",
    "process_pending",
    "select_batches",
    "get_state",
    "run_agent_background",
]