"""SSE event building + gap-free subscribe + stream generator (Step 5).

Recovery model implemented here (PRD §32, arch §6, Step 5 Part 10/11/12):

    GET /transcript                 -> max_segment_seq = N
    GET /events?after_segment_seq=N
      connect()  [under meeting lock]
        1. read persisted FINAL with seq > N   (catch-up source)
        2. register the SSE subscriber          (in the same lock scope)
      event_stream()
        1. yield catch-up envelopes (segment_seq ASC)
        2. then drain the live bounded queue
    -> FINAL segments are delivered exactly once: either as catch-up (if they
       committed before our query) or as live (committed after subscribe).
       The shared per-meeting lock (same one FINAL ingest uses) makes "between
       query and subscribe" impossible -> no gap, no duplicate.

Non-goals / semantics:
  - PARTIAL is never replayed (Part 12); live only.
  - Stable SSE id for FINAL (Part 13): final:<meeting_id>:<segment_seq>.
  - Last-Event-ID header: NOT implemented (Part 14) — after_segment_seq is the
    single recovery cursor. Documented; not faked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.meeting.meeting_model import Meeting
from app.transcript.segment_model import TranscriptSegment
from app.config import settings
from app.db import SessionLocal
from app.common.exceptions import MeetingNotFoundError
from app.common.datetime_utils import utc_iso
from app.transcript import repository as transcript_repository
from app.realtime.schemas import SSEEventEnvelope
from app.realtime.event_broker import Subscriber, event_broker
from app.common.meeting_lock import meeting_lock


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


# --- Envelope builders ------------------------------------------------------

def build_partial(
    meeting_id: str,
    *,
    utterance_id: str,
    speaker_id: str,
    text: str,
    start_ms: int | None,
    end_ms: int | None,
) -> SSEEventEnvelope:
    return SSEEventEnvelope(
        sse_id=None,
        event_type="transcript.partial",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload={
            "utterance_id": utterance_id,
            "speaker_id": speaker_id,
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
        },
    )


def build_final(meeting_id: str, segment: TranscriptSegment) -> SSEEventEnvelope:
    """Stable, derivable SSE id for a persisted FINAL (Part 13)."""
    return SSEEventEnvelope(
        sse_id=f"final:{meeting_id}:{segment.segment_seq}",
        event_type="transcript.final",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload={
            "segment_seq": segment.segment_seq,
            "utterance_id": segment.utterance_id,
            "speaker_id": segment.speaker_id,
            "text": segment.text,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
        },
    )


def build_meeting_status(meeting: Meeting) -> SSEEventEnvelope:
    payload: dict = {"status": meeting.status}
    if meeting.started_at is not None:
        payload["started_at"] = utc_iso(meeting.started_at)
    if meeting.ended_at is not None:
        payload["ended_at"] = utc_iso(meeting.ended_at)
    return SSEEventEnvelope(
        sse_id=None,
        event_type="meeting.status",
        meeting_id=meeting.id,
        emitted_at=_utcnow_iso(),
        payload=payload,
    )


def _summary_event(
    meeting_id: str,
    *,
    event_type: str,
    version: int,
    status: str,
) -> SSEEventEnvelope:
    """Small, stable Summary lifecycle envelope (Step 7B §13/§14).

    payload is deliberately minimal: {version, status}. NO raw_model_output,
    NO provider error stack, NO API key, NO prompt — those never go to SSE.
    After a terminal event the client recovers the full snapshot via
    GET /summary. Stable `id:` allows client-side dedup.
    """
    return SSEEventEnvelope(
        sse_id=f"summary:{meeting_id}:{version}:{status.lower()}",
        event_type=event_type,  # type: ignore[arg-type]  # Literal widened
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload={"version": version, "status": status},
    )


def build_summary_started(meeting_id: str, *, version: int) -> SSEEventEnvelope:
    return _summary_event(meeting_id, event_type="summary.started", version=version,
                          status="GENERATING")


def build_summary_completed(meeting_id: str, *, version: int) -> SSEEventEnvelope:
    return _summary_event(meeting_id, event_type="summary.completed", version=version,
                          status="COMPLETED")


def build_summary_failed(meeting_id: str, *, version: int) -> SSEEventEnvelope:
    return _summary_event(meeting_id, event_type="summary.failed", version=version,
                          status="FAILED")


def build_heartbeat(meeting_id: str) -> SSEEventEnvelope:
    return SSEEventEnvelope(
        sse_id=None,
        event_type="heartbeat",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload={"timestamp": _utcnow_iso()},
    )


def build_captain_completed(meeting_id: str, *, report: dict) -> SSEEventEnvelope:
    """Referee report ready (ADR-002 Phase 2-A).

    DELIBERATE DEVIATION from the `meeting.state.updated` / `summary.*` house
    rule, which keeps SSE a PURE INVALIDATION signal and always re-reads content
    over HTTP. Here the payload DOES carry the report substance
    (`report_id`, `anchor_seq`, `positions`, `dispute_points`, `uncertainty`)
    so the panel can render the moment the referee finishes speaking.

    The deviation is safe only because `report_id` is in the payload: the event
    stays non-durable and is never replayed, so a client that was disconnected,
    refreshing or reconnecting misses it — and recovers by GET
    /api/meetings/{id}/captain/{report_id} (or /latest). The event is a fast
    path, never the fact source. Same contract as `transcript.final` rendering
    from the event while `/transcript` remains the recovery read.

    NEVER carries: raw model output, `model_name`, `last_error`, a prompt, a
    provider stack. `uncertainty` is the report's OWN field, not an error
    channel — its presence is a product requirement (the referee must state what
    it does not know), not a leak.
    """
    payload: dict = {
        "report_id": report.get("report_id"),
        "anchor_seq": report.get("anchor_segment_seq"),
        "positions": report.get("positions") or [],
        "dispute_points": report.get("dispute_points") or [],
        "uncertainty": report.get("uncertainty") or "",
        # Phase C.5. ADDITIVE and defaulted: added to an event that has already
        # shipped, so a client written against the previous payload keeps working
        # — `report.get` and the `or` fallback mean a report row written before
        # this field existed still produces a well-formed event.
        #
        # It rides here rather than on `captain.quick_assessment` because
        # research finishes between the two events: the Quick event is emitted
        # BEFORE the search starts, so it cannot carry the search's outcome. A
        # client that wants to say "外部资料检索未完成" can do so from this event
        # without string-matching the Chinese disclosure sentence in
        # `uncertainty`.
        "research_status": report.get("research_status") or "not_required",
        "external_evidence_count": len(report.get("external_evidence") or []),
    }
    return SSEEventEnvelope(
        sse_id=f"captain:{meeting_id}:{payload['report_id']}:completed",
        event_type="captain.completed",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload=payload,
    )


def build_captain_quick_assessment(
    meeting_id: str, *, report_id: str, quick: dict
) -> SSEEventEnvelope:
    """The fast first pass of the two-stage referee (Task 6).

    Deliberately NOT folded into `captain.completed`: that event means "the
    durable report is written", and a client that received it has everything it
    needs. A client that receives THIS one must keep waiting. If the Quick
    substance rode on `captain.completed` as a sub-field, every client would
    have to inspect that sub-field to decide whether to stop showing a spinner —
    the "looks complete, is not" confusion the report `status` field exists to
    prevent, reintroduced at the transport layer.

    Carries content (issue / key_evidence / provisional_judgement /
    question_to_resolve) under the same recorded deviation as
    `build_captain_completed`: `report_id` is always present, the event is
    non-durable and never replayed, and the durable GET remains the fact source.

    `provisional` is not decoration — it is the report's own semantics
    (`QuickAssessment.provisional_judgement`) surfaced so a client can label the
    first answer as provisional rather than presenting a 1-second opinion and a
    30-second analysis as equally final.

    NEVER carries: raw model output, `model_name`, a prompt, a provider stack.
    """
    payload: dict = {
        "report_id": report_id,
        "provisional": True,
        "issue": quick.get("issue") or "",
        "key_evidence": quick.get("key_evidence") or [],
        "provisional_judgement": quick.get("provisional_judgement") or "",
        "question_to_resolve": quick.get("question_to_resolve") or "",
    }
    return SSEEventEnvelope(
        sse_id=f"captain:{meeting_id}:{report_id}:quick_assessment",
        event_type="captain.quick_assessment",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload=payload,
    )


def build_captain_failed(
    meeting_id: str, *, report_id: str, anchor_seq: int | None, reason: str = ""
) -> SSEEventEnvelope:
    """Referee report failed (ADR-002 Phase 2-A).

    Carries NO report content — there is none, and there never will be for this
    report_id (a failure leaves `report_json` NULL by design). `reason` is a
    short product-safe code, NOT a provider stack trace.

    `anchor_seq` is included so the panel can attribute the failure to the
    trigger the user just made; the client is expected to clear its spinner and
    say the referee could not produce a report, rather than retrying silently.
    """
    payload: dict = {
        "report_id": report_id,
        "anchor_seq": anchor_seq,
        "reason": reason,
    }
    return SSEEventEnvelope(
        sse_id=f"captain:{meeting_id}:{report_id}:failed",
        event_type="captain.failed",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload=payload,
    )


def build_meeting_state_updated(
    meeting_id: str, *, version: int, status: str
) -> SSEEventEnvelope:
    """Meeting State change notice (Step UI-INTEL-1).

    PURE INVALIDATION, not a payload carrier. The client receives this and
    re-reads the durable state over HTTP (GET /api/meetings/{id}/state); the
    event itself is never the source of truth. Hence the payload stays minimal —
    `{version, status}` only, no topics / decisions / action items, and never
    `model_name`, `last_error`, raw model output, a prompt or a provider stack.

    It fires whenever the PRODUCT-VISIBLE durable state changes, which is
    deliberately NOT the same as "version incremented":

        ACTIVE vN -> ACTIVE vN+1   content updated
        ACTIVE -> STALE            transient update failed; last-good retained
        EMPTY -> FAILED            first update failed; no last-good exists
        STALE|FAILED -> ACTIVE     recovered
        ACTIVE -> FROZEN           meeting ended, state frozen

    `status` is in the payload precisely because of the non-version-bumping
    transitions: the client cannot distinguish ACTIVE@4 from STALE@4 by version
    alone. The `id:` therefore carries the status too (see STATE_ID_PREFIX).
    """
    return SSEEventEnvelope(
        sse_id=f"state:{meeting_id}:{version}:{status.lower()}",
        event_type="meeting.state.updated",
        meeting_id=meeting_id,
        emitted_at=_utcnow_iso(),
        payload={"version": version, "status": status},
    )


# --- Publish entry points (Producer side; MUST run AFTER commit) -----------

def publish_partial(
    meeting_id: str,
    *,
    utterance_id: str,
    speaker_id: str,
    text: str,
    start_ms: int | None,
    end_ms: int | None,
) -> None:
    event_broker.publish(
        meeting_id,
        build_partial(
            meeting_id,
            utterance_id=utterance_id,
            speaker_id=speaker_id,
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
        ),
    )


def publish_final(meeting_id: str, segment: TranscriptSegment) -> None:
    event_broker.publish(meeting_id, build_final(meeting_id, segment))


def publish_meeting_status(meeting: Meeting) -> None:
    event_broker.publish(meeting.id, build_meeting_status(meeting))


def publish_summary_started(meeting_id: str, *, version: int) -> None:
    """summary.started — MUST be published AFTER the GENERATING row committed
    (Step 7B §11: DB commit → SSE publish; SSE failure never rolls back DB)."""
    event_broker.publish(meeting_id, build_summary_started(meeting_id, version=version))


def publish_summary_completed(meeting_id: str, *, version: int) -> None:
    """summary.completed — AFTER terminal SUMMARY + Meeting COMPLETED are durable."""
    event_broker.publish(meeting_id, build_summary_completed(meeting_id, version=version))


def publish_summary_failed(meeting_id: str, *, version: int) -> None:
    """summary.failed — AFTER terminal FAILED summary is durable."""
    event_broker.publish(meeting_id, build_summary_failed(meeting_id, version=version))


def publish_heartbeat(meeting_id: str) -> None:
    event_broker.publish(meeting_id, build_heartbeat(meeting_id))


def publish_meeting_state_updated(
    meeting_id: str, *, version: int, status: str
) -> None:
    """meeting.state.updated — MUST be published AFTER the state row committed.

    Same commit-before-publish rule as every other event here: the durable
    Meeting State is already on disk, so a failed publish never rolls it back
    (SSE is not a fact source). Never replayed; the client re-reads via
    GET /api/meetings/{id}/state on connect / refresh / reconnect.
    """
    event_broker.publish(
        meeting_id, build_meeting_state_updated(meeting_id, version=version, status=status)
    )


def publish_captain_completed(meeting_id: str, *, report: dict) -> None:
    """captain.completed — MUST be published AFTER mark_completed committed.

    Commit-before-publish (ADR D4): the durable row is already on disk, so a
    failed publish (no subscribers, full queue) never rolls the report back.
    Callers must therefore treat publish as best-effort and NEVER let it change
    the analysis outcome.
    """
    event_broker.publish(meeting_id, build_captain_completed(meeting_id, report=report))


def publish_captain_quick_assessment(
    meeting_id: str, *, report_id: str, quick: dict
) -> None:
    """captain.quick_assessment — published while the report is STILL ANALYZING.

    NOT commit-before-publish, and that is correct rather than a violation: this
    event announces a result that is deliberately not durable yet. It must be
    published as soon as the Quick pass returns — waiting for the Deep commit
    would delay it by the Deep pass's own latency, which is exactly the wait the
    two-stage design exists to remove. The row stays ANALYZING, so a client that
    missed the event recovers by polling the report and nothing is lost.

    Best-effort, like every other sidecar publish: a subscriber-less or full
    broker must not be able to fail a report that is still being computed.
    """
    event_broker.publish(
        meeting_id,
        build_captain_quick_assessment(
            meeting_id, report_id=report_id, quick=quick
        ),
    )


def publish_captain_failed(
    meeting_id: str, *, report_id: str, anchor_seq: int | None, reason: str = ""
) -> None:
    """captain.failed — MUST be published AFTER mark_failed committed."""
    event_broker.publish(
        meeting_id,
        build_captain_failed(
            meeting_id, report_id=report_id, anchor_seq=anchor_seq, reason=reason
        ),
    )


# --- Gap-free connect (Part 10/11) ------------------------------------------

def connect(meeting_id: str, after_segment_seq: int) -> tuple[Subscriber, list[SSEEventEnvelope]]:
    """Atomically (w.r.t. FINAL ingest) register the subscriber + read catch-up.

    Runs entirely under meeting_lock(meeting_id) — the SAME lock FINAL ingest
    uses. Sequence inside the lock: read persisted FINAL seq > after_segment_seq
    (catch-up), then subscribe. Since no FINAL can commit in between (same lock),
    each FINAL is delivered once — as catch-up or as live — never lost.

    Raises MeetingNotFoundError(404) if the meeting does not exist (Part 15).
    """
    db = SessionLocal()
    try:
        with meeting_lock(meeting_id):
            meeting = db.get(Meeting, meeting_id)
            if meeting is None:
                raise MeetingNotFoundError(meeting_id)
            segments = transcript_repository.list_sorted(db, meeting_id)
            catchup = [
                build_final(meeting_id, seg)
                for seg in segments
                if seg.segment_seq > after_segment_seq
            ]
            # Subscribe AFTER the read, still under the lock (Part 11).
            sub = event_broker.subscribe(meeting_id)
            # Bind the subscriber to THIS event loop NOW (it is the same loop
            # the StreamingResponse generator will run on). Closing this window
            # means every publish() after connect() takes the thread-safe
            # call_soon_threadsafe path.
            sub.loop = asyncio.get_running_loop()
    finally:
        db.close()
    return sub, catchup


# --- Stream generator --------------------------------------------------------

def _render_frame(env: SSEEventEnvelope) -> str:
    """Render one SSE frame: optional id:, event:, data: (multiline-safe), blank line."""
    lines: list[str] = []
    if env.sse_id:
        lines.append(f"id: {env.sse_id}")
    lines.append(f"event: {env.event_type}")
    for line in (env.to_sse_data() or "").splitlines() or [""]:
        lines.append(f"data: {line}")
    lines.append("")  # blank line terminates the frame per SSE spec
    return "\n".join(lines) + "\n"


async def event_stream(
    sub: Subscriber,
    meeting_id: str,
    catchup: list[SSEEventEnvelope],
):
    """Async generator backing the StreamingResponse.

    Order guarantee (Part 12): all catch-up frames first (segment_seq ASC),
    only then live frames. Heartbeats (Part 9) are injected on idle. On ANY
    exit — including client disconnect — `finally` unsubscribes (Part 18), so
    refreshing 100x never leaks 100 dead subscriber slots.
    """
    sub.loop = asyncio.get_running_loop()
    try:
        for env in catchup:
            yield _render_frame(env)

        heartbeat_interval = settings.sse_heartbeat_interval
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                yield _render_frame(build_heartbeat(meeting_id))
                continue
            if item is None:  # disconnect/evict sentinel
                break
            yield _render_frame(item)
    finally:
        event_broker.unsubscribe(meeting_id, sub)


__all__ = [
    "connect",
    "event_stream",
    "publish_partial",
    "publish_final",
    "publish_meeting_status",
    "publish_summary_started",
    "publish_summary_completed",
    "publish_summary_failed",
    "publish_meeting_state_updated",
    "publish_captain_completed",
    "publish_captain_failed",
]



