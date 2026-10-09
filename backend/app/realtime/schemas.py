"""SSE event envelope (Step 5). PRD §30 event list + Part 3 of the Step 5 spec.

Contract frame (per SSE spec):

```
id: final:meeting_x:3
event: transcript.final
data: {event_type, meeting_id, emitted_at, payload}
<blank line>
```

Event types produced in this step:
  - transcript.partial      (live only, no replay)
  - transcript.final        (persisted; replayed via catch-up)
  - meeting.status          (live only, no replay)
  - heartbeat               (liveness signal, no `id:`)
  - captain.completed       (live only, no replay; carries the referee report)
  - captain.failed          (live only, no replay)
  - captain.quick_assessment (live only, no replay; the fast first pass)

`captain.quick_assessment` (Task 6, two-stage referee)
-----------------------------------------------------
The two-stage referee produces a QUICK result in ~1-2s and a DEEP result
seconds later. Without an event for the first one the client would have to poll
to render it, which is the whole latency the two-stage design exists to remove.

It is a SEPARATE event rather than a field on `captain.completed` for one
reason: ordering. `captain.completed` means "the durable report is written", and
a client that received it has everything; a client that receives
`captain.quick_assessment` must keep waiting. Folding the Quick payload into
`captain.completed` would force every client to inspect a sub-field to learn
whether it should stop showing a spinner — the same "looks complete, is not"
trap the report's own `status` field avoids.

Both the Quick and the Deep substance travel in the payload, following the
DELIBERATE deviation `captain.completed` already documents (see
`sse_service.build_captain_completed`): the event carries content, `report_id` is
always present, and the durable GET remains the fact source because the event is
never replayed.

Reserved for later steps (never produced here; NOT in the Literal so a stray
publish is a typing error): summary.started / summary.completed / summary.failed.

NOTE: PRD §30 does NOT list transcript.stream_end among SSE events, so the
stream-end signal is NOT broadcast (Step 5 Part 19). It stays an internal
Finalizer control signal.

Step UI-INTEL-1 added `meeting.state.updated`: a pure INVALIDATION signal for
the Meeting Intelligence Panel. It carries only `{version, status}` — never the
state itself. The durable Meeting State is read over HTTP
(GET /api/meetings/{id}/state); this event just says "re-read it". Like
`summary.*` it is non-durable, never replayed, and does NOT extend the
`after_segment_seq` recovery protocol.
"""

from typing import Any, Literal

from pydantic import BaseModel

# Event types produced by this step.
SSE_EVENT_TYPES = Literal[
    "transcript.partial",
    "transcript.final",
    "meeting.status",
    "summary.started",
    "summary.completed",
    "summary.failed",
    "meeting.state.updated",
    "captain.completed",
    "captain.failed",
    "captain.quick_assessment",
    "heartbeat",
]

# Stable SSE `id:` prefix for a persited FINAL (Part 13):
#   final:<meeting_id>:<segment_seq>
FINAL_ID_PREFIX = "final:"

# Stable SSE `id:` prefix for a summary lifecycle event (Step 7B §14):
#   summary:<meeting_id>:<version>:started|completed|failed
SUMMARY_ID_PREFIX = "summary:"

# Stable SSE `id:` prefix for a Meeting State change (Step UI-INTEL-1):
#   state:<meeting_id>:<version>:active|stale|failed|frozen|empty
#
# NOTE the trailing status. Unlike a FINAL, a state change is NOT uniquely
# identified by (meeting, version): a failed attempt deliberately does NOT bump
# `version`, so ACTIVE@4 and STALE@4
# are two DIFFERENT durable states with the same version. Including the status
# keeps every distinct change distinguishable without inventing a new persisted
# event sequence — and this id is only ever a client-side dedup key, since the
# event itself is non-durable and never replayed.
STATE_ID_PREFIX = "state:"

# Stable SSE `id:` prefix for a referee lifecycle event (ADR-002 Phase 2-A):
#   captain:<meeting_id>:<report_id>:completed|failed
#
# UNLIKE `final:` / `summary:` this id is NOT derivable from (meeting, version):
# a referee report is keyed by its own `report_id` and the anchor segment that
# triggered it. The trailing status is included because the two terminal states
# share the same report_id — and, since Task 6, because a report now has TWO
# published moments (`quick_assessment` while ANALYZING, then `completed`), which
# share that report_id too. As with `state:`, this is only ever a client-side
# dedup key — the event is non-durable and never replayed.
CAPTAIN_ID_PREFIX = "captain:"


class SSEEventEnvelope(BaseModel):
    """One logical event delivered over a SSE connection.

    `sse_id` renders as the SSE `id:` field (absent when None). The SSE `data:`
    field always carries the full envelope ({event_type, meeting_id, emitted_at,
    payload}) so a client that ignores `event:`/`id:` still has self-contained
    JSON.
    """

    sse_id: str | None = None
    event_type: SSE_EVENT_TYPES
    meeting_id: str
    emitted_at: str  # ISO-8601 UTC
    payload: dict[str, Any]

    def to_sse_data(self) -> str:
        """The `data:` JSON body (excludes the SSE-only `sse_id`)."""
        import json

        return json.dumps(
            {
                "event_type": self.event_type,
                "meeting_id": self.meeting_id,
                "emitted_at": self.emitted_at,
                "payload": self.payload,
            },
            ensure_ascii=False,
        )
