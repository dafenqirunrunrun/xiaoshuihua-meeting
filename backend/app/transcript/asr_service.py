"""ASR ingress application layer — external ASR events into the Transcript Domain.

Sits between the HTTP router and the **existing, unchanged** Transcript Service::

    POST /api/internal/meetings/{id}/asr-events
      -> asr_schemas.AsrTranscriptEvent      (external camelCase DTO)
      -> asr_service.ingest_asr_event()      (THIS module)
           1. path/payload meetingId consistency      (reject mismatch)
           2. asr_adapter.to_internal_event()         (pure translation)
           3. FINAL only: allocate segment_seq UNDER THE MEETING LOCK
           4. transcript_service.handle_event()       (existing domain, unchanged)
      -> live cache / persistence / commit / SSE

Why the ordering allocation lives HERE and not in the adapter
------------------------------------------------------------

``segment_seq`` is the meeting-wide transcript ordering. The collaborator's ASR
does not supply it (ASR-0A §8), and inside one ASR session the only ordering the
producer knows is ``sentenceId`` — which is explicitly NOT the same concept.
Using ``segment_seq = sentenceId`` is forbidden: a reconnect starts a new ASR
session whose sentence numbering restarts, which would collide with the existing
``UNIQUE(meeting_id, segment_seq)`` and corrupt the transcript's global order.

So the Transcript Domain allocates it::

    next_seq = (max persisted segment_seq) + 1

The read-max -> allocate -> persist sequence MUST be one critical section,
otherwise two concurrent FINALs could observe the same max and both pick the
same seq. This module therefore holds ``meeting_lock(meeting_id)`` across the
whole operation. ``handle_event`` re-enters the same lock — it is an ``RLock``
keyed per meeting, and we are on the same thread, so the critical section is
held continuously and no interleaving is possible.

Idempotency (why a retry does not consume a new seq)
----------------------------------------------------

Before allocating, we look the derived ``event_id`` up. If the FINAL was already
persisted, we REUSE its stored ``segment_seq`` instead of allocating. The retry
then flows into the existing Case A idempotency path and returns 200 without
inserting. This is what keeps retries from burning sequence numbers and from
tripping the ``UNIQUE(meeting_id, segment_seq)`` constraint.

Concurrency scope (documented limitation, not implemented here)
---------------------------------------------------------------

``meeting_lock`` is a **process-local** ``RLock`` registry. It makes allocation
atomic within a single process — which is the entire MVP deployment. It is NOT a
cross-process / cross-instance guarantee. See ``docs/architecture.md``
"FUTURE: multi-worker sequence allocation".
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.exceptions import TranscriptValidationError
from app.common.meeting_lock import meeting_lock
from app.transcript import asr_adapter
from app.transcript import repository as transcript_repository
from app.transcript import schemas as transcript_schemas
from app.transcript import service as transcript_service
from app.transcript.asr_schemas import AsrTranscriptEvent


def _resolve_final_segment_seq(
    db: Session,
    meeting_id: str,
    internal: transcript_schemas.TranscriptEvent,
) -> int:
    """Allocate the meeting-wide ordering for a FINAL, or reuse it on a retry.

    MUST be called while holding ``meeting_lock(meeting_id)`` — the
    read-max -> return-next pair is the critical section that makes concurrent
    allocation safe.

    Reuse branch: the derived FINAL ``event_id`` is deterministic per ASR
    sentence, so an identical retry finds the persisted row and continues to
    that same sequence number. Without this, a retry would allocate a fresh
    tail seq, miss Case A, and collide on ``UNIQUE(meeting_id, segment_seq)``.
    """
    existing = transcript_repository.get_by_event_id(db, meeting_id, internal.event_id)
    if existing is not None:
        return existing.segment_seq

    max_seq = transcript_repository.max_seq(db, meeting_id)
    return (max_seq or 0) + 1


def ingest_asr_event(
    db: Session,
    path_meeting_id: str,
    external: AsrTranscriptEvent,
    *,
    background_tasks=None,
) -> transcript_schemas.TranscriptEvent:
    """Adapt + ingest one external ASR event. Returns the internal event used.

    Raises the same domain exceptions as the internal ingress, so the existing
    HTTP exception handlers map them unchanged (400 / 404 / 409).

    `background_tasks` is forwarded straight through to the Transcript Service,
    which uses it to schedule the meeting-state consumer for a newly committed
    FINAL. This layer stays a pure pass-through: it does not inspect, reorder or
    duplicate the scheduling decision, so the "one agent run per committed FINAL"
    rule is enforced in exactly one place.
    """
    # --- 1. path/payload meeting id consistency (ASR-0A §21) ----------------
    # Checked on the EXTERNAL value, before translation: the adapter copies
    # path_meeting_id into the internal event, so this is the only point where
    # a divergent payload meetingId is still visible.
    if external.meetingId != path_meeting_id:
        raise TranscriptValidationError(
            f"path meeting_id {path_meeting_id} != payload meetingId {external.meetingId}"
        )

    # --- 2. pure translation (no locking, no DB, no seq) --------------------
    internal = asr_adapter.to_internal_event(external, path_meeting_id=path_meeting_id)

    if internal.status == "PARTIAL":
        # PARTIAL never persists and is never ordered: no allocation, no
        # critical section needed beyond handle_event's own lock.
        transcript_service.handle_event(
            db, path_meeting_id, internal, background_tasks=background_tasks
        )
        return internal

    # --- 3+4. FINAL: allocate and persist inside ONE critical section -------
    with meeting_lock(path_meeting_id):
        seq = _resolve_final_segment_seq(db, path_meeting_id, internal)
        ordered = internal.model_copy(update={"segment_seq": seq})
        transcript_service.handle_event(
            db, path_meeting_id, ordered, background_tasks=background_tasks
        )
    return ordered


__all__ = ["ingest_asr_event"]
