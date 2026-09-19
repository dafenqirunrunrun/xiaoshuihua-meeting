"""Final Summary V2 schemas (Step AGENT-2).

The Final Summary is generated from the **Meeting State** (AGENT-1 working
memory) plus a bounded set of **transcript evidence** — NOT from the whole
transcript re-read from scratch. These are the models for that output.

Two contracts live here, and the split is deliberate:

  `FinalMeetingSummary`   what the Final Summary Agent produces: the full
                          structured result, WITH evidence trace.
  `MeetingSummarySchema`  the PRE-EXISTING API/persistence shape (overview +
                          three string lists), which the frontend already
                          consumes. It is NOT modified here — the compatibility
                          mapper (`final_summary_service.to_legacy_schema`)
                          projects V2 onto it.

Why keep both rather than just enriching the old model: the Result page, the
SSE contract and the durable `meeting_summary` columns all speak the old shape.
Widening it in place would ripple into the frontend and the DB schema, neither
of which this step is allowed to break (or, for the DB, to migrate at all).

Evidence trace (`source_segment_seqs`) is part of the contract, not decoration:
it is what lets the UI later answer "which lines is this conclusion based on?"
The Final Summary Agent is instructed to carry the state's evidence through, and
`final_summary_service` repairs obviously-broken citations (see `sanitize`).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Bounds mirror the Meeting State item bounds so a summary can always express
# whatever the state expressed, without letting a runaway model emit an
# unbounded document.
_MAX_TEXT = 2000
_MAX_TITLE = 200
_MAX_NAME = 200


class SummaryTopic(BaseModel):
    """One key topic in the final summary."""

    title: str = Field(..., min_length=1, max_length=_MAX_TITLE)
    summary: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryDecision(BaseModel):
    """A conclusion the meeting actually reached (not a proposal).

    The prompt forbids promoting a suggestion ("我觉得下周一有点赶") into a
    decision ("定下周三上线"); §7 of the step spec makes the Final Summary Agent
    the place where an over-confident state entry gets downgraded or dropped.
    """

    text: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryActionItem(BaseModel):
    """A concrete follow-up task.

    `owner` / `deadline` are nullable ON PURPOSE and default to null: an
    invented owner or date is worse than an explicit "unknown". `status` is
    carried for a future task-tracking step and is null in this step — nothing
    tracks execution yet (AGENT-3 territory, §22).
    """

    task: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    owner: str | None = Field(default=None, max_length=_MAX_NAME)
    deadline: str | None = Field(default=None, max_length=_MAX_NAME)
    status: str | None = Field(default=None, max_length=_MAX_NAME)
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryOpenQuestion(BaseModel):
    """Something raised but not settled."""

    text: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryRisk(BaseModel):
    """A potential problem / concern — NOT an already-happened failure."""

    text: str = Field(..., min_length=1, max_length=_MAX_TEXT)
    source_segment_seqs: list[int] = Field(default_factory=list)


class FinalMeetingSummary(BaseModel):
    """The structured Final Summary (V2) — the LLM's success criterion.

    Every list defaults to empty so a legitimately empty meeting ("no usable
    transcript") is expressible WITHOUT the model inventing content. Pydantic
    validation of this model IS the parse gate, exactly as
    `MeetingSummarySchema` is for the legacy path.
    """

    model_config = ConfigDict(extra="forbid")

    overview: str = Field(..., min_length=1, max_length=4000)
    key_topics: list[SummaryTopic] = Field(default_factory=list)
    decisions: list[SummaryDecision] = Field(default_factory=list)
    action_items: list[SummaryActionItem] = Field(default_factory=list)
    open_questions: list[SummaryOpenQuestion] = Field(default_factory=list)
    risks: list[SummaryRisk] = Field(default_factory=list)
    closing_summary: str = Field(default="", max_length=4000)


__all__ = [
    "FinalMeetingSummary",
    "SummaryActionItem",
    "SummaryDecision",
    "SummaryOpenQuestion",
    "SummaryRisk",
    "SummaryTopic",
]
