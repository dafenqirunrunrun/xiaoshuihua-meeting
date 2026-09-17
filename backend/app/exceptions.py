"""Domain exceptions.

Service / repository layers raise these domain exceptions.
The API layer maps them onto HTTP status codes, so business logic never
imports FastAPI. PRD §49: FINAL conflicts, invalid state, validation errors.

Mapping (main.py):
  MeetingNotFoundError          -> 404
  TranscriptValidationError     -> 400
  InvalidMeetingStateTransition -> 409
  TranscriptConflictError       -> 409
"""


class MeetingNotFoundError(Exception):
    """Meeting does not exist -> HTTP 404."""

    def __init__(self, meeting_id: str) -> None:
        self.meeting_id = meeting_id
        super().__init__(f"meeting not found: {meeting_id}")


class InvalidMeetingStateTransition(Exception):
    """A transition is not allowed by the meeting state machine / transcript gating -> HTTP 409."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class TranscriptValidationError(Exception):
    """Transcript event payload is invalid / pathway mismatch -> HTTP 400."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class TranscriptConflictError(Exception):
    """Idempotency conflict: an identical key with different content -> HTTP 409."""

    def __init__(self, meeting_id: str, event_id: str, /, *, message: str | None = None) -> None:
        self.meeting_id = meeting_id
        self.event_id = event_id
        super().__init__(
            message or f"transcript conflict meeting={meeting_id} event={event_id}"
        )
