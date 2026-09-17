"""Domain exceptions.

Service / repository layers raise these domain exceptions.
The API layer maps them onto HTTP status codes (404 / 409 / 400), so business
logic never imports FastAPI. PRD §49.2: FINAL conflicts, invalid transitions, etc.
"""


class MeetingNotFoundError(Exception):
    """Meeting does not exist -> HTTP 404."""

    def __init__(self, meeting_id: str) -> None:
        self.meeting_id = meeting_id
        super().__init__(f"meeting not found: {meeting_id}")


class InvalidMeetingStateTransition(Exception):
    """A transition is not allowed by the meeting state machine -> HTTP 409."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
