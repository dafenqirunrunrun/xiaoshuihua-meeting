"""Summary schemas. PRD §43."""

from pydantic import BaseModel, ConfigDict


class MeetingSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    overview: str | None
    key_topics: list[str] = []
    conclusions: list[str] = []
    open_questions: list[str] = []
    status: str = "NOT_STARTED"
    raw_model_output: str | None = None
