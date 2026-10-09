"""ADR-002 Phase 2-C: 水华队长 scenario evaluation.

Six classic meeting shapes, driven through the REAL pipeline
(`trigger -> worker -> context -> prompt -> provider -> Pydantic -> evidence`),
asserting what the REFEREE CONTRACT guarantees rather than what a particular
model happens to say.

What this file can and cannot prove
-----------------------------------
The suite runs offline on scripted providers (`LLM_PROVIDER=fake`), and a
scripted provider does not reason. So a test that feeds in "the model answers
X" and asserts "we got X" would be a tautology — it would describe the fixture,
not the referee. Every assertion here is therefore one of:

  (a) a STRUCTURAL guarantee the pipeline enforces regardless of model output —
      the anchor is excluded, positions are bounded 1..5, `side_a`/`side_b`
      cannot exist, a fabricated quote cannot survive, `uncertainty` is
      non-empty, a failure leaves no partial report;
  (b) a STATEMENT ABOUT THE MATERIAL the model was given — the reversal segment
      is inside the window, the hedge is inside the window — which is what makes
      a correct report possible at all;
  (c) a REGRESSION PIN on a realistic full report shape.

Whether the real LLM narrates an opinion shift well, or refrains from inventing
a conflict on its own, is NOT verifiable in this environment and is not claimed
here. `scripts/replay_real_provider*.py` is what exercises the real model; the
`fake` provider's own docstring says the same thing.

`assert_report_is_grounded` is the load-bearing helper: it re-derives grounding
from the raw transcript AFTER the report was written, so it is an independent
check rather than a restatement of what the pipeline already did.
"""

from __future__ import annotations

import json
import re

import pytest

from app.captain.evidence import _LOOSE as _EVIDENCE_LOOSE
from app.captain.worker import captain_worker
from tests.test_captain import (  # noqa: F401  (helper reuse, Phase 2-B)
    WindowProvider,
    _make_meeting,
    _patch_provider,
    _post_final,
    _trigger,
)

# A neutral sentence used as the trigger anchor throughout. It must NOT contain
# the wake word: a FINAL containing 「水华队长」 auto-triggers its own report and
# would throttle the explicit trigger below (429), which would make these
# scenario tests measure the rate limiter instead of the referee.
_ANCHOR_LINE = "先讨论到这里，继续推进。"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _run_scenario(
    client,
    monkeypatch,
    lines: list[str],
    *,
    provider=None,
    anchor: int | None = None,
):
    """Ingest `lines` as FINALs and produce ONE referee report over them.

    `lines` end with a neutral closing line that becomes the anchor. Whether the
    anchor is analysed depends on the TRIGGER, not on the field: this helper
    omits `anchor`, i.e. the button path, where the anchor is the current
    high-water mark and is ordinary content — so it IS in the window. (The
    wake-word path is the one that excludes it; see `captain/context.py`.)

    Returns (meeting_id, report_dict, provider). The report dict is the raw
    API response (`CaptainReportPublic`), so assertions see exactly what the
    product sees.
    """
    meeting_id = _make_meeting(client)
    for i, text in enumerate(lines, start=1):
        _post_final(client, meeting_id, i, text)

    provider = provider if provider is not None else WindowProvider()
    _patch_provider(monkeypatch, provider)

    body = _trigger(client, meeting_id, anchor=anchor).json()
    assert captain_worker.drain(timeout=10.0), "captain worker did not drain"

    report = client.get(
        f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
    ).json()
    return meeting_id, report, provider


def _window_texts(provider: WindowProvider) -> dict[int, str]:
    """The segments the model actually read, keyed by seq.

    Read back out of the prompt the provider received, so the grounding check
    is anchored to the REAL window rather than to a re-derivation of it.

    `[1]`, not `[-1]`. The tag appears MORE THAN ONCE in a prompt that carries
    external evidence: the `<external_evidence>` supplementary rules name the
    window block by its delimiter when they state which material the referee may
    attribute to participants. `split(...)[1]` is the block itself; `[-1]` used
    to be the same thing only because the tag was unique, and silently became
    the rules text the moment a second mention existed — which would have made
    every grounding assertion pass against an empty window instead of failing.
    """
    prompt = provider.calls[-1] if provider.calls else ""
    body = prompt.split("<discussion_window>")[1].split("</discussion_window>")[0]
    return {
        int(seq): text
        for seq, text in re.findall(r"^\[(\d+)\] (.*)$", body, re.MULTILINE)
    }


def assert_report_is_grounded(report: dict, window: dict[int, str]) -> None:
    """Every citation in the report must resolve against the window.

    Deliberately re-checks what `captain/evidence.py` already enforced. A
    checker that trusts the previous checker verifies nothing; this one reads
    the finished product and the raw transcript and demands they agree.

    Two properties, both hard:
      * every cited `segment_seq` exists in the window;
      * every `quote` is present in its cited segment's text.
    """
    assert report["status"] == "COMPLETED", report["status"]
    content = report["report"]
    assert content is not None

    def _check_seqs(seqs: list[int], where: str) -> None:
        for seq in seqs:
            assert seq in window, f"{where} cites segment {seq}, not in window {sorted(window)}"

    for pos in content["positions"]:
        _check_seqs(pos["source_segment_seqs"], f"position {pos['label']!r}")
    for point in content["agreement_points"]:
        _check_seqs(point["source_segment_seqs"], "agreement_point")
    for point in content["dispute_points"]:
        _check_seqs(point["source_segment_seqs"], "dispute_point")

    for item in content["evidence"]:
        seq = item["segment_seq"]
        assert seq in window, f"evidence cites segment {seq}, not in window"
        haystack = _EVIDENCE_LOOSE.sub("", window[seq] or "")
        needle = _EVIDENCE_LOOSE.sub("", item["quote"] or "")
        assert needle and needle in haystack, (
            f"evidence quote is not verbatim in segment {seq}: {item['quote']!r}"
        )


def _declared_fields() -> set[str]:
    """The field names `CaptainReportContent` accepts (the full JSON contract)."""
    from app.captain.schemas import CaptainReportContent

    return set(CaptainReportContent.model_fields)


# ==========================================================================
# 1. clear conflict
# ==========================================================================


class TestClearConflict:
    def test_clear_conflict_generates_multiple_positions(self, client, monkeypatch):
        """Two opposed positions must both survive into the report.

        `positions >= 2` is a STRUCTURAL claim here only in the sense that the
        schema permits up to 5 and the pipeline does not collapse them; the
        scripted provider supplies two, so what this pins is that nothing
        between the provider and the API turns two into one.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "检索速度是当前最痛的点，应该优先优化检索速度",
                "速度已经够用了，准确率才是瓶颈，应该优先提升准确率",
                "我仍然认为准确率更重要，慢一点可以接受",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=2, extra={
                "dispute_points": [{
                    "text": "优先级：检索速度 vs 准确率",
                    "source_segment_seqs": [1, 2],
                }],
            }),
        )

        content = report["report"]
        assert len(content["positions"]) >= 2, "two opposed positions were merged"
        assert content["dispute_points"], "dispute_points was emptied for a clear conflict"
        assert_report_is_grounded(report, _window_texts(provider))

    def test_clear_conflict_keeps_both_sides_citable(self, client, monkeypatch):
        """Both positions must be traceable to material the model actually read."""
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "优先优化检索速度",
                "优先提升准确率",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=2, extra={
                "positions": [
                    {"label": "速度优先", "statement": "先优化检索速度。",
                     "source_segment_seqs": [1]},
                    {"label": "准确率优先", "statement": "先提升准确率。",
                     "source_segment_seqs": [2]},
                ],
            }),
        )

        window = _window_texts(provider)
        # Both sides of the disagreement are inside the window, so neither
        # position had to be grounded in material the model could not see.
        assert {1, 2} <= set(window), window
        assert_report_is_grounded(report, window)


# ==========================================================================
# 2. multi position
# ==========================================================================


class TestMultiPosition:
    def test_three_positions_are_not_collapsed(self, client, monkeypatch):
        """Three distinct positions must not be forced into a two-sided frame."""
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "继续使用 MySQL 就够了，迁移成本太高",
                "数据量很快会撑不住，应该迁移到 TiDB",
                "先别动存储层，把业务模块拆开才是当务之急",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=3),
        )

        content = report["report"]
        assert len(content["positions"]) == 3, "three positions were not preserved"
        # Distinct labels — a genuine multi-position reading, not three copies.
        labels = [p["label"] for p in content["positions"]]
        assert len(set(labels)) == 3, labels

    def test_no_side_a_side_b_anywhere_in_the_payload(self, client, monkeypatch):
        """The abandoned two-sided schema must be absent from the wire format.

        `side_a` / `side_b` was the V0 shape before the opinion model was
        changed to autonomous N-cluster discovery (ADR-002 §4). This asserts the
        replacement is TOTAL: the strings appear nowhere in the response the
        frontend receives, not merely as absent model fields.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "继续使用 MySQL",
                "迁移 TiDB",
                "先拆业务模块",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=3),
        )

        rendered = json.dumps(report, ensure_ascii=False)
        assert "side_a" not in rendered
        assert "side_b" not in rendered
        # And the schema has no such field to begin with.
        assert not {"side_a", "side_b"} & _declared_fields()


# ==========================================================================
# 3. no conflict
# ==========================================================================


class TestNoConflict:
    def test_no_conflict_does_not_invent_dispute(self, client, monkeypatch):
        """A division of labour is not a disagreement.

        The failure this guards is the expensive one: telling a team they are
        deadlocked when they are actually aligned. `dispute_points` must be
        allowed to be empty, and the schema must not pressure the model to fill
        it (no min_length on the list).
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "我来负责开发",
                "我来负责测试",
                "文档我来写",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=1),
        )

        content = report["report"]
        assert content["dispute_points"] == [], "invented a dispute where there is none"
        assert len(content["positions"]) == 1
        assert_report_is_grounded(report, _window_texts(provider))

    def test_dispute_points_is_not_schema_required(self, client, monkeypatch):
        """Structural form of the same guarantee: an empty list is VALID.

        Asserted on the schema directly, because the prompt may still ask for
        the field — what must not exist is a structural lower bound that forces
        the model to produce a conflict to pass validation.
        """
        from app.captain.schemas import CaptainReportContent, Position

        content = CaptainReportContent(
            discussion_summary="各方确认了分工。",
            positions=[Position(label="分工确认", statement="按角色分工推进。")],
            dispute_points=[],  # must be accepted
            uncertainty="未讨论时间安排。",
            suggestion="建议明确交付时间。",
        )
        assert content.dispute_points == []
        # And the same holds for the pipeline's own validation gate.
        from app.captain.service import parse_report

        parsed = parse_report(json.dumps(content.model_dump(), ensure_ascii=False))
        assert parsed.dispute_points == []


# ==========================================================================
# 4. insufficient evidence
# ==========================================================================


class TestInsufficientEvidence:
    def test_uncertainty_is_present_and_substantive(self, client, monkeypatch):
        """With only hedged material, the report must state what it does not know.

        Both inputs are deliberately weak ("可能更好" / "也有优势"). The referee
        cannot resolve them, and the contract requires it to SAY so rather than
        to project confidence it has not earned.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "方案A可能更好一些",
                "方案B也有它的优势",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=2, extra={
                "uncertainty": "两方都只给了倾向性表述，缺少成本、排期等可比数据。",
            }),
        )

        content = report["report"]
        assert content["uncertainty"].strip(), "uncertainty was empty"
        # The schema cannot even represent "no uncertainty".
        assert content["uncertainty"] != "无"

    def test_report_has_no_field_carrying_a_verdict(self, client, monkeypatch):
        """No decision, no winner: the shape cannot express one.

        This is the STRUCTURAL half of "不强行选择某个方案" and the only half
        that is verifiable offline. `suggestion` is the nearest field to a
        recommendation, and the prompt constrains it to "what to clarify next" —
        that part is a prompt obligation, asserted by contract here, not claimed
        as model behaviour.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            ["方案A可能更好", "方案B也有优势", _ANCHOR_LINE],
            provider=WindowProvider(positions=2),
        )

        fields = _declared_fields()
        # Nothing in the contract can hold a chosen option or a score.
        for forbidden in (
            "decision", "verdict", "recommendation", "winner",
            "chosen_option", "score", "side_a", "side_b",
        ):
            assert forbidden not in fields, f"{forbidden} is a decision field"

        # Every position is a claim with evidence, never a ruling. The schema
        # gives a position exactly label/statement/citations — no verdict slot.
        from app.captain.schemas import Position

        assert set(Position.model_fields) == {"label", "statement", "source_segment_seqs"}
        assert report["report"]["suggestion"].strip()


# ==========================================================================
# 5. opinion shift
# ==========================================================================


class TestOpinionShift:
    def test_reversal_material_is_inside_the_window(self, client, monkeypatch):
        """The whole arc — position, new evidence, reversal — must be visible.

        This is the real, verifiable claim about opinion shift in an offline
        suite. A referee can only notice a change of position if the segment
        containing the reversal is IN the window; if the window were cut short or
        ordered wrongly, the shift would be invisible and the report would
        describe a discussion that never happened.

        Whether the model then NARRATES the shift is a model-quality question
        and is explicitly not asserted here.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            [
                "我支持方案A，上线快",
                "补充一个信息：方案A的成本是方案B的三倍",
                "成本差这么多的话，我转向方案B",
                _ANCHOR_LINE,
            ],
            provider=WindowProvider(positions=2, extra={
                "dispute_points": [],
                "uncertainty": "讨论过程中立场发生了变化，需要确认最终结论。",
            }),
        )

        window = _window_texts(provider)
        # 1..4 — the button-path anchor (seq 4, the neutral closing line) is
        # content and is included.
        assert sorted(window) == [1, 2, 3, 4], window
        # The reversal segment is present AND carries its own text, so the model
        # had the material to see the shift rather than an unexplained flip.
        assert "转向方案B" in window[3]
        # The new information that CAUSED the shift is present too.
        assert "三倍" in window[2]
        assert_report_is_grounded(report, window)

    def test_window_preserves_chronological_order(self, client, monkeypatch):
        """Order matters: a shift read out of order is not a shift.

        `select_window` accumulates newest-first to bound size, then re-sorts.
        If it returned the accumulation order, the reversal would arrive BEFORE
        the position it reversed.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            ["支持方案A", "成本是B的三倍", "转向方案B", _ANCHOR_LINE],
            provider=WindowProvider(positions=1),
        )

        seqs = sorted(_window_texts(provider))
        assert seqs == [1, 2, 3, 4]
        # Re-read the prompt body in WIRE ORDER (not sorted) and confirm it
        # ascends — i.e. the model reads the conversation forwards.
        body = provider.calls[-1].split("<discussion_window>")[1]
        wire_order = [int(m) for m in re.findall(r"^\[(\d+)\]", body, re.MULTILINE)]
        assert wire_order == sorted(wire_order), wire_order


# ==========================================================================
# 6. evidence guard
# ==========================================================================


class TestEvidenceGuard:
    def test_fabricated_evidence_cannot_survive(self, client, monkeypatch):
        """A quote with no basis in the transcript must never reach the client.

        The provider is hostile on purpose: it returns one legitimate citation,
        one quote invented out of nothing, and one citation to a segment outside
        the window. Only the legitimate one may survive.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            ["我们选择方案A。", "同意。", _ANCHOR_LINE],
            provider=WindowProvider(positions=1, extra={
                "evidence": [
                    {"segment_seq": 1, "quote": "我们选择方案A。",
                     "supports": "决策依据"},
                    {"segment_seq": 2, "quote": "这句话转写里根本没有出现过",
                     "supports": "编造的依据"},
                    {"segment_seq": 999, "quote": "任意内容",
                     "supports": "窗口外的片段"},
                ],
            }),
        )

        content = report["report"]
        assert [e["segment_seq"] for e in content["evidence"]] == [1]
        assert content["evidence"][0]["quote"] == "我们选择方案A。"
        # The removal is disclosed, not silently swallowed.
        assert "剔除" in content["uncertainty"]
        assert_report_is_grounded(report, _window_texts(provider))

    def test_all_fabricated_evidence_yields_empty_not_a_failure(
        self, client, monkeypatch
    ):
        """Dropping every citation degrades the report; it does not fail it.

        This is the current documented behaviour (ADR-002 §5): evidence is
        PRUNED and the pruning is reported, because the report's substantive
        claims may still be sound. A failure here would be the wrong call — the
        user would lose a usable report over a citation slip.

        The distinction that matters and is pinned below: an UNUSABLE report
        (no material, unparseable output) is FAILED; a usable report with bad
        citations is COMPLETED with disclosed degradation.
        """
        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            ["我们选择方案A。", "同意。", _ANCHOR_LINE],
            provider=WindowProvider(positions=1, extra={
                "evidence": [
                    {"segment_seq": 1, "quote": "完全不存在的引用",
                     "supports": "编造1"},
                    {"segment_seq": 999, "quote": "编造2", "supports": "编造2"},
                ],
            }),
        )

        assert report["status"] == "COMPLETED"
        assert report["report"]["evidence"] == []
        assert "剔除" in report["report"]["uncertainty"]

    def test_grounding_holds_for_the_real_default_provider(
        self, client, monkeypatch
    ):
        """The guard is exercised on a provider NOT wired by this test.

        Every other case here uses a `WindowProvider` the test authored. This one
        swaps in the application's own `FakeProvider` (the `LLM_PROVIDER=fake`
        path) so the pipeline is driven by a producer this file does not control
        — which is the closest thing to an independent input available offline.
        """
        from app.agent.llm.fake import FakeProvider

        _, report, provider = _run_scenario(
            client,
            monkeypatch,
            ["我们选择方案A。", "同意。", _ANCHOR_LINE],
            provider=FakeProvider(),
        )

        # The fake emits its own captain-shaped payload; whatever it says, it
        # must still satisfy the grounding contract end to end.
        assert report["status"] == "COMPLETED", report
        assert_report_is_grounded(report, _window_texts(provider))

    def test_unusable_window_fails_rather_than_fabricating(
        self, client, monkeypatch
    ):
        """With too little material the referee refuses — it does not invent.

        Uses the minimum-material boundary directly: a single discussion line
        cannot meet `captain_min_segments`, so the pipeline fails BEFORE
        spending an LLM call. This is the FAILED half of the behaviour
        distinguished above.
        """
        meeting_id = _make_meeting(client)
        _post_final(client, meeting_id, 1, "只有一句话")

        provider = WindowProvider()
        _patch_provider(monkeypatch, provider)
        body = _trigger(client, meeting_id, anchor=1).json()
        assert captain_worker.drain(timeout=10.0)

        report = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()
        assert report["status"] == "FAILED"
        assert report["report"] is None
        # Refused BEFORE the provider was ever called.
        assert provider.calls == []
