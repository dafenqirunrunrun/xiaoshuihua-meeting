"""FakeProvider shape-dispatch contract (LLM-CONFIG-1 §4).

The FakeProvider is offline/CI infrastructure, but it had a real defect: it
returned ONE fixed JSON — the legacy summary shape — for every prompt. AGENT-1
validates `MeetingStateUpdate` (``extra="forbid"``), which the legacy shape is
not a valid instance of, so every default-configured local run failed the state
update and silently degraded to `transcript_fallback`. Local dev therefore never
exercised the AGENT-1 -> AGENT-2 path at all.

The fix dispatches on the real prompts' own delimiter markers. These tests pin
that contract so it cannot silently drift again:

  * each default VALIDATES against its real Pydantic model (the actual bug);
  * the defaults are built from the REAL prompt builders, not invented strings,
    so renaming a delimiter in a prompt module fails here rather than in prod;
  * `responses` FIFO and `error` still take precedence (existing suites rely
    on both).
"""

from __future__ import annotations

import json

import pytest

from app.agent.final_summary_prompts import build_summary_input, build_v2_prompt
from app.agent.final_summary_schemas import FinalMeetingSummary
from app.agent.llm.fake import (
    DEFAULT_JSON,
    DEFAULT_STATE_JSON,
    DEFAULT_V2_JSON,
    FakeProvider,
)
from app.agent.summary_schemas import MeetingSummarySchema
from app.meeting_state.prompts import build_update_prompt
from app.meeting_state.schemas import MeetingStateContent, MeetingStateUpdate
from app.transcript.segment_model import TranscriptSegment


@pytest.fixture
def seg() -> TranscriptSegment:
    return TranscriptSegment(
        meeting_id="m1", segment_seq=1, utterance_id="u1", speaker_id="unknown",
        event_id="e1", start_ms=0, end_ms=500, text="今天讨论上线时间",
    )


def _state_prompt(seg: TranscriptSegment) -> str:
    """AGENT-1's prompt, exactly as the service builds it."""
    return build_update_prompt(
        "测试会议", MeetingStateContent(), [seg], last_processed_segment_seq=0,
    )


def _v2_prompt(seg: TranscriptSegment) -> str:
    """AGENT-2's prompt, exactly as the service builds it."""
    from app.agent.evidence import EvidenceSelection

    return build_v2_prompt(
        build_summary_input(
            meeting_id="m1", title="测试会议", started_at=None, ended_at=None,
            state=MeetingStateContent(), state_version=0,
            last_processed_segment_seq=1,
            selection=EvidenceSelection(
                segments=[seg], cited_seqs=[1], truncated=False, dropped_count=0,
            ),
        )
    )


LEGACY_PROMPT = "你是内部会议纪要助手。请阅读以下会议的完整逐字转写，输出 JSON。"


# -- dispatch: each prompt gets the shape IT asks for ------------------------

def test_state_prompt_gets_state_shape(seg):
    assert FakeProvider().complete(_state_prompt(seg)) == DEFAULT_STATE_JSON


def test_v2_prompt_gets_v2_shape(seg):
    assert FakeProvider().complete(_v2_prompt(seg)) == DEFAULT_V2_JSON


def test_legacy_prompt_keeps_legacy_shape():
    assert FakeProvider().complete(LEGACY_PROMPT) == DEFAULT_JSON


def test_v2_prompt_is_not_mistaken_for_a_state_update(seg):
    """The V2 prompt embeds <meeting_state>, so marker ORDER matters.

    If the state marker were checked first, AGENT-2 would receive a state-update
    payload and the summary would fail validation instead.
    """
    prompt = _v2_prompt(seg)
    assert FakeProvider().complete(prompt) != DEFAULT_STATE_JSON


# -- the defaults must actually VALIDATE (this was the bug) ------------------

def test_state_default_validates_against_meeting_state_update():
    MeetingStateUpdate.model_validate(json.loads(DEFAULT_STATE_JSON))


def test_v2_default_validates_against_final_meeting_summary():
    FinalMeetingSummary.model_validate(json.loads(DEFAULT_V2_JSON))


def test_legacy_default_validates_against_meeting_summary_schema():
    MeetingSummarySchema.model_validate(json.loads(DEFAULT_JSON))


# -- existing behaviour must be preserved -----------------------------------

def test_responses_fifo_takes_precedence_over_dispatch(seg):
    fake = FakeProvider(responses=["first", "second"])
    assert fake.complete(_state_prompt(seg)) == "first"
    assert fake.complete(_v2_prompt(seg)) == "second"


def test_error_raises_before_dispatch(seg):
    from app.agent.llm.base import SummaryGenerationError

    fake = FakeProvider(error="boom")
    with pytest.raises(SummaryGenerationError):
        fake.complete(_state_prompt(seg))


def test_calls_are_recorded(seg):
    fake = FakeProvider()
    fake.complete(_state_prompt(seg))
    assert len(fake.calls) == 1
    assert _state_prompt(seg) in fake.calls[0]
