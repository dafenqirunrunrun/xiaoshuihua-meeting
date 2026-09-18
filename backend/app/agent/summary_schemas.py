"""Summary schemas. PRD §43 / §44."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MeetingSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    overview: str | None
    key_topics: list[str] = []
    conclusions: list[str] = []
    open_questions: list[str] = []
    status: str = "NOT_STARTED"
    raw_model_output: str | None = None


class MeetingSummarySchema(BaseModel):
    """Structured LLM output — success criterion for a generation attempt.

    PRD §43: the provider response must conform to this shape (overview +
    three string lists). Pydantic validation IS the parse gate: on failure the
    attempt falls back to a bounded repair prompt (summary_max_attempts).
    """

    overview: str = Field(..., min_length=1)
    key_topics: list[str] = []
    conclusions: list[str] = []
    open_questions: list[str] = []


class SummaryOut(BaseModel):
    """Full Summary snapshot returned by GET /summary (Step 7B §15).

    The durable recovery fact-source for the frontend: after an SSE summary.*
    event (or none), a client that (re)connects reads THIS to see the latest
    version's state. Includes version (append-only regeneration) and the safe
    raw_model_output evidence (LLM JSON or a short human FAILED reason) — never
    provider exception stacks / API keys / internal headers.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_id: str
    version: int
    status: str
    overview: str | None = None
    key_topics: list[str] = []
    conclusions: list[str] = []
    open_questions: list[str] = []
    model_name: str | None = None
    raw_model_output: str | None = None
    created_at: datetime | None = None


class RegenerationAccepted(BaseModel):
    """202 response for POST /summary/regenerate (Step 7B §22).

    The HTTP call does NOT block on DeepSeek: it creates the next version's
    GENERATING row synchronously (so the client learns the version) and returns
    immediately; the LLM completes in the background via BackgroundTasks.
    """

    meeting_id: str
    version: int
    status: str = "GENERATING"
