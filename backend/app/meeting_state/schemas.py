"""Meeting State schemas (Step AGENT-1).

Meeting State is the **Meeting Working Memory**: a structured, continuously
updated picture of the meeting built from FINAL transcript only. It is NOT the
final summary — it is the structure the final summary should later consume
instead of re-reading the whole transcript every time.

Two distinct shapes live here, and the distinction matters:

  `MeetingState`        the DURABLE state: what we know + how far we have read
                        (`version`, `last_processed_segment_seq`). These two
                        fields are owned by US, never by the model.

  `MeetingStateContent` the LLM-WRITABLE part: topics / decisions / action
                        items / open questions / risks / current topic.

The LLM only ever produces `MeetingStateContent` (see `MeetingStateUpdate`).
Letting the model emit `version` or `last_processed_segment_seq` would let it
corrupt our idempotency cursor, so those fields are structurally unreachable
from the model's output schema. `MeetingState` composes the two.

Evidence trace: every decision / action item / open question / risk carries
`source_segment_seqs` — the persisted FINAL `segment_seq` values it was derived
from. That is what later lets the UI answer "why does the agent think this?"
and jump back to the original transcript line. It is part of the contract, not
a nice-to-have.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TopicState(BaseModel):
    """One discussed topic."""

    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(..., min_length=1, max_length=2000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class DecisionItem(BaseModel):
    """A DECISION the meeting actually reached.

    Deliberately distinct from an opinion / proposal under discussion: the
    prompt instructs the model not to promote discussion to a decision, and
    this schema keeps the two apart instead of flattening them into one list.
    """

    text: str = Field(..., min_length=1, max_length=2000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class ActionItem(BaseModel):
    """A concrete follow-up task.

    `owner` and `deadline` are nullable ON PURPOSE: the transcript frequently
    does not name an owner or a date, and inventing one is worse than saying
    "unknown". The contract is null, never a placeholder string.
    """

    task: str = Field(..., min_length=1, max_length=2000)
    owner: str | None = Field(default=None, max_length=200)
    deadline: str | None = Field(default=None, max_length=200)
    source_segment_seqs: list[int] = Field(default_factory=list)


class OpenQuestion(BaseModel):
    """Something raised but not settled."""

    text: str = Field(..., min_length=1, max_length=2000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class RiskItem(BaseModel):
    """A risk / concern / blocker called out during the meeting."""

    text: str = Field(..., min_length=1, max_length=2000)
    source_segment_seqs: list[int] = Field(default_factory=list)


class MeetingStateContent(BaseModel):
    """The LLM-writable substance of the meeting state.

    All lists default to empty, so a valid (if sparse) state is always
    expressible — the model is never forced to invent content to satisfy the
    schema. This is the object the updater asks the LLM for.
    """

    current_topic: str | None = Field(default=None, max_length=200)
    topics: list[TopicState] = Field(default_factory=list)
    decisions: list[DecisionItem] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)


class MeetingStateUpdate(BaseModel):
    """The exact JSON shape the LLM must return for one incremental update.

    Kept as a named model (rather than "just use MeetingStateContent") so the
    prompt's declared output contract and the parse gate are the SAME object;
    there is no second, hand-maintained description of the shape to drift.

    `version` / `last_processed_segment_seq` are deliberately ABSENT: only the
    pipeline may write those (see module docstring).
    """

    model_config = ConfigDict(extra="forbid")

    current_topic: str | None = None
    topics: list[TopicState] = Field(default_factory=list)
    decisions: list[DecisionItem] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)


class MeetingState(MeetingStateContent):
    """The durable, versioned meeting state (content + pipeline-owned cursor).

    Returned by GET /api/internal/meetings/{id}/state and persisted as
    `meeting_state.state_json` + the scalar columns around it.
    """

    model_config = ConfigDict(from_attributes=True)

    meeting_id: str
    version: int = 0
    last_processed_segment_seq: int = 0
    status: str = "EMPTY"
    model_name: str | None = None
    updated_at: str | None = None


__all__ = [
    "TopicState",
    "DecisionItem",
    "ActionItem",
    "OpenQuestion",
    "RiskItem",
    "MeetingStateContent",
    "MeetingStateUpdate",
    "MeetingState",
]
