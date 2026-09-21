"""Final Summary V2 schemas (Step AGENT-2).

The Final Summary is generated from the **Meeting State** (AGENT-1 working
memory) plus transcript grounding. Normal meetings include the complete FINAL
transcript so an incremental-state omission can be recovered; oversized
meetings fall back to a bounded, priority-selected evidence set. These are the
models for that output.

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
The Final Summary Agent is instructed to carry the state's evidence through.
Nothing here REPAIRS a citation: a seq the model reported is persisted exactly
as reported, including one that does not exist in the transcript. Whether a
citation resolves is decided at READ time, by the UI, against the transcript it
already has (Step UI-INTEL-2) — so no write-path sanitizing step exists or is
needed. See `SummaryEvidence` for the read-side projection.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Bounds mirror the Meeting State item bounds so a summary can always express
# whatever the state expressed, without letting a runaway model emit an
# unbounded document.
_MAX_TEXT = 2000
_MAX_TITLE = 200
_MAX_NAME = 200


def prune_code_fence(text: str) -> str:
    """Strip a ```json ... ``` wrapper if the model added one (lenient).

    Shared by the WRITE path (`final_summary_service.parse_v2`, which decides
    whether a generation succeeds) and the READ path
    (`project_summary_evidence`, which decides whether the stored result is a
    verifiable V2 summary). Both must unwrap identically: if the reader were
    stricter than the writer, a fenced response would be accepted and persisted
    and then silently report `summary_evidence = null` forever — the feature
    would work in tests and not in production.
    """
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text


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


# =============================================================================
# Evidence projection (Step UI-INTEL-2B) — READ path only
# =============================================================================
# The items above are what the MODEL produced; the models below are what the
# PUBLIC Summary API hands to the UI so it can offer 「查看原文」. They are a
# separate set rather than a reuse of the item models because they are a
# different contract with a different audience: these are frozen presentation
# shapes, and widening an internal model later must not silently widen what the
# product API exposes.
#
# The projection carries `source_segment_seqs` VERBATIM — no dedupe, no sort,
# no filtering of impossible seqs. Normalizing (valid integers, ascending,
# deduped, bounded) is the frontend's job and lives in exactly one place
# (`frontend/src/utils/evidence.ts`). Doing it again here would create a second
# implementation to keep in sync, and the seqs must stay the model's own report
# so a dangling citation is still visible as one.

class SummaryEvidenceDecision(BaseModel):
    """A decision + the lines it cites."""

    text: str
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryEvidenceTopic(BaseModel):
    """A key topic + its concise summary and cited lines."""

    title: str
    summary: str
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryEvidenceActionItem(BaseModel):
    """An action item + the lines it cites.

    `owner` / `deadline` stay nullable with no default-substitution: the UI's
    「待确认」 fallback is a presentation choice, and inventing a value here would
    make a genuinely-unknown owner indistinguishable from a model-guessed one.
    """

    task: str
    owner: str | None = None
    deadline: str | None = None
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryEvidenceRisk(BaseModel):
    """A risk + the lines it cites."""

    text: str
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryEvidenceOpenQuestion(BaseModel):
    """An open question + the lines it cites."""

    text: str
    source_segment_seqs: list[int] = Field(default_factory=list)


class SummaryEvidence(BaseModel):
    """Structured evidence for every load-bearing item of a V2 summary.

    Present ONLY when the persisted summary is a verifiable
    `FinalMeetingSummary`. Every other case — a legacy summary, a
    transcript-fallback generation, a FAILED row, an empty meeting, or a
    `raw_model_output` we cannot parse as V2 — yields `summary_evidence = null`
    on the public response, and the UI then renders no 「查看原文」 entry at all.

    **Partial output is deliberate, NOT a bug.** `overview` and
    `closing_summary` are prose and cite nothing, and the model may legally
    return empty lists (an empty meeting expresses itself exactly that way).
    Requiring at least one item would turn "this summary has no decisions" into
    "this summary failed to parse", which is a lie about the data. So an
    all-empty summary is still a VALID V2 result and yields a SummaryEvidence
    with four empty lists — distinguishable at the frontend because there are
    simply no items to render.

    What this object never contains: `raw_model_output`, `model_name`, prompts,
    provider errors, or any internal identifier. It is content + citations only.
    """

    topics: list[SummaryEvidenceTopic] = Field(default_factory=list)
    decisions: list[SummaryEvidenceDecision] = Field(default_factory=list)
    action_items: list[SummaryEvidenceActionItem] = Field(default_factory=list)
    risks: list[SummaryEvidenceRisk] = Field(default_factory=list)
    open_questions: list[SummaryEvidenceOpenQuestion] = Field(default_factory=list)


def project_summary_evidence(raw_model_output: str | None) -> SummaryEvidence | None:
    """Parse a persisted `raw_model_output` into public evidence, or None.

    Returns None — never raising — for anything that is not a valid V2
    `FinalMeetingSummary`:

      * `None` / empty (no output recorded)
      * a legacy `MeetingSummarySchema` JSON (overview + `key_topics` +
        `conclusions` + `open_questions`)
      * the empty-meeting precomputed summary, which IS such a legacy JSON
      * a `"[summary failed] ..."` reason string
      * malformed / truncated JSON

    The V2/legacy disambiguation is NOT done by guessing from keys. It is exact,
    and it rests on one property: `FinalMeetingSummary` sets `extra="forbid"`.
    Every legacy payload carries `conclusions` — a field V2 does not declare — so
    a legacy payload FAILS validation rather than validating "successfully" with
    `decisions=[]` / `action_items=[]`. Without that config the two shapes would
    be indistinguishable (they share `overview` / `key_topics` /
    `open_questions`), and every legacy summary would silently advertise empty
    structured evidence — an entry set that renders nothing but claims the
    summary was understood.

    So the known consequence is that a summary with NO load-bearing items at all
    (the empty meeting) returns None, not an all-empty `SummaryEvidence`. That is
    the honest answer: there is nothing to trace to. An all-empty
    `SummaryEvidence` IS still reachable — a V2 meeting that genuinely decided
    nothing — and the two cases are told apart by the payload, not by the
    response being null.

    This reads the column; it never writes it, and a failure here cannot turn a
    readable summary into a 500.
    """
    if not raw_model_output:
        return None
    try:
        data = json.loads(prune_code_fence(raw_model_output).strip())
        if not isinstance(data, dict):
            return None
        parsed = FinalMeetingSummary.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError):
        return None

    return SummaryEvidence(
        topics=[
            SummaryEvidenceTopic(
                title=t.title,
                summary=t.summary,
                source_segment_seqs=t.source_segment_seqs,
            )
            for t in parsed.key_topics
        ],
        decisions=[
            SummaryEvidenceDecision(text=d.text, source_segment_seqs=d.source_segment_seqs)
            for d in parsed.decisions
        ],
        action_items=[
            SummaryEvidenceActionItem(
                task=a.task,
                owner=a.owner,
                deadline=a.deadline,
                source_segment_seqs=a.source_segment_seqs,
            )
            for a in parsed.action_items
        ],
        risks=[
            SummaryEvidenceRisk(text=r.text, source_segment_seqs=r.source_segment_seqs)
            for r in parsed.risks
        ],
        open_questions=[
            SummaryEvidenceOpenQuestion(text=q.text, source_segment_seqs=q.source_segment_seqs)
            for q in parsed.open_questions
        ],
    )


__all__ = [
    "FinalMeetingSummary",
    "SummaryActionItem",
    "SummaryDecision",
    "SummaryEvidence",
    "SummaryEvidenceActionItem",
    "SummaryEvidenceDecision",
    "SummaryEvidenceOpenQuestion",
    "SummaryEvidenceRisk",
    "SummaryEvidenceTopic",
    "SummaryOpenQuestion",
    "SummaryRisk",
    "SummaryTopic",
    "project_summary_evidence",
    "prune_code_fence",
]
