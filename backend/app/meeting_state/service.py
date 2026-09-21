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
The batch is NOT flushed per segment (one LLM call per FINAL would be ruinous in
cost and latency). The process-local scheduler merges FINALs over a short quiet
window, capped by a maximum wait, then force-flushes that live window. Direct
callers can still use the size thresholds (`batch_max_segments` /
`batch_max_chars`), and the finalizer performs one last forced flush at Finish.

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
        ACTIVE|EMPTY -> FAILED     status moved       -> publish
        FAILED -> ACTIVE           status + version   -> publish
        ACTIVE -> FROZEN           status moved       -> publish
        FAILED -> FAILED           neither moved      -> SUPPRESSED

    That last row is why this is a predicate and not a bare call: a retry that
    fails again re-writes the same FAILED status, and waking the panel for a
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
                # Capture the PRIOR status BEFORE the write: `upsert` mutates the
                # identity-mapped row in place, so reading `row.status` afterwards
                # would return the new FROZEN value and make this look like a
                # no-op change (see `_current_state_identity`).
                previous_status = row.status
                previous_version = row.version
                state_repository.upsert(
                    db, meeting_id,
                    content=previous,
                    version=version,
                    last_processed_segment_seq=cursor,
                    status=STATUS_FROZEN,
                    model_name=row.model_name,
                )
            # ACTIVE -> FROZEN is a product-visible change (the panel switches to
            # its archived presentation). Version deliberately does NOT move: the
            # cursor did not advance, so there is nothing new to count.
            _publish_state_changed(meeting_id, version=version, status=STATUS_FROZEN,
                                   previous_version=previous_version,
                                   previous_status=previous_status)
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
            with meeting_lock(meeting_id):
                before_status, before_version = _current_state_identity(db, meeting_id)
                state_repository.mark_failed(db, meeting_id, reason=reason,
                                             model_name=getattr(provider, "name", None))
            # A failure is a product-visible change even though NOTHING else
            # moved: content and cursor are preserved and `version` does not
            # increment (it counts SUCCESSFUL updates). The panel must be able to
            # say "AI 整理暂时不可用" while still showing the last good content.
            _publish_state_changed(meeting_id, version=version, status=STATUS_FAILED,
                                   previous_version=before_version, previous_status=before_status)
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
                before_status, before_version = _current_state_identity(db, meeting_id)
                state_repository.mark_failed(
                    db, meeting_id, reason=result.reason or "unknown error",
                    model_name=getattr(provider, "name", None),
                )
            # Same non-version-bumping failure notice as the size-guard path
            # above, and for the same reason: the durable status changed.
            _publish_state_changed(meeting_id, version=version, status=STATUS_FAILED,
                                   previous_version=before_version, previous_status=before_status)
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
            # Captured as plain values before the write: `upsert` mutates the
            # identity-mapped row in place, so a later `fresh.status` read would
            # already show the new status (see `_current_state_identity`).
            previous_status = fresh.status if fresh is not None else None
            state_repository.upsert(
                db, meeting_id,
                content=merged,
                version=new_version,
                last_processed_segment_seq=batch_end,
                status=status,
                model_name=getattr(provider, "name", None),
                last_error=None,
            )

        # Announced AFTER the lock is released and the row is committed. This is
        # the successful-update case (version bumped), and it also covers the
        # recovery transition FAILED -> ACTIVE for free: `mark_failed` never
        # bumped the version, so the next successful upsert both advances it and
        # drops `last_error`, which is exactly what the panel needs to see.
        # Publishing inside the critical section would only lengthen it.
        _publish_state_changed(meeting_id, version=new_version, status=status,
                               previous_version=fresh_version,
                               previous_status=previous_status)

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
    "STATUS_FAILED",
    "STATUS_FROZEN",
    "merge_content",
    "process_pending",
    "select_batches",
    "get_state",
    "get_public_state",
    "run_agent_background",
]
