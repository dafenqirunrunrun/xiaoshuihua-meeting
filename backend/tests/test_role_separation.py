"""Responsibility separation between the observer and the referee.

`meeting_state` and `captain` read the SAME committed FINAL stream and, before
this change, could converge on the same output: a summary of the discussion.
That convergence is the defect this file pins. When it happens, the referee
reads as "the meeting summarised again" instead of a judgement, and the two
capabilities become indistinguishable to the user even though every individual
component works.

These are PROMPT-level tests, which is unusual and deliberate. The separation
is not a data-flow property: both agents already receive raw transcript segments
and neither can structurally be prevented from emitting prose about them. The
only place the boundary can be expressed is the instruction, so the instruction
is what is tested. Schema-level consequences (a referee report must carry
judgement material that the observer has no field for) are covered in
`test_captain.py`, not here.

Like the prompt-adjacent tests in `test_meeting_state_agent.py`, these assert on
SUBSTRINGS of the built prompt rather than on the model's behaviour: the model
is not available offline, and a scripted provider would just echo whatever it
was told. What is checkable offline is that the constraint is actually present
in the text the model will read — the failure mode being guarded against is a
future edit silently dropping it.
"""

from __future__ import annotations

from app.meeting_state.prompts import build_update_prompt
from app.meeting_state.schemas import MeetingStateContent
from app.transcript.segment_model import TranscriptSegment


def _segments(*texts: str) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            meeting_id="m",
            segment_seq=i,
            utterance_id=f"u{i}",
            speaker_id="s",
            event_id=f"e{i}",
            start_ms=i * 1000,
            end_ms=i * 1000 + 900,
            text=text,
        )
        for i, text in enumerate(texts, start=1)
    ]


def _observer_prompt() -> str:
    return build_update_prompt(
        "上线评审",
        MeetingStateContent(),
        _segments("功能测试过了", "压测还没做"),
        last_processed_segment_seq=0,
    )


def _referee_prompt() -> str:
    from app.captain.context import ContextWindow
    from app.captain.prompts import build_report_prompt

    window = ContextWindow(
        segments=_segments("功能测试过了", "压测还没做"),
        from_seq=1,
        window_to_seq=2,
        truncated=False,
        dropped_count=0,
        used_position_fallback=False,
    )
    return build_report_prompt(
        meeting_title="上线评审", window=window, window_seconds=180.0
    )


# --- observer side ---------------------------------------------------------

class TestObserverPrompt:
    """The meeting-state agent must declare itself an observer, not a judge."""

    def test_declares_it_is_not_a_referee(self):
        prompt = _observer_prompt()
        assert "会议实时状态维护器" in prompt
        assert "不是会议裁判" in prompt

    def test_states_the_capability_handoff(self):
        """The prompt must name who the judgement belongs to.

        Without this the model has a prohibition but no place to put the
        material, and a prohibition alone tends to leak: the model records the
        judgement anyway "because it noticed something".
        """
        assert "水华队长" in _observer_prompt()

    def test_forbids_judging_each_side(self):
        prompt = _observer_prompt()
        assert "不判断谁对谁错" in prompt
        assert "不判断证据强弱" in prompt

    def test_forbids_seeking_counter_evidence(self):
        assert "不主动寻找反证" in _observer_prompt()

    def test_forbids_decision_advice(self):
        assert "不给决策建议" in _observer_prompt()


# --- referee side ----------------------------------------------------------

class TestRefereePrompt:
    """The referee must declare itself a judge, not a summariser."""

    def test_declares_itself_not_a_minute_taker(self):
        prompt = _referee_prompt()
        assert "不是会议记录员" in prompt

    def test_forbids_regenerating_a_full_summary(self):
        prompt = _referee_prompt()
        assert "不要重新生成一份完整的会议摘要" in prompt

    def test_demotes_discussion_summary_to_a_locator(self):
        """`discussion_summary` is the field most likely to become a minute.

        The rule must name the FIELD, not just the concept, or the model has no
        way to know which part of its own output the constraint applies to.
        """
        assert "discussion_summary" in _referee_prompt()

    def test_requires_judgement_material(self):
        prompt = _referee_prompt()
        assert "禁止只转述不判断" in prompt
        # The specific material the observer is forbidden to produce.
        for required in ("反证", "已确认的事实", "缺失"):
            assert required in prompt, required


# --- the boundary itself ---------------------------------------------------

class TestBoundaryIsMutual:
    """Both prompts must know where the other one starts.

    A one-sided rule is easy to satisfy and easy to drift out of: the observer
    stops judging, but the referee still opens with a full summary, and the user
    still sees the same content twice. The boundary only holds if each side is
    told what the other owns.
    """

    def test_observer_knows_the_referee_owns_judgement(self):
        assert "水华队长" in _observer_prompt()

    def test_referee_knows_the_observer_owns_meeting_state(self):
        prompt = _referee_prompt()
        assert "实时" in prompt and "状态" in prompt

    def test_the_two_prompts_are_not_interchangeable(self):
        """Neither prohibitions list belongs in the other's prompt.

        Anchored on the exact phrases from each side. If a refactor ever merges
        the two prompt modules — the tempting "one shared base prompt" cleanup —
        this fails immediately.
        """
        observer = _observer_prompt()
        referee = _referee_prompt()
        assert "不是会议裁判" in observer and "不是会议裁判" not in referee
        assert "不是会议记录员" in referee and "不是会议记录员" not in observer


# --- two-stage referee: BOTH stages must carry the boundary ------------------

def _quick_prompt() -> str:
    from app.captain.context import ContextWindow
    from app.captain.prompts import build_quick_prompt

    window = ContextWindow(
        segments=_segments("功能测试过了", "压测还没做"),
        from_seq=1,
        window_to_seq=2,
        truncated=False,
        dropped_count=0,
        used_position_fallback=False,
    )
    return build_quick_prompt(
        meeting_title="上线评审", window=window, window_seconds=180.0
    )


def _deep_prompt() -> str:
    from app.captain.context import ContextWindow
    from app.captain.prompts import build_deep_prompt

    window = ContextWindow(
        segments=_segments("功能测试过了", "压测还没做"),
        from_seq=1,
        window_to_seq=2,
        truncated=False,
        dropped_count=0,
        used_position_fallback=False,
    )
    return build_deep_prompt(
        meeting_title="上线评审", window=window, window_seconds=180.0
    )


class TestTwoStageStagesBothCarryTheBoundary:
    """The product path is two-stage, and it is a DIFFERENT prompt pair.

    `TestRefereePrompt` above pins `build_report_prompt` — the single-pass
    referee, still the default. Once two-stage is on (Task 4), the model reads
    `build_quick_prompt` and `build_deep_prompt` instead, and neither was
    covered: a regression that dropped the "not a minute-taker" clause from
    either would leave the single-pass suite green while the SHIPPED path
    drifted back into summarising. That is exactly the confusion this whole
    file exists to prevent, so it must be pinned where the product actually
    reads.
    """

    def test_quick_declares_itself_not_a_minute_taker(self):
        prompt = _quick_prompt()
        assert "不是会议记录员" in prompt
        assert "不要输出会议摘要" in prompt

    def test_quick_names_the_other_capability_as_the_state_owner(self):
        """Quick must know WHY it may not give meeting status: someone else does."""
        assert "另一个能力维护" in _quick_prompt()

    def test_deep_declares_itself_not_a_minute_taker(self):
        assert "不是会议记录员" in _deep_prompt()

    def test_deep_forbids_regenerating_a_full_summary(self):
        assert "禁止重新生成完整的会议摘要" in _deep_prompt()

    def test_deep_requires_the_judgement_material(self):
        """The three lists the field names put in front of the model.

        Asserted by FIELD NAME, not by concept: the model needs to know which
        part of its own output a guarantee applies to.
        """
        prompt = _deep_prompt()
        for field in ("confirmed_facts", "assumptions", "missing_evidence", "supersedes"):
            assert field in prompt, field
        assert "禁止只转述不判断" in prompt

    def test_neither_stage_offers_a_percentage(self):
        """Both stages must refuse the arbitrary 60/40 split.

        The spec forbids it and the schema has no numeric field for it, so the
        instruction is the only remaining place it could be invited in.
        """
        assert "禁止使用百分比" in _deep_prompt()
        assert "不要给出百分比或分数" in _quick_prompt()

    def test_quick_is_the_early_stage_and_says_so(self):
        """Quick's own prompt must frame its output as provisional.

        If Quick omitted it, the model could present a first read as final and
        then Deep would have nothing to supersede — the reversal would be
        silent because it was never declared a reversal.
        """
        assert "初步" in _quick_prompt()

    # The complementary half — that Quick is never GIVEN external evidence, so
    # the 1-2s target cannot be blocked on the network — lives in
    # `test_captain_two_stage.py::test_quick_prompt_carries_no_external_evidence`.
    # That one is a call-site property (which arguments `run_analysis` passes),
    # so it is pinned next to the call chain rather than here.
