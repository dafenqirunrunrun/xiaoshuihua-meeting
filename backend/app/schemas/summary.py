"""Summary schemas. PRD §43 / §44."""

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
