"""Finalizer service — coordinates Summary terminal -> Meeting COMPLETED (Step 7B).

Responsibilities (Step 7B §2-§7):
  - Trigger the INITIAL summary generation once (only on the first accepted
    stream-end; idempotent retries must never re-run the LLM, §5).
  - Observe Summary reaching a terminal state (COMPLETED / FAILED) and only
    then transition Meeting FINALIZING -> COMPLETED (§9).
  - Explain crash-gap recovery: `summary terminal + meeting FINALIZING` is a
    legal, RECOVERABLE state (Step 7A Phase C commits the summary first). A
    Finalizer retry sees a terminal summary, does NOT re-call the LLM, and
    simply completes the meeting (§6/§7).
  - NEVER creates Meeting FAILED / FINALIZING_FAILED (D1/§2).

This service deliberately does NOT re-implement the Summary pipeline. It calls
the Step 7A `summary_service.generate()` / `summary_service.regenerate()` —
prompt, retry, schema validation, DeepSeek call, persistence all live there
(§3).

Background trigger (process-local, §4/§22): FastAPI BackgroundTasks calls
`run_finalizer_background(meeting_id)`, which OWNS its own DB session — the
request-scoped session is never used after the response returns (§23).
BackgroundTasks is a thin MVP trigger, NOT a durable job queue: the durable
facts remain in the DB (transcript_stream, meeting_summary, meeting.status),
recoverable by re-running the Finalizer (no startup full-database scan, §7).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.agent.summary_model import MeetingSummary
from app.meeting.meeting_model import Meeting
from app.realtime import sse_service
from app.meeting.service import complete_after_summary
from app.agent.summary_service import STATUS_COMPLETED, STATUS_FAILED, generate as generate_initial

logger = logging.getLogger(__name__)


def finalize_for_meeting(db: Session, meeting_id: str, *, provider=None) -> bool:
    """Idempotent, safe-to-repeat termination step for one meeting.

    Returns:
      True  — the meeting is (or was already) COMPLETED after this call.
      False — the meeting does not exist (nothing to do; not an error).

    Safe under any re-entry because:
      - meeting already COMPLETED        -> early return (skip LLM + transition)
      - summary v1 already terminal      -> generate() idempotent no-op
                                            (returns the row, ZERO LLM calls)
      - complete_after_summary()         -> FINALIZING -> COMPLETED idempotent

    Flow (meeting stays FINALIZING until summary terminal, §9):
      initial summary generation (if not terminal yet)
      -> summary COMPLETED | FAILED (durable)
      -> summary.completed|failed SSE (B19/B20: AFTER terminal durable)
      -> meeting FINALIZING -> COMPLETED (durable) + meeting.status SSE
    SSE order (£12/B21): summary.started … then summary.completed|failed, then
    meeting.status(COMPLETED).
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        return False

    if meeting.status == "COMPLETED":
        # already done — even if a retry arrives mid-work, never re-run LLM.
        return True

    # Step 0 (Step AGENT-2 §2): make the Meeting State CURRENT before the
    # summary reads it.
    #
    #     finish -> flush remaining FINAL -> state current -> summary
    #
    # `POST /finish` already schedules a forced flush, but it runs on ITS OWN
    # background task and may have failed, raced, or never run (e.g. a trailing
    # FINAL arrived after finish). The summary must never be generated from a
    # state that has not absorbed the end of the meeting, so we flush HERE,
    # synchronously, immediately before generating.
    #
    # This is best-effort by construction: `process_pending` records its own
    # failures and never raises for business reasons. If the flush does not
    # close the gap, `final_summary_service.assess_usability` sees a stale state
    # and the summary falls back to the full transcript — degraded, but never
    # silently under-reported. An agent failure must not block completion (§18).
    _flush_meeting_state(db, meeting_id)

    # Step 1: ensure the initial summary is terminal (generate is idempotent —
    # a terminal v1 returns immediately → crash-gap retry runs ZERO LLM calls).
    # Generate emits summary.started itself (after the GENERATING commit).
    item = generate_initial(db, meeting_id, provider=provider)

    # Step 2: terminal SSE — only after the terminal summary is durable
    # (B19/B20). Meeting is still FINALIZING at this point; §11's "durable
    # Meeting COMPLETED" preference is best-effort while §12's relative SSE
    # order (terminal before meeting.status) is the testable invariant (B21).
    # A publish failure (evicted subscriber etc.) must NEVER abort the durable
    # completion — commit-before-publish, SSE is not a fact source (B25).
    version = item.version
    try:
        if item.status == STATUS_COMPLETED:
            sse_service.publish_summary_completed(meeting_id, version=version)
        elif item.status == STATUS_FAILED:
            sse_service.publish_summary_failed(meeting_id, version=version)
    except Exception:  # pragma: no cover - defensive; never blocks the Finalizer
        logger.exception(
            "summary.%s SSE publish failed for meeting %s (SSE is not a fact source; "
            "the durable summary + meeting COMPLETED already committed)",
            item.status.lower(), meeting_id,
        )

    # Step 3: only once the summary is terminal do we complete the meeting.
    # complete_after_summary emits meeting.status(COMPLETED) after its commit.
    complete_after_summary(db, meeting_id)
    return True


def _flush_meeting_state(db: Session, meeting_id: str) -> None:
    """Force the Meeting State to absorb the end of the meeting (Step AGENT-2 §2).

    The §2 ordering rule is::

        finish -> flush remaining FINAL -> state current -> summary

    This closes the last hop. `POST /finish` already asks AGENT-1 for a forced
    flush, but that runs in ITS OWN background task: it can fail, race with a
    trailing FINAL, or simply never have been scheduled (crash-gap recovery).
    The Finalizer therefore flushes again, synchronously, immediately before the
    summary is generated. `process_pending(force_flush=True)` treats any
    remaining tail as one final batch regardless of the batching thresholds.

    Best-effort by construction: `process_pending` never raises for business
    reasons — it records the failure on the state row and reports it. That is
    exactly what we want here. If the flush cannot close the gap,
    `final_summary_service.assess_usability` will observe a stale cursor and the
    summary will fall back to the full transcript: DEGRADED, never silently
    under-reported (§2/§9). An agent failure must never block completion (§18).

    Runs on the caller's session so the flush and the summary that follows see a
    consistent view; the flush commits its own transaction.

    Deliberately does NOT thread the summary's provider through: the flush is
    AGENT-1's own LLM call and resolves its provider the way it always does
    (the factory). In production both resolve to the same provider object; in
    tests this keeps the state-agent queue and the summary queue independent, so
    scripting one cannot silently consume the other's responses.
    """
    from app.meeting_state import service as meeting_state_service

    try:
        report = meeting_state_service.process_pending(db, meeting_id, force_flush=True)
    except Exception:  # pragma: no cover - defensive; process_pending absorbs
        logger.exception(
            "meeting state flush failed for %s before summary; the summary will "
            "fall back to the full transcript if the state is stale",
            meeting_id,
        )
        return

    outcome = report.get("outcome")
    if outcome in (
        meeting_state_service.OUTCOME_OK,
        meeting_state_service.OUTCOME_NO_WORK,
        # The agent is switched off: not a flush failure, just no rich path. The
        # summary falls back to the transcript by design (§9).
        meeting_state_service.OUTCOME_DISABLED,
    ):
        return
    # SUPERSEDED / BATCH_LIMIT_REACHED / BATCH_TOO_LARGE / FAILED / NOT_FOUND:
    # the state may lag the transcript or be unusable. Not fatal — the caller's
    # `assess_usability` decides, and a genuinely stale state degrades to the
    # transcript path rather than being summarized as if complete.
    logger.warning(
        "meeting state flush for %s did not converge before summary "
        "(outcome=%s, reason=%s); summary may fall back to transcript",
        meeting_id, outcome, report.get("reason"),
    )


def run_finalizer_background(meeting_id: str) -> None:
    """BackgroundTasks entry point — owns its own DB session (§23).

    Never uses the HTTP request's session. Opens a fresh SessionLocal, runs the
    idempotent finalize_for_meeting, and ALWAYS closes it. Any failure is
    logged (the durable facts remain recoverable by re-running the Finalizer).
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        finalize_for_meeting(db, meeting_id)
    except Exception:  # pragma: no cover - defensive
        logger.exception("finalizer failed for meeting %s (durable facts remain; retry-safe)", meeting_id)
    finally:
        db.close()


def run_regenerate_background(meeting_id: str) -> None:
    """BackgroundTasks entry for a POST /summary/regenerate (Step 7B §22/§23).

    The HTTP route already created a fresh vN+1 GENERATING row and returned
    202. Here we run the Phase B/C pipeline (LLM + validation + terminal
    persistence) on OUR OWN DB session (never the request's). The meeting is
    already COMPLETED for a normal regenerate, so there is NO Finalizer
    meeting-step — only the summary becomes terminal.

    Terminal SSE: like the Finalizer path, we publish `summary.completed` /
    `summary.failed` AFTER the terminal row is durable (commit-before-publish).
    `complete_generation` only emits `summary.started`, so without this the
    Result page would never be notified that a regenerated version finished.
    Publish failure must NEVER abort the durable result (SSE is not a fact
    source) — the client recovers the truth via GET /summary on reconnect.
    """
    from app.db import SessionLocal
    from app.agent import summary_repository
    from app.agent.summary_service import complete_generation
    from app.realtime import sse_service

    db = SessionLocal()
    try:
        latest = summary_repository.max_version(db, meeting_id) or 0
        item = summary_repository.get_for_meeting_version(db, meeting_id, latest)
        if item is None or item.status != "GENERATING":
            # nothing in flight (or an idempotent retry already finished)
            return
        result = complete_generation(db, meeting_id, item.version)

        # Terminal SSE — only after the terminal summary is durable (Step 7B
        # §11/B18: DB commit -> SSE publish). Same ordering contract as the
        # Finalizer: summary.started ... then summary.completed|failed.
        try:
            if result.status == "COMPLETED":
                sse_service.publish_summary_completed(meeting_id, version=result.version)
            elif result.status == "FAILED":
                sse_service.publish_summary_failed(meeting_id, version=result.version)
        except Exception:  # pragma: no cover - defensive; never blocks durability
            logger.exception(
                "summary.%s SSE publish failed for regenerated meeting %s "
                "(SSE is not a fact source; the durable summary already committed)",
                result.status.lower(), meeting_id,
            )
    except Exception:  # pragma: no cover - defensive
        logger.exception("regenerate background failed for meeting %s (retry-safe)", meeting_id)
    finally:
        db.close()


__all__ = ["finalize_for_meeting", "run_finalizer_background", "run_regenerate_background"]




