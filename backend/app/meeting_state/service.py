"""Meeting State Agent service — the consumer side of the FINAL stream.

Boundary (Step AGENT-1)
-----------------------
::

    FINAL committed (Transcript Domain)
        -> application layer schedules this service        (no MQ, no callback
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
The batch is NOT flushed per segment. The process-local scheduler tracks one
pending high-watermark per meeting and triggers on segment count, silence or a
bounded interval. One run performs at most one LLM call over one larger
contiguous window. Finalization never drains or waits for this pipeline.

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

from sqlalchemy.orm import Session

from app.config import settings
from app.meeting.meeting_model import Meeting
from app.meeting_state import repository as state_repository
from app.meeting_state import snapshot_repository
from app.meeting_state import updater
from app.meeting_state.prompts import build_repair_prompt, build_update_prompt
from app.meeting_state.schemas import MeetingState, MeetingStateContent, MeetingStatePublic
from app.common.meeting_lock import meeting_lock
from app.realtime import sse_service  # publish AFTER db.commit() only
from app.transcript import repository as transcript_repository
from app.transcript import stream_service
from app.transcript.segment_model import TranscriptSegment

logger = logging.getLogger(__name__)

# MeetingState.status values (NOT a meeting lifecycle state — the meeting state
# machine stays four-state; this is the agent's own working status).
STATUS_EMPTY = "EMPTY"
STATUS_ACTIVE = "ACTIVE"
STATUS_STALE = "STALE"
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


def _publish_state_changed(
    meeting_id: str,
    *,
    version: int,
    status: str,
    previous_version: int,
    previous_status: str | None,
) -> None:
    """Announce a PRODUCT-VISIBLE durable state change (Step UI-INTEL-1).

    The trigger condition is deliberately "the durable state actually changed",
    NOT "the version incremented": a failed update changes what the user must be
    told (the AI is unavailable) while leaving `version` untouched, so a
    version-gated publish would hide exactly the transition the panel needs.

        ACTIVE vN -> ACTIVE vN+1   version moved      -> publish
        ACTIVE -> STALE            status moved       -> publish
        EMPTY -> FAILED            status moved       -> publish
        STALE|FAILED -> ACTIVE     status + version   -> publish
        ACTIVE -> FROZEN           status moved       -> publish
        STALE -> STALE             neither moved      -> SUPPRESSED

    That last row is why this is a predicate and not a bare call: a retry that
    fails again re-writes the same degraded status, and waking the panel for a
    no-op would make an outage generate a re-fetch per retry. The comparison is
    against the status/version read immediately BEFORE the write, so it reflects
    the real prior durable state rather than the state at the start of the run.

    Called only AFTER the row committed, and can never raise:

    **Commit-before-publish.** The durable state is already on disk, so a failed
    announce must never roll it back — SSE is a notification channel, not a fact
    source. A client that misses the event still converges, because every connect
    / refresh / reconnect re-reads GET /api/meetings/{id}/state.

    **Never raises.** This runs inside `process_pending`, whose contract is that
    an agent problem never propagates to the transcript path. A broken broker or
    a slow-consumer eviction must stay contained here instead of surfacing as a
    bogus agent failure — the state itself is perfectly fine.
    """
    if status == previous_status and version == previous_version:
        return
    try:
        sse_service.publish_meeting_state_updated(meeting_id, version=version, status=status)
    except Exception:  # pragma: no cover - defensive; SSE is not a fact source
        logger.exception(
            "meeting.state.updated publish failed for meeting %s "
            "(state is already durable; client recovers via GET /state)", meeting_id,
        )


def _current_state_identity(db: Session, meeting_id: str) -> tuple[str | None, int]:
    """(status, version) of the persisted row as it stands RIGHT NOW.

    Read immediately before a write so the publish predicate compares against the
    real prior durable state — within one run the persisted status can already
    have moved (an earlier batch succeeded, then a later one failed), so a value
    captured back in Phase A would be stale.

    Returns a PLAIN TUPLE, never the ORM row, and that is load-bearing: SQLAlchemy
    hands back the same identity-mapped instance every time, and `repository.upsert`
    mutates that instance in place. Holding the row object across the write and
    reading `row.status` afterwards would therefore report the NEW status as if it
    were the old one — making every status-only change look like "no change" and
    silently suppressing exactly the events this predicate exists to emit.
    """
    row = state_repository.get(db, meeting_id)
    if row is None:
        return None, 0
    return row.status, row.version


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

    The prompt asks for the FULL updated state. A field explicitly returned by
    the model therefore replaces the previous value, including ``[]`` / null:
    that is how a resolved question, corrected topic or retracted proposal can
    disappear. A genuinely omitted field remains lenient and preserves the
    previous value, which keeps older/scripted providers backward compatible.
    """
    fields_set = update.model_fields_set

    def value(name: str):
        return getattr(update, name) if name in fields_set else getattr(previous, name)

    return MeetingStateContent(
        live_summary=value("live_summary"),
        current_topic=value("current_topic"),
        topics=value("topics"),
        decisions=value("decisions"),
        action_items=value("action_items"),
        open_questions=value("open_questions"),
        risks=value("risks"),
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
    if meeting.status != "IN_PROGRESS":
        return {
            "outcome": OUTCOME_NO_WORK,
            "agent_run_id": run_id,
            "reason": f"meeting status is {meeting.status}; realtime processing stopped",
        }

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
        pending = _pending_segments(db, meeting_id, cursor)

    if not pending:
        return {"outcome": OUTCOME_NO_WORK, "agent_run_id": run_id}

    # A realtime pass is one coalesced high-watermark window and therefore at
    # most one provider call. The old implementation split a ready range into
    # many 8-segment calls and synchronously drained all of them, which merely
    # hid an unbounded LLM queue inside one worker.
    if not force_flush:
        pending_chars = sum(len(segment.text or "") for segment in pending)
        if (
            len(pending) < settings.meeting_agent_batch_max_segments
            and pending_chars < settings.meeting_agent_batch_max_chars
        ):
            return {"outcome": OUTCOME_NO_WORK, "agent_run_id": run_id,
                    "buffered_segment_seqs": [s.segment_seq for s in pending],
                    "from_segment_seq": cursor, "to_segment_seq": cursor,
                    "previous_state_version": version, "new_state_version": version,
                    "batches": 0}

    batch = _coalesced_window(
        pending, max_chars=settings.meeting_agent_max_batch_chars
    )

    if provider is None:
        from app.agent.llm.factory import get_provider

        provider = get_provider(max_retries=0, retry_timeouts=False)

    from_seq = cursor
    batch_end = batch[-1].segment_seq
    batch_text = "\n".join(
        f"[{s.segment_seq}] <{s.speaker_id}> {s.text}" for s in batch
    )
    state_text = _state_text_for_guard(previous)
    reason = _guard_prompt_sizes(state_text, batch_text)
    if reason is not None:
        with meeting_lock(meeting_id):
            before_status, before_version = _current_state_identity(db, meeting_id)
            failed = state_repository.mark_failed(
                db, meeting_id, reason=reason,
                model_name=getattr(provider, "name", None),
            )
        _publish_state_changed(meeting_id, version=version, status=failed.status,
                               previous_version=before_version,
                               previous_status=before_status)
        _log_run(meeting_id, run_id, from_seq, batch_end, version, version, 0,
                 OUTCOME_TOO_LARGE, reason)
        return {"outcome": OUTCOME_TOO_LARGE, "agent_run_id": run_id,
                "reason": reason, "from_segment_seq": from_seq,
                "to_segment_seq": batch_end, "previous_state_version": version,
                "new_state_version": version, "batches": 0}

    prompt = build_update_prompt(
        meeting.title, previous, batch, last_processed_segment_seq=cursor
    )
    snapshot_repository.record_attempt(db, meeting_id)
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
        with meeting_lock(meeting_id):
            before_status, before_version = _current_state_identity(db, meeting_id)
            failed = state_repository.mark_failed(
                db, meeting_id, reason=result.reason or "unknown error",
                model_name=getattr(provider, "name", None),
            )
        _publish_state_changed(meeting_id, version=version, status=failed.status,
                               previous_version=before_version,
                               previous_status=before_status)
        _log_run(meeting_id, run_id, from_seq, batch_end, version, version,
                 latency_ms, OUTCOME_FAILED, result.reason)
        return {"outcome": OUTCOME_FAILED, "agent_run_id": run_id,
                "reason": result.reason, "from_segment_seq": from_seq,
                "to_segment_seq": batch_end, "pending_until": pending[-1].segment_seq,
                "processed_until": cursor, "previous_state_version": version,
                "new_state_version": version, "batches": 0}

    merged = merge_content(previous, result.parsed)
    with meeting_lock(meeting_id):
        fresh = state_repository.get(db, meeting_id)
        fresh_cursor = fresh.last_processed_segment_seq if fresh is not None else 0
        fresh_version = fresh.version if fresh is not None else 0
        if fresh_cursor != cursor:
            _log_run(meeting_id, run_id, from_seq, batch_end, version, fresh_version,
                     latency_ms, OUTCOME_SUPERSEDED, None)
            return {"outcome": OUTCOME_SUPERSEDED, "agent_run_id": run_id,
                    "from_segment_seq": from_seq, "to_segment_seq": batch_end,
                    "previous_state_version": version,
                    "new_state_version": fresh_version, "batches": 0}

        new_version = fresh_version + 1
        # Finish may freeze the transcript while this provider call is in
        # flight. Persisting its valid result is fine, but never reopen the
        # archived state as ACTIVE afterwards.
        status = STATUS_FROZEN if stream_service.is_frozen(db, meeting_id) else STATUS_ACTIVE
        previous_status = fresh.status if fresh is not None else None
        snapshot_repository.add_snapshot(
            db,
            meeting_id,
            from_segment_seq=batch[0].segment_seq,
            to_segment_seq=batch_end,
            window_start_ms=batch[0].start_ms,
            window_end_ms=batch[-1].end_ms,
            content=merged,
            model_name=getattr(provider, "name", None),
        )
        state_repository.upsert(
            db, meeting_id,
            content=merged,
            version=new_version,
            last_processed_segment_seq=batch_end,
            status=status,
            model_name=getattr(provider, "name", None),
            last_error=None,
        )

    _publish_state_changed(meeting_id, version=new_version, status=status,
                           previous_version=fresh_version,
                           previous_status=previous_status)
    _log_run(meeting_id, run_id, from_seq, batch_end, version, new_version,
             latency_ms, OUTCOME_OK, None)
    remaining = [s.segment_seq for s in pending[len(batch):]]
    return {
        "outcome": OUTCOME_LIMIT_REACHED if remaining else OUTCOME_OK,
        "agent_run_id": run_id,
        "from_segment_seq": from_seq,
        "to_segment_seq": batch_end,
        "pending_until": pending[-1].segment_seq,
        "processed_until": batch_end,
        "previous_state_version": version,
        "new_state_version": new_version,
        "batches": 1,
        "remaining_segment_seqs": remaining,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _coalesced_window(
    segments: list[TranscriptSegment], *, max_chars: int
) -> list[TranscriptSegment]:
    """Largest contiguous pending prefix that fits one realtime LLM call."""
    selected: list[TranscriptSegment] = []
    chars = 0
    for segment in segments:
        line_chars = len(segment.text or "")
        if selected and chars + line_chars > max_chars:
            break
        selected.append(segment)
        chars += line_chars
    return selected


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


def freeze_state(db: Session, meeting_id: str) -> None:
    """Archive the last-good realtime view without invoking the LLM.

    Transcript freeze and Final Summary are independent of realtime coverage.
    This only communicates that no more live refreshes will be scheduled; the
    cursor still truthfully exposes how much transcript the view covered.
    """
    with meeting_lock(meeting_id):
        row = state_repository.get(db, meeting_id)
        if row is None or row.status in {STATUS_EMPTY, STATUS_FAILED, STATUS_FROZEN}:
            return
        previous_status = row.status
        previous_version = row.version
        content = state_repository.get_content(db, meeting_id)
        state_repository.upsert(
            db,
            meeting_id,
            content=content,
            version=row.version,
            last_processed_segment_seq=row.last_processed_segment_seq,
            status=STATUS_FROZEN,
            model_name=row.model_name,
            last_error=row.last_error,
            record_health_success=False,
        )
    _publish_state_changed(
        meeting_id,
        version=previous_version,
        status=STATUS_FROZEN,
        previous_version=previous_version,
        previous_status=previous_status,
    )


def get_public_state(db: Session, meeting_id: str) -> MeetingStatePublic:
    """Product-facing projection of the state (Step UI-INTEL-1).

    Differs from `get_state` in exactly one behaviour: a CORRUPT `state_json`
    degrades to `status = FAILED` with EMPTY content instead of propagating
    `MeetingStateCorruptError` to the caller.

    Why degrade rather than re-raise: `get_state` is the internal/debug view, and
    loud is right there — a state we cannot decode is our own data-integrity
    problem and a human should look at the row. But this projection is consumed
    by the product UI, where an unhandled exception becomes a 500 that reads as
    "the app is broken". Worse, the frontend would then have no idea whether the
    AI is working, and an `ACTIVE + empty content` fallback would actively LIE:
    it would tell the user the AI is running fine while showing nothing.

    So the corruption is reported HONESTLY as a degraded status — the panel says
    "AI 整理暂时不可用 / 会议字幕仍在正常记录" and the transcript is untouched.
    The scalars we can still read (`version`, cursor, `updated_at`) are kept, and
    NOTHING is written back to the DB: this is a read-path projection only, so a
    bad row is never laundered away by a GET.
    """
    from app.common.exceptions import MeetingNotFoundError

    if db.get(Meeting, meeting_id) is None:
        raise MeetingNotFoundError(meeting_id)

    row = state_repository.get(db, meeting_id)
    if row is None:
        return MeetingStatePublic(meeting_id=meeting_id, version=0,
                                  last_processed_segment_seq=0, status=STATUS_EMPTY)

    updated_at = row.updated_at.isoformat() if row.updated_at else None
    try:
        content = state_repository.get_content(db, meeting_id)
    except state_repository.MeetingStateCorruptError:
        logger.error(
            "meeting state unreadable; public API degrading to FAILED",
            extra={"meeting_id": meeting_id},
        )
        return MeetingStatePublic(
            meeting_id=meeting_id,
            version=row.version,
            last_processed_segment_seq=row.last_processed_segment_seq,
            status=STATUS_FAILED,
            updated_at=updated_at,
        )

    return MeetingStatePublic(
        meeting_id=meeting_id,
        version=row.version,
        last_processed_segment_seq=row.last_processed_segment_seq,
        status=row.status,
        updated_at=updated_at,
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

    from app.common.meeting_agent_lock import meeting_agent_run_lock

    with meeting_agent_run_lock(meeting_id):
        db = SessionLocal()
        try:
            process_pending(db, meeting_id, provider=provider, force_flush=force_flush)
        except Exception:  # pragma: no cover - defensive; transcript is durable
            logger.exception(
                "meeting state agent failed for meeting %s (transcript is already "
                "durable; state update is retryable)",
                meeting_id,
            )
        finally:
            db.close()


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_EMPTY",
    "STATUS_STALE",
    "STATUS_FAILED",
    "STATUS_FROZEN",
    "merge_content",
    "process_pending",
    "select_batches",
    "get_state",
    "get_public_state",
    "freeze_state",
    "run_agent_background",
]
