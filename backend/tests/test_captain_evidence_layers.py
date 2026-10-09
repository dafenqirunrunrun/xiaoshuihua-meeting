"""Phase 3-2 §7/§8 at the PROMPT level: how the three layers are ranked.

`test_captain_research.py` proves the router refuses internal questions and
never spends a provider call on them. This file proves the complementary half:
that the model is TOLD how to weigh what it is given.

Both halves are necessary and neither is sufficient. The router can only refuse
what it recognises; a question phrased obliquely enough to pass the markers
still reaches the model, and a model with no stated priority will average the
layers — which is exactly what the spec forbids ("优先级：Meeting Facts >
External Evidence > Model Inference"). Conversely the prompt cannot un-search a
provider call the router already made.

These tests assert on the prompt STRING because that is the contract with the
model; the pipeline-level guarantee (external evidence is overwritten from
provider results after generation) is pinned in `test_captain_search.py`.
"""

from __future__ import annotations

import pytest

from app.captain.context import ContextWindow
from app.captain.prompts import build_deep_prompt, build_quick_prompt
from app.captain.schemas import ExternalEvidence


def _window(*texts: str) -> ContextWindow:
    from app.transcript.segment_model import TranscriptSegment

    segments = [
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
    return ContextWindow(
        segments=segments,
        from_seq=1,
        window_to_seq=len(segments),
        truncated=False,
        dropped_count=0,
        used_position_fallback=False,
    )


def _source(n: int = 1) -> ExternalEvidence:
    return ExternalEvidence(
        source_id=f"web_{n}",
        title=f"参考资料 {n}",
        url=f"https://example{n}.test/x",
        context="示例内容。",
        domain=f"example{n}.test",
        retrieved_at="2026-09-29T10:00:00+00:00",
        provider="fake",
        source_quality="official",
        answers_question="claim_1",
    )


def _deep(with_external: bool) -> str:
    return build_deep_prompt(
        meeting_title="示例会议",
        window=_window("压测还没做", "发布会定在下周五"),
        window_seconds=120.0,
        external_evidence=[_source()] if with_external else None,
    )


class TestLayerPriorityIsStated:
    def test_deep_declares_the_strict_priority_order(self):
        prompt = _deep(with_external=True)

        # The order itself, not merely the existence of a preference: a model
        # told the layers are "different sources" will average them.
        assert "会议事实 > 外部资料 > 模型推断" in prompt

    def test_internal_topics_are_named_as_unresolvable_externally(self):
        prompt = _deep(with_external=True)

        assert "内部" in prompt
        assert "需要内部信息" in prompt or "无法通过外部资料解决" in prompt

    def test_network_content_may_not_be_dressed_as_meeting_fact(self):
        prompt = _deep(with_external=True)

        assert "禁止把网络内容伪装成会议事实" in prompt

    def test_priority_rules_only_apply_when_sources_exist(self):
        """With no sources there is no second layer to rank.

        Adding the priority rules to a local-only run would be instructing the
        model about a block that is not in the prompt — and would break the
        byte-equivalence Phase 3-2E depends on for its baseline arm.
        """
        assert "会议事实 > 外部资料" not in _deep(with_external=False)


class TestExternalRulesAreScoped:
    def test_quality_field_is_explained_and_ranked(self):
        prompt = _deep(with_external=True)

        assert "source_quality" in prompt
        # official/standard establish facts; the weaker classes are only leads.
        assert "official" in prompt and "standard" in prompt
        assert "secondary" in prompt

    def test_the_answering_claim_is_bound(self):
        prompt = _deep(with_external=True)

        assert "answers_claim" in prompt

    def test_sources_are_shown_with_quality_and_claim(self):
        """The rendered block must carry the provenance the rules refer to.

        A rule that describes a field the model never sees is unenforceable.
        """
        prompt = _deep(with_external=True)

        assert "source_quality: official" in prompt
        assert "answers_claim: claim_1" in prompt

    def test_external_block_is_absent_without_sources(self):
        prompt = _deep(with_external=False)

        assert "<external_evidence>" not in prompt
        assert "web_" not in prompt


class TestTheRankingIsSharedAcrossCallPaths:
    """The priority rules travel WITH the block, not with one prompt.

    `captain_two_stage_enabled` defaults to False, so the single-pass
    `build_report_prompt` is still a live call path. If the ranking lived in
    `_render_deep_rules`, that path would show sources with no stated priority
    — the same document behaving differently depending on a feature flag.
    """

    def test_single_pass_prompt_carries_the_priority_order_when_sources_exist(self):
        from app.captain.prompts import build_report_prompt

        prompt = build_report_prompt(
            meeting_title="示例会议",
            window=_window("压测还没做"),
            window_seconds=120.0,
            external_evidence=[_source()],
        )

        assert "会议事实 > 外部资料 > 模型推断" in prompt
        assert "source_quality: official" in prompt

    def test_single_pass_prompt_is_unchanged_without_sources(self):
        """The Phase 3-2E baseline equivalence must hold.

        The search-off arm has to be byte-comparable to the pre-3-2 referee, so
        no priority rule may appear when there is nothing to rank.
        """
        from app.captain.prompts import build_report_prompt

        prompt = build_report_prompt(
            meeting_title="示例会议",
            window=_window("压测还没做"),
            window_seconds=120.0,
            external_evidence=None,
        )

        assert "会议事实 > 外部资料" not in prompt
        assert "<external_evidence>" not in prompt


class TestQuickNeverSeesTheNetwork:
    def test_quick_prompt_has_no_external_block_even_when_sources_were_fetched(self):
        """Quick must never be blocked on the network.

        The call chain already passes `external_evidence=None` to Quick (see
        `service.run_analysis`); this pins the prompt-level consequence, since a
        future change could start passing the block through "for symmetry" and
        silently put search latency in front of the 1-2s target.
        """
        prompt = build_quick_prompt(
            meeting_title="示例会议",
            window=_window("压测还没做"),
            window_seconds=120.0,
            external_evidence=None,
        )

        assert "<external_evidence>" not in prompt
        assert "会议事实 > 外部资料" not in prompt

    def test_quick_still_declares_it_is_not_a_minute_taker(self):
        prompt = build_quick_prompt(
            meeting_title="示例会议",
            window=_window("压测还没做"),
            window_seconds=120.0,
        )

        assert "不要输出会议摘要" in prompt
