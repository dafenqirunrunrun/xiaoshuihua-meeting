"""SSE event envelope (Step 5). PRD §30 event list + Part 3 of the Step 5 spec.

Contract frame (per SSE spec):

```
id: final:meeting_x:3
event: transcript.final
data: {event_type, meeting_id, emitted_at, payload}
<blank line>
```

Event types produced in this step:
  - transcript.partial   (live only, no replay)
  - transcript.final     (persisted; replayed via catch-up)
  - meeting.status       (live only, no replay)
  - heartbeat            (liveness signal, no `id:`)

Reserved for later steps (never produced here; NOT in the Literal so a stray
publish is a typing error): summary.started / summary.completed / summary.failed.

NOTE: PRD §30 does NOT list transcript.stream_end among SSE events, so the
stream-end signal is NOT broadcast (Step 5 Part 19). It stays an internal
Finalizer control signal.
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
    "heartbeat",
]

# Stable SSE `id:` prefix for a persited FINAL (Part 13):
#   final:<meeting_id>:<segment_seq>
FINAL_ID_PREFIX = "final:"

# Stable SSE `id:` prefix for a summary lifecycle event (Step 7B §14):
#   summary:<meeting_id>:<version>:started|completed|failed
SUMMARY_ID_PREFIX = "summary:"


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
