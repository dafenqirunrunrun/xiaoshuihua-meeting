"""Final Summary V2 service — Meeting State as the primary summary input.

Step AGENT-2. Upgrades the summary input from

    full transcript  ->  LLM  ->  summary

to

    Meeting State (working memory) + selected transcript evidence  ->  LLM  ->  summary

with the legacy path kept intact as a FALLBACK.

Where this sits
---------------
`summary_service` still owns the Phase A/B/C pipeline, versioning, idempotency,
repair and persistence. This module only answers ONE question for it: **what
material should the summary be generated from, and can we use the meeting
state?** It returns a `SummaryPlan`; `summary_service` executes it. Nothing here
holds a lock, calls the LLM, or writes a row.

The core ordering rule (§2)
---------------------------
The summary must not be generated from a state that has not yet absorbed the
end of the meeting::

    finish -> flush remaining FINAL -> state current -> summary

This module ENFORCES the last hop: `assess_usability` compares the state's
`last_processed_segment_seq` against the final transcript sequence and refuses
the state when it is behind. It never silently summarizes a stale state. The
flush that closes the gap is performed by the caller (`finalizer`) before this
runs; a state that is STILL behind after that falls back to the legacy path
rather than being used as if it were complete.

Fallback (§9)
-------------
`MEETING_AGENT_ENABLED=false`, no state, a FAILED state, an EMPTY state, a
stale state, an unreadable state, or `FINAL_SUMMARY_V2_ENABLED=false` all select
the legacy full-transcript path. The chosen source is reported as
`summary_source = meeting_state | transcript_fallback | empty_meeting` on the
plan (logged, and asserted in tests). It is NOT persisted as its own column:
that would need an ALTER on `meeting_summary`, which this step is not permitted
to do — see the Known Limitations section of the step report.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agent import evidence as evidence_module
from app.agent.final_summary_prompts import (
    build_summary_input,
    build_v2_prompt,
    build_v2_repair_prompt,
    to_legacy_schema,
)
from app.agent.final_summary_schemas import FinalMeetingSummary, prune_code_fence
from app.agent.summary_schemas import MeetingSummarySchema
from app.config import settings
from app.meeting.meeting_model import Meeting
from app.meeting_state import repository as state_repository
from app.meeting_state.schemas import MeetingStateContent
from app.transcript import repository as transcript_repository

logger = logging.getLogger(__name__)

# summary_source values (step spec §9). Logged for gray-release observation.
SOURCE_MEETING_STATE = "meeting_state"
SOURCE_TRANSCRIPT_FALLBACK = "transcript_fallback"
SOURCE_EMPTY_MEETING = "empty_meeting"

# Why a meeting state was refused. Logged as `fallback_reason`, so an operator
# can tell WHY the rich path was skipped (gray-release debugging).
REASON_DISABLED = "v2_disabled"
REASON_AGENT_DISABLED = "agent_disabled"
REASON_NO_STATE = "no_state"
REASON_STATE_FAILED = "state_failed"
REASON_STATE_EMPTY = "state_empty"
REASON_STATE_STALE = "state_stale"
REASON_STATE_UNREADABLE = "state_unreadable"


class SummaryPlanError(Exception):
    """The plan could not be built at all (e.g. the material is too large).

    A provider-level-shaped failure: it means "do not call the LLM", which
    `summary_service` maps to `summary.status = FAILED` with this reason —
    exactly like the legacy oversized-transcript guard.
    """


class FinalSummarySchemaError(Exception):
    """Shape failure not covered by Pydantic (e.g. a top-level list)."""


@dataclass
class StateAssessment:
    """Whether the meeting state can back the final summary, and why not."""

    usable: bool
    reason: str | None = None
    content: MeetingStateContent = field(default_factory=MeetingStateContent)
    version: int = 0
    last_processed_segment_seq: int = 0
    final_transcript_seq: int = 0
    status: str = "EMPTY"


@dataclass
class SummaryPlan:
    """Everything `summary_service` needs to run Phase B for one attempt.

    Three mutually exclusive execution modes, checked in this order by the
    caller:

      1. `precomputed` is set   -> no LLM call at all (empty meeting, §16).
      2. `prompt` is set        -> normal V2 or legacy generation.
    """

    source: str
    prompt: str = ""
    #: Output parse gate. Raises json.JSONDecodeError / ValidationError /
    #: FinalSummarySchemaError, i.e. exactly what the legacy repair loop already
    #: handles, so `summary_service` needs no new failure branch.
    parse: Callable[[str], MeetingSummarySchema] | None = None
    #: Bounded repair prompt builder: (raw_output, error) -> prompt.
    build_repair: Callable[[str, str], str] | None = None
    #: A fully-formed result that needs no provider call (empty meeting only).
    precomputed: MeetingSummarySchema | None = None
    #: Raw output to persist for a precomputed result (the serialized result).
    precomputed_raw: str = ""
    #: §19 observability fields.
    state_version: int | None = None
    last_processed_segment_seq: int | None = None
    final_transcript_seq: int | None = None
    evidence_segment_count: int = 0
    fallback_reason: str | None = None


# =============================================================================
# Usability gate (§2 ordering rule, §9 fallback triggers)
# =============================================================================

def _final_transcript_seq(db: Session, meeting_id: str) -> int:
    """The last persisted FINAL seq — the cursor target for a usable state.

    Reads the persisted MAX rather than the frozen stream's `last_segment_seq`:
    the question here is "has the state absorbed everything we actually have?",
    which is the persisted segment set.
    """
    return transcript_repository.max_seq(db, meeting_id) or 0


def assess_usability(db: Session, meeting_id: str) -> StateAssessment:
    """Decide whether the meeting state may back the final summary.

    Refuses (-> legacy fallback) when: V2 or the agent is disabled; there is no
    state row; the state is FAILED (its last update did not land); the state is
    empty; the state is unreadable (a corrupt row must never be laundered into
    "no content"); or the state is STALE — its cursor is behind the final
    transcript sequence, i.e. the end of the meeting never made it in.

    Read-only, no lock: a torn read can only cause a fallback, never a wrong
    summary (the plan is built from a single consistent snapshot below).
    """
    final_seq = _final_transcript_seq(db, meeting_id)

    if not settings.final_summary_v2_enabled:
        return StateAssessment(False, REASON_DISABLED, final_transcript_seq=final_seq)
    if not settings.meeting_agent_enabled:
        return StateAssessment(False, REASON_AGENT_DISABLED,
                               final_transcript_seq=final_seq)

    row = state_repository.get(db, meeting_id)
    if row is None:
        return StateAssessment(False, REASON_NO_STATE, final_transcript_seq=final_seq)

    try:
        content = state_repository.get_content(db, meeting_id)
    except state_repository.MeetingStateCorruptError as exc:
        logger.warning(
            "meeting state unreadable for %s; falling back to transcript summary: %s",
            meeting_id, exc,
        )
        return StateAssessment(False, REASON_STATE_UNREADABLE,
                               final_transcript_seq=final_seq)

    assessment = StateAssessment(
        usable=False,
        content=content,
        version=row.version,
        last_processed_segment_seq=row.last_processed_segment_seq,
        final_transcript_seq=final_seq,
        status=row.status,
    )

    if row.status == "FAILED":
        # The last update did not land. The state is still readable and may be
        # mostly right, but we do NOT present it as current: the segments that
        # failed are precisely the end of the meeting.
        assessment.reason = REASON_STATE_FAILED
        return assessment
    if content == MeetingStateContent():
        assessment.reason = REASON_STATE_EMPTY
        return assessment
    if row.last_processed_segment_seq < final_seq:
        # §2: never build the summary on a state older than the transcript. A
        # caller-side flush should have prevented this; reaching here means it
        # did not, so degrade rather than silently under-report the meeting.
        assessment.reason = REASON_STATE_STALE
        return assessment

    assessment.usable = True
    return assessment


# =============================================================================
# Parsing
# =============================================================================

# `_prune_code_fence` is kept as a thin alias: the READ path shares the exact
# same unwrapping (see `final_summary_schemas.prune_code_fence`), and it must
# not drift from the write path.
_prune_code_fence = prune_code_fence


def parse_v2(raw: str) -> tuple[MeetingSummarySchema, FinalMeetingSummary]:
    """Parse V2 output -> (legacy-compatible schema, full V2 result).

    Raises json.JSONDecodeError / ValidationError / FinalSummarySchemaError —
    the same types the legacy `parse_and_validate` raises, so the existing
    bounded-repair loop handles a bad V2 response with no change to
    `summary_service`.
    """
    pruned = _prune_code_fence(raw).strip()
    data = json.loads(pruned)
    if not isinstance(data, dict):
        raise FinalSummarySchemaError(
            f"expected a JSON object, got {type(data).__name__}"
        )
    parsed = FinalMeetingSummary.model_validate(data)
    return to_legacy_schema(parsed), parsed


def empty_meeting_summary() -> tuple[MeetingSummarySchema, str]:
    """The deterministic summary for a meeting with no transcript (§16).

    Deliberately does NOT call the LLM: there is nothing to summarize, and any
    model output for an empty transcript would be fabricated content. The shape
    is legal and terminal (COMPLETED), so an empty meeting still reaches a
    usable Result page instead of 404 or a perpetual GENERATING row.
    """
    legacy = MeetingSummarySchema(
        overview="本次会议未产生有效转写内容。",
        key_topics=[],
        conclusions=[],
        open_questions=[],
    )
    return legacy, legacy.model_dump_json()


# =============================================================================
# Plan building
# =============================================================================

def build_plan(
    db: Session,
    meeting: Meeting,
    *,
    legacy_material: Callable[[], tuple[str, str]],
) -> SummaryPlan:
    """Choose V2 / legacy / empty-meeting and return an executable plan.

    `legacy_material` is a thunk supplied by `summary_service` that builds the
    full-transcript prompt — passed in rather than imported so this module never
    reaches back into the pipeline that calls it (no cycle, and the legacy
    prompt stays defined in exactly one place).
    """
    meeting_id = meeting.id
    assessment = assess_usability(db, meeting_id)

    # --- §16 empty meeting: deterministic, no LLM ---------------------------
    # Checked BEFORE the usability outcome so it applies even when no state row
    # exists at all (the normal shape of an empty meeting: finish + stream-end
    # with zero FINALs).
    if assessment.final_transcript_seq == 0:
        legacy, raw = empty_meeting_summary()
        plan = SummaryPlan(
            source=SOURCE_EMPTY_MEETING,
            precomputed=legacy,
            precomputed_raw=raw,
            state_version=assessment.version,
            last_processed_segment_seq=assessment.last_processed_segment_seq,
            final_transcript_seq=0,
        )
        _log_plan(meeting_id, plan)
        return plan

    if not assessment.usable:
        prompt, _body = legacy_material()
        plan = SummaryPlan(
            source=SOURCE_TRANSCRIPT_FALLBACK,
            prompt=prompt,
            parse=_legacy_parse,
            # The legacy prompt embeds the transcript inline, so its repair
            # variant re-sends `prompt` whole with an empty boundary: the model
            # still has the material, and the job is only to fix the shape.
            build_repair=lambda raw, err: _legacy_repair(prompt, raw, err),
            state_version=assessment.version,
            last_processed_segment_seq=assessment.last_processed_segment_seq,
            final_transcript_seq=assessment.final_transcript_seq,
            fallback_reason=assessment.reason,
        )
        _log_plan(meeting_id, plan)
        return plan

    # --- V2: meeting state + selected evidence ------------------------------
    segments = transcript_repository.list_sorted(db, meeting_id)
    selection = evidence_module.select_evidence(
        assessment.content,
        segments,
        max_evidence_segments=settings.final_summary_max_evidence_segments,
        neighbor_window=settings.final_summary_evidence_neighbor_window,
    )
    summary_input = build_summary_input(
        meeting_id=meeting_id,
        title=meeting.title,
        started_at=meeting.started_at.isoformat() if meeting.started_at else None,
        ended_at=meeting.ended_at.isoformat() if meeting.ended_at else None,
        state=assessment.content,
        state_version=assessment.version,
        last_processed_segment_seq=assessment.last_processed_segment_seq,
        selection=selection,
    )
    prompt = build_v2_prompt(summary_input)
    if len(prompt) > settings.final_summary_max_input_chars:
        raise SummaryPlanError(
            f"final summary input too large ({len(prompt)} chars > "
            f"{settings.final_summary_max_input_chars}); refusing to silently truncate"
        )

    plan = SummaryPlan(
        source=SOURCE_MEETING_STATE,
        prompt=prompt,
        parse=_v2_parse,
        build_repair=lambda raw, err: build_v2_repair_prompt(prompt, raw, err),
        state_version=assessment.version,
        last_processed_segment_seq=assessment.last_processed_segment_seq,
        final_transcript_seq=assessment.final_transcript_seq,
        evidence_segment_count=len(selection.segments),
    )
    _log_plan(meeting_id, plan)
    return plan


def _legacy_parse(raw: str) -> MeetingSummarySchema:
    from app.agent.summary_service import parse_and_validate

    return parse_and_validate(raw)


def _legacy_repair(prompt: str, raw: str, error: str) -> str:
    """Delegate to the legacy repair prompt (keeps one definition).

    The legacy prompt already embeds the transcript inline, so the repair
    variant re-sends `prompt` whole and passes an empty boundary — the model
    still has the material, and the job here is only to fix the shape.
    """
    from app.agent.summary_service import build_repair_prompt

    return build_repair_prompt(prompt, "", raw, error)


def _v2_parse(raw: str) -> MeetingSummarySchema:
    legacy, _full = parse_v2(raw)
    return legacy


def _log_plan(meeting_id: str, plan: SummaryPlan) -> None:
    """§19 observability. Never logs transcript text or model output."""
    logger.info(
        "final_summary_plan meeting_id=%s summary_source=%s state_version=%s "
        "last_processed_segment_seq=%s final_transcript_seq=%s "
        "evidence_segment_count=%s%s",
        meeting_id,
        plan.source,
        plan.state_version,
        plan.last_processed_segment_seq,
        plan.final_transcript_seq,
        plan.evidence_segment_count,
        f" fallback_reason={plan.fallback_reason}" if plan.fallback_reason else "",
    )


__all__ = [
    "REASON_AGENT_DISABLED",
    "REASON_DISABLED",
    "REASON_NO_STATE",
    "REASON_STATE_EMPTY",
    "REASON_STATE_FAILED",
    "REASON_STATE_STALE",
    "REASON_STATE_UNREADABLE",
    "SOURCE_EMPTY_MEETING",
    "SOURCE_MEETING_STATE",
    "SOURCE_TRANSCRIPT_FALLBACK",
    "FinalSummarySchemaError",
    "StateAssessment",
    "SummaryPlan",
    "SummaryPlanError",
    "assess_usability",
    "build_plan",
    "empty_meeting_summary",
    "parse_v2",
]
