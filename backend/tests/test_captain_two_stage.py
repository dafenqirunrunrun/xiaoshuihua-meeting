"""Two-stage referee assessment: Quick then Deep (Task 4).

What this file pins, and why each one is a real failure mode rather than a
restatement of the implementation:

  1. ONE trigger produces BOTH stage outputs, and the flat fields the UI and the
     evidence validator read are DERIVED from the Deep pass — so the two views
     of one report cannot disagree.
  2. A Quick FAILURE degrades to "no quick result" and the report still
     completes. The tempting design (one try/except around both calls) makes a
     timed-out fast pass cost the user the whole report, which inverts the point
     of having a fast pass at all.
  3. The Quick call is bounded by its OWN token budget and timeout, and is not
     affected by (and does not affect) the Deep budget.
  4. The Quick call carries NO external evidence and no search runs before it —
     Quick must never be blocked on the network.
  5. Deep declares its reversal of Quick in `supersedes`; the pipeline records
     it, and an unexplained reversal is visible rather than silent.
  6. `dispute_points` is NOT manufactured from a single claim.

The provider used here is a scripted stub that dispatches on WHICH prompt it
was handed (the Quick prompt declares "快速判断", the Deep one "深入核对"), so the
test also proves the two calls are actually made with different instructions
rather than the same prompt twice.
"""

from __future__ import annotations

import json

import pytest

from app.captain import service as captain_service
from app.captain.worker import captain_worker
from app.config import settings

from test_captain import (  # noqa: E402  - sibling test module, same directory
    _make_meeting,
    _post_final,
    _trigger,
)


class StagedProvider:
    """Scripted provider that answers each stage with a stage-shaped payload.

    Dispatches on the prompt text because that is the only thing a provider can
    see — which means the test cannot pass by accident if the pipeline ever
    sends the same prompt twice.
    """

    name = "staged-provider"

    def __init__(
        self,
        *,
        quick_raw: str | None = None,
        deep_raw: str | None = None,
        quick_error: str | None = None,
        deep_error: str | None = None,
        claims: int = 2,
        supersedes: str = "",
        timeouts: list[float | None] | None = None,
    ) -> None:
        self.quick_raw = quick_raw
        self.deep_raw = deep_raw
        self.quick_error = quick_error
        self.deep_error = deep_error
        self.claims = claims
        self.supersedes = supersedes
        self.timeouts = timeouts if timeouts is not None else []
        self.quick_prompts: list[str] = []
        self.deep_prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        from app.agent.llm.base import SummaryGenerationError

        self.timeouts.append(timeout_seconds)
        if "快速判断" in prompt:
            self.quick_prompts.append(prompt)
            if self.quick_error:
                raise SummaryGenerationError(self.quick_error)
            if self.quick_raw is not None:
                return self.quick_raw
            return json.dumps(
                {
                    "issue": "是否按计划上线",
                    "key_evidence": [
                        {
                            "segment_seq": 2,
                            "quote": "讨论内容",
                            "supports": "压测未完成",
                        }
                    ],
                    "provisional_judgement": "争议在验证标准而非时间",
                    "question_to_resolve": "上线前需要达到什么压力验证标准",
                },
                ensure_ascii=False,
            )

        self.deep_prompts.append(prompt)
        if self.deep_error:
            raise SummaryGenerationError(self.deep_error)
        if self.deep_raw is not None:
            return self.deep_raw
        return json.dumps(
            {
                "discussion_summary": "讨论围绕上线时机展开。",
                "agreement_points": [
                    {"text": "功能测试已通过", "source_segment_seqs": [1]}
                ],                "uncertainty": "窗口较短，压测标准未在讨论中出现。",
                "deep": {
                    "issue": "上线前需要达到什么验证标准",
                    "claims": [
                        {
                            "label": f"主张{i + 1}",
                            "statement": f"第{i + 1}种主张。",
                            "source_segment_seqs": [i + 1],
                        }
                        for i in range(self.claims)
                    ],
                    "evidence": [
                        {
                            "segment_seq": 1,
                            "quote": "讨论内容",
                            "supports": "主张1",
                        }
                    ],
                    "counter_evidence": [],
                    "confirmed_facts": [
                        {"text": "功能测试已通过", "source_segment_seqs": [1]}
                    ],
                    "assumptions": [],
                    "missing_evidence": [
                        {"text": "压测通过标准", "source_segment_seqs": []}
                    ],
                    "evidence_strength": [
                        {
                            "claim": "主张1",
                            "level": "中",
                            "basis": "有直接转写依据，但缺少量化标准",
                            "source_segment_seqs": [1],
                        }
                    ],
                    "judgement": "争议真正落在验证标准上。",
                    "recommendation": "请明确压测通过标准。",
                    "citations": [],
                    "supersedes": self.supersedes,
                },
            },
            ensure_ascii=False,
        )


def _seed(client) -> str:
    meeting_id = _make_meeting(client)
    for i in range(1, 4):
        _post_final(client, meeting_id, i, f"第{i}句讨论内容")
    return meeting_id


@pytest.fixture
def two_stage(monkeypatch):
    monkeypatch.setattr(settings, "captain_two_stage_enabled", True)
    yield


def _run_with(client, monkeypatch, provider) -> tuple[dict, str]:
    """Trigger, drain, and return (report, report_id)."""
    from test_captain import _patch_provider

    _patch_provider(monkeypatch, provider)
    meeting_id = _seed(client)
    body = _trigger(client, meeting_id).json()
    assert captain_worker.drain(timeout=15.0)
    got = client.get(
        f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
    ).json()
    return got, body["report_id"]


# --- the happy path --------------------------------------------------------

class TestBothStagesRun:
    def test_one_trigger_yields_both_assessments(self, client, monkeypatch, two_stage):
        provider = StagedProvider()
        got, _ = _run_with(client, monkeypatch, provider)

        assert got["status"] == "COMPLETED"
        report = got["report"]
        assert report["quick"]["issue"] == "是否按计划上线"
        assert report["deep"]["judgement"] == "争议真正落在验证标准上。"

    def test_the_two_calls_get_different_prompts(self, client, monkeypatch, two_stage):
        """Guards against the pipeline sending one prompt twice."""
        provider = StagedProvider()
        _run_with(client, monkeypatch, provider)

        assert len(provider.quick_prompts) == 1
        assert len(provider.deep_prompts) == 1
        assert "快速判断" in provider.quick_prompts[0]
        assert "深入核对" in provider.deep_prompts[0]
        assert "快速判断" not in provider.deep_prompts[0]

    def test_flat_fields_are_derived_from_deep(self, client, monkeypatch, two_stage):
        """`positions` / `evidence` / `suggestion` must mirror the Deep output.

        The evidence validator and `CaptainPanel` read the FLAT fields; a report
        whose flat view disagreed with its Deep view would show one thing and
        validate another.

        Every quoted segment must also survive validation — the e2e path runs
        `evidence.py`, so a scripted quote that is not a verbatim substring of
        the cited segment is dropped, which would make this test pass for the
        wrong reason if the quote were invented.
        """
        provider = StagedProvider(claims=3)
        got, _ = _run_with(client, monkeypatch, provider)
        report = got["report"]

        assert [p["label"] for p in report["positions"]] == [
            "主张1",
            "主张2",
            "主张3",
        ]
        assert report["suggestion"] == "请明确压测通过标准。"
        assert len(report["evidence"]) == 1
        assert report["evidence"][0]["segment_seq"] == 1
        assert report["evidence"][0]["quote"] == "讨论内容"

    def test_missing_evidence_is_surfaced_in_uncertainty(
        self, client, monkeypatch, two_stage
    ):
        got, _ = _run_with(client, monkeypatch, StagedProvider())
        assert "尚缺证据" in got["report"]["uncertainty"]
        assert "压测通过标准" in got["report"]["uncertainty"]


# --- degradation -----------------------------------------------------------

class TestQuickDegradation:
    """A failed fast pass must cost the user nothing."""

    def test_quick_error_still_completes(self, client, monkeypatch, two_stage):
        provider = StagedProvider(quick_error="gateway 502")
        got, _ = _run_with(client, monkeypatch, provider)

        assert got["status"] == "COMPLETED"
        assert got["report"]["quick"] is None
        assert got["report"]["deep"] is not None

    def test_quick_bad_json_still_completes(self, client, monkeypatch, two_stage):
        provider = StagedProvider(quick_raw="{not json at all")
        got, _ = _run_with(client, monkeypatch, provider)

        assert got["status"] == "COMPLETED"
        assert got["report"]["quick"] is None

    def test_deep_prompt_omits_the_provisional_block_when_quick_failed(
        self, client, monkeypatch, two_stage
    ):
        """The Deep pass must not be told about a Quick result that never was."""
        provider = StagedProvider(quick_error="down")
        _run_with(client, monkeypatch, provider)

        assert "<provisional_assessment>" not in provider.deep_prompts[0]
        assert "无声推翻" not in provider.deep_prompts[0]

    def test_deep_failure_still_fails_the_report(self, client, monkeypatch, two_stage):
        """The opposite direction is NOT symmetric, deliberately.

        Quick alone is a directional opinion. Storing it as the finished report
        would look complete while being exactly the half-report the schema and
        evidence validator exist to prevent.
        """
        provider = StagedProvider(deep_error="gateway 502")
        got, _ = _run_with(client, monkeypatch, provider)

        assert got["status"] == "FAILED"
        assert got["report"] is None


# --- budgets and the network ----------------------------------------------

class TestBudgets:
    def test_quick_call_is_bounded_by_its_own_timeout(self, client, monkeypatch, two_stage):
        """The tighter Quick budget must actually REACH the provider call.

        Asserted against a RESOLVED provider rather than the injected stub,
        because the distinction is the whole point of `_quick_provider`:

          * an injected provider stays exactly itself (`llm_provider == "fake"`
            in tests), so the offline suite never depends on the network;
          * a resolved provider is rebuilt with the Quick output budget, which
            is what bounds the fast pass.

        The budget constant being correct while the call site still passes the
        report budget is the silent regression this catches. Note the `_run_with`
        helper is NOT used here: it installs its own factory stub, which would
        overwrite the recorder this test needs.
        """
        import app.agent.llm.factory as factory

        seen_timeouts: list[float | None] = []
        seen_budgets: list[int | None] = []
        inner = StagedProvider()

        class RecordingProvider:
            name = "recording"

            def complete(self, prompt, *, timeout_seconds=None):
                seen_timeouts.append(timeout_seconds)
                return inner.complete(prompt, timeout_seconds=timeout_seconds)

        def fake_get_provider(*args, **kwargs):
            seen_budgets.append(kwargs.get("max_output_tokens"))
            return RecordingProvider()

        monkeypatch.setattr(factory, "get_provider", fake_get_provider)
        # Pretend the deployment is a real one for the duration of the run, so
        # `_quick_provider` takes the resolve-a-second-time branch.
        monkeypatch.setattr(settings, "llm_provider", "deepseek")

        meeting_id = _seed(client)
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=15.0)

        assert settings.captain_quick_max_output_tokens in seen_budgets, (
            "the Quick pass must resolve a provider with the smaller budget"
        )
        assert settings.captain_max_output_tokens in seen_budgets, (
            "the Deep pass keeps the report budget"
        )
        assert settings.captain_quick_timeout_seconds in seen_timeouts
        assert settings.captain_llm_timeout_seconds in seen_timeouts

    def test_injected_provider_keeps_the_report_budget(self, client, monkeypatch, two_stage):
        """The offline suite must not silently start calling a real provider.

        `_quick_provider` returns an injected provider unchanged, so the only
        difference between the two stages here is the per-call timeout — the
        output budget stays whatever the injecting caller chose.
        """
        provider = StagedProvider()
        _run_with(client, monkeypatch, provider)

        assert provider.timeouts == [
            settings.captain_quick_timeout_seconds,
            settings.captain_llm_timeout_seconds,
        ], "Quick then Deep, each with its own bound"

    def test_quick_output_budget_is_smaller_than_the_report_budget(self):
        assert (
            settings.captain_quick_max_output_tokens
            < settings.captain_max_output_tokens
        )
        assert (
            settings.captain_quick_timeout_seconds
            < settings.captain_llm_timeout_seconds
        )

    def test_quick_prompt_carries_no_external_evidence(
        self, client, monkeypatch, two_stage
    ):
        """Quick must never be blocked on the network.

        The search layer may already have run for Deep's benefit; the Quick
        prompt must still contain no web block and no web rules, because a
        research call in front of a 1-2 second target is unbounded.
        """
        provider = StagedProvider()
        _run_with(client, monkeypatch, provider)

        quick = provider.quick_prompts[0]
        assert "<external_evidence>" not in quick
        assert "web_" not in quick


# --- reversal disclosure ---------------------------------------------------

class TestReversalIsDisclosed:
    def test_supersedes_is_preserved(self, client, monkeypatch, two_stage):
        provider = StagedProvider(
            supersedes="核对到连接池参数已修改，调整第一版判断"
        )
        got, _ = _run_with(client, monkeypatch, provider)
        assert "连接池参数已修改" in got["report"]["deep"]["supersedes"]

    def test_empty_supersedes_means_no_reversal(self, client, monkeypatch, two_stage):
        got, _ = _run_with(client, monkeypatch, StagedProvider())
        assert got["report"]["deep"]["supersedes"] == ""

    def test_deep_prompt_asks_for_the_disclosure_when_quick_exists(
        self, client, monkeypatch, two_stage
    ):
        """The rule must be in the prompt the model actually receives."""
        provider = StagedProvider()
        _run_with(client, monkeypatch, provider)

        deep = provider.deep_prompts[0]
        assert "无声推翻" in deep
        assert "<provisional_assessment>" in deep
        assert "争议在验证标准而非时间" in deep


# --- no manufactured conflict ---------------------------------------------

class TestDisputePoints:
    def test_single_claim_does_not_become_a_dispute(self, client, monkeypatch, two_stage):
        """ADR-002 禁止事项 §7, re-established for the derived path.

        `dispute_points` now has a second possible source (the Deep claims), so
        the structural guarantee has to be re-asserted on that path.
        """
        got, _ = _run_with(client, monkeypatch, StagedProvider(claims=1))
        report = got["report"]
        assert len(report["positions"]) == 1
        assert report["dispute_points"] == []

    def test_multiple_claims_become_dispute_points(self, client, monkeypatch, two_stage):
        got, _ = _run_with(client, monkeypatch, StagedProvider(claims=2))
        assert len(got["report"]["dispute_points"]) == 2


# --- off by default --------------------------------------------------------

class TestSingleCallPathUnchanged:
    def test_flag_off_still_uses_one_call(self, client, monkeypatch):
        """Every existing offline test and A/B search arm depends on this.

        The default must stay OFF: the legacy single-call path has a different
        output contract (`CaptainReportContent`), and flipping the default
        silently changes the behaviour the whole suite — and the Phase 3-2E
        search A/B — was written against.
        """
        assert settings.captain_two_stage_enabled is False

        seen: list[str] = []

        class CountingProvider:
            name = "counting"

            def complete(self, prompt, *, timeout_seconds=None):
                seen.append(prompt)
                return json.dumps(
                    {
                        "discussion_summary": "讨论集中在方案取舍上。",
                        "positions": [
                            {
                                "label": "主张1",
                                "statement": "第一种主张。",
                                "source_segment_seqs": [1],
                            }
                        ],
                        "agreement_points": [],
                        "dispute_points": [],
                        "evidence": [],
                        "uncertainty": "窗口较短。",
                        "suggestion": "建议补充信息。",
                    },
                    ensure_ascii=False,
                )

        got, _ = _run_with(client, monkeypatch, CountingProvider())

        assert got["status"] == "COMPLETED"
        assert len(seen) == 1, "the single-call path must make exactly one call"
        assert got["report"]["quick"] is None
        assert got["report"]["deep"] is None
