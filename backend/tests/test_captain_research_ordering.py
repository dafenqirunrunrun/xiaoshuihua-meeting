"""Phase C.5 §5: research must not be on Quick's critical path.

The bug this file exists to prevent
-----------------------------------
`run_analysis` computed the search enrichment ONCE, before the branch that
chooses between the single-pass and two-stage call chains, and handed the result
to both. On the two-stage path that placed an unbounded web request BETWEEN the
trigger and Quick — so Quick's 1-2 second target was really "1-2 seconds plus
however long the network takes", and no search timeout would have been tight
enough to fix an ordering bug.

The fix is structural (research became a thunk called after `on_quick`), which
means it can only be pinned structurally: the assertions below are about the
ORDER OF CALLS, observed from inside the fakes, because every result-level
assertion would have passed on the broken version too. The old code produced the
same final report; it just produced it late.

Why this is separate from `test_captain_quick_delivery.py`
----------------------------------------------------------
That file pins WHEN THE EVENT REACHES THE CLIENT. This one pins WHEN THE SEARCH
RUNS. They are different properties and they fail independently — a report can
publish Quick early and still have searched first.
"""

from __future__ import annotations

import pytest

from app.config import settings

from test_captain import _make_meeting, _patch_provider, _post_final  # noqa: E402
from test_captain_two_stage import StagedProvider  # noqa: E402


@pytest.fixture
def two_stage(monkeypatch):
    monkeypatch.setattr(settings, "captain_two_stage_enabled", True)
    yield


@pytest.fixture
def external_search(monkeypatch):
    """Turn the external-search feature ON for this test only.

    It is off in the test environment and has to be ON for this file to mean
    anything: with it off, `enrich` returns immediately and every ordering would
    look correct.
    """
    monkeypatch.setattr(settings, "captain_external_search_enabled", True)
    yield


# A window that the policy will actually want to search. The transcript must
# carry a checkable claim dispute AND avoid the internal-topic markers, or
# `decide` returns NO_SEARCH and the ordering assertions become vacuous.
_DISPUTE_LINES = (
    "Redis 默认端口是 6379 对吧",
    "不对，我记得默认端口是 6380",
    "文档里写的是不是 6379 我也记不清了",
)


def _seed_searchable(client) -> str:
    meeting_id = _make_meeting(client)
    for i, text in enumerate(_DISPUTE_LINES, start=1):
        _post_final(client, meeting_id, i, text)
    return meeting_id


def _fake_search(**kwargs):
    from app.captain.search.fake import FakeSearchProvider

    return FakeSearchProvider(generate=True, result_count=3, **kwargs)


def _inject_search(monkeypatch, search) -> None:
    """Make the pipeline use `search` as this analysis's provider.

    Patches the resolution seam, NOT the factory and NOT `run_analysis`. Both of
    those substitutes would stop exercising the code under test: swapping
    `run_analysis` skips the very call chain whose ordering this file exists to
    pin.
    """
    from app.captain import service as captain_service

    monkeypatch.setattr(
        captain_service, "_resolve_search_provider", lambda *a, **k: search
    )


def _run_simple(client, monkeypatch, llm, search):
    """Trigger, drain, return the report body. No timeline instrumentation."""
    from app.captain.worker import captain_worker

    _patch_provider(monkeypatch, llm)
    _inject_search(monkeypatch, search)
    meeting_id = _seed_searchable(client)
    body = _trigger(client, meeting_id).json()
    assert captain_worker.drain(timeout=20.0)
    return client.get(
        f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
    ).json()


def _run_with(client, monkeypatch, llm, search):
    """One analysis with BOTH providers instrumented into a shared timeline.

    Returns `(timeline, report_body)`. Both providers write into the SAME list,
    so the assertion is on their relative order rather than on two separate
    observations that would each pass under a broken interleaving.

    `llm` is subclassed rather than replaced: a test that wants to inspect the
    prompt a stage received gets it through `llm`'s own `complete`, and this
    wrapper only records WHICH stage the call was for. Replacing it would make
    the caller's own recording silently dead code.
    """
    timeline: list[str] = []

    class TimelineLLM(type(llm)):
        def complete(self, prompt, *, timeout_seconds=None):
            timeline.append("deep" if "深入核对" in prompt else "quick")
            return llm.complete(prompt, timeout_seconds=timeout_seconds)

    class TimelineSearch:
        name = "timeline"

        def search(self, query, *, max_results, timeout_ms):
            timeline.append("search")
            return search.search(query, max_results=max_results, timeout_ms=timeout_ms)

    from app.captain import service as captain_service
    from app.captain.worker import captain_worker

    original_enrich = captain_service._safe_enrich

    def enrich_spy(segments, *, provider=None):
        timeline.append("enrich")
        return original_enrich(segments, provider=provider)

    monkeypatch.setattr(captain_service, "_safe_enrich", enrich_spy)
    _inject_search(monkeypatch, TimelineSearch())
    _patch_provider(monkeypatch, TimelineLLM())

    meeting_id = _seed_searchable(client)
    body = _trigger(client, meeting_id).json()
    assert captain_worker.drain(timeout=20.0)
    got = client.get(
        f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
    ).json()
    return timeline, got


def _trigger(client, meeting_id: str):
    from test_captain import _trigger as trigger

    return trigger(client, meeting_id)


class TestQuickRunsBeforeResearch:
    """The ordering itself — the thing the old code got wrong."""

    def test_enrich_is_called_after_quick_and_before_deep(
        self, client, monkeypatch, two_stage, external_search
    ):
        timeline, got = _run_with(
            client, monkeypatch, StagedProvider(), _fake_search()
        )

        assert got["status"] == "COMPLETED", got
        assert "search" in timeline, (
            f"the policy did not decide to search, so this test proves nothing: {timeline}"
        )
        # The load-bearing assertion: `enrich` runs on the boundary between the
        # two stages, never before the first one.
        assert timeline.index("quick") < timeline.index("enrich")
        assert timeline.index("enrich") < timeline.index("deep")

    def test_quick_prompt_carries_no_external_evidence_block(
        self, client, monkeypatch, two_stage, external_search
    ):
        """Quick must not even be SHOWN sources, let alone wait for them.

        Ordering alone is not enough: a prompt assembled before the search and
        filled in afterwards would also produce the right call order while still
        handing Quick a `<external_evidence>` block. This asserts on the text the
        Quick model actually read.
        """
        seen: dict = {}

        class RecordingLLM(StagedProvider):
            def complete(self, prompt, *, timeout_seconds=None):
                if "深入核对" in prompt:
                    seen["deep"] = prompt
                else:
                    seen["quick"] = prompt
                return super().complete(prompt, timeout_seconds=timeout_seconds)

        _, got = _run_with(client, monkeypatch, RecordingLLM(), _fake_search())

        assert got["status"] == "COMPLETED"
        assert "<external_evidence>" not in seen["quick"]
        # Deep DID get sources, or this test would pass trivially by the search
        # never having produced anything at all.
        assert "<external_evidence>" in seen["deep"]


class TestResearchStatusReachesTheReport:
    """§7: the outcome must be machine-readable, not only prose."""

    def test_success_is_recorded(self, client, monkeypatch, two_stage, external_search):
        _, got = _run_with(client, monkeypatch, StagedProvider(), _fake_search())

        assert got["status"] == "COMPLETED"
        # `success` or `partial` depending on how many of the planned questions
        # the fake answered; both mean "research produced usable evidence".
        assert got["report"]["research_status"] in {"success", "partial"}
        assert got["report"]["external_evidence"]

    def test_the_sse_event_carries_the_same_research_outcome_as_the_report(
        self, client, monkeypatch, two_stage, external_search
    ):
        """The fast-path event must not contradict the durable report.

        `_publish_completed` passes `build_captain_completed` a SUBSET of the
        report, and it omitted `research_status` / `external_evidence`, so the
        event always claimed `not_required` / 0 even when research had run and
        succeeded. The report body was right the whole time — which is exactly
        why this needs its own assertion on the EVENT: a result-level test on the
        report cannot see it.

        Asserted against the report's own values, so the two cannot disagree
        without this failing.
        """
        import json as _json

        from app.realtime.event_broker import event_broker

        events: list = []
        original = event_broker.publish

        def spy(meeting_id, event):
            if event.event_type == "captain.completed":
                events.append(event)
            return original(meeting_id, event)

        monkeypatch.setattr(event_broker, "publish", spy)
        _, got = _run_with(client, monkeypatch, StagedProvider(), _fake_search())

        assert got["status"] == "COMPLETED"
        assert events, "captain.completed was never published"
        env = events[-1]
        assert env.payload["research_status"] == got["report"]["research_status"]
        assert env.payload["external_evidence_count"] == len(
            got["report"]["external_evidence"]
        )
        # And the concrete expectation for a successful fake run.
        assert env.payload["research_status"] in {"success", "partial"}
        assert env.payload["external_evidence_count"] > 0
        # The payload must not have grown a full evidence list into the event.
        assert "external_evidence" not in env.payload
        assert "web_1" not in _json.dumps(env.payload, ensure_ascii=False)

    def test_timeout_is_recorded(self, client, monkeypatch, two_stage, external_search):
        _, got = _run_with(
            client, monkeypatch, StagedProvider(), _fake_search(timed_out=True)
        )

        # §5: a blown budget costs evidence, never the report.
        assert got["status"] == "COMPLETED"
        assert got["report"]["research_status"] == "timeout"
        assert got["report"]["external_evidence"] == []

    def test_provider_error_is_recorded(
        self, client, monkeypatch, two_stage, external_search
    ):
        _, got = _run_with(
            client,
            monkeypatch,
            StagedProvider(),
            _fake_search(error_code="http_500"),
        )

        assert got["status"] == "COMPLETED"
        assert got["report"]["research_status"] == "failed"
        assert got["report"]["external_evidence"] == []

    def test_a_raising_provider_still_completes(
        self, client, monkeypatch, two_stage, external_search
    ):
        """The one failure `_safe_enrich` exists for: a provider that throws.

        The Protocol says implementations return failures rather than raising,
        so a guard is only worth having if it also covers an implementation that
        does not obey — which is a third-party provider written later, not this
        repository's own code.
        """

        class ExplodingSearch:
            name = "exploding"

            def search(self, query, *, max_results, timeout_ms):
                raise RuntimeError("search backend on fire")

        got = _run_simple(client, monkeypatch, StagedProvider(), ExplodingSearch())

        assert got["status"] == "COMPLETED"
        assert got["report"]["external_evidence"] == []
        # The guard's fallback carries the dataclass default, so the report says
        # `not_required` — the search never produced a verdict because it never
        # returned one. Asserted as a member set rather than an exact value: the
        # property under test is "did not fail the report", and the status here
        # is the guard's own default rather than a classification.
        assert got["report"]["research_status"] in {"not_required", "failed"}

    def test_no_search_needed_is_not_a_degradation(
        self, client, monkeypatch, two_stage, external_search
    ):
        """The common case must not wear a failure label.

        A window with no checkable factual dispute produces a NO_SEARCH verdict,
        and that is the CORRECT outcome for most meeting content. If it were
        reported as `failed`, every ordinary report would read as a degradation
        and the field would stop meaning anything.
        """
        from test_captain import _make_meeting as make_meeting

        meeting_id = make_meeting(client)
        _post_final(client, meeting_id, 1, "我们先对齐一下今天的议程")
        _post_final(client, meeting_id, 2, "我觉得应该先把功能做完整")
        _post_final(client, meeting_id, 3, "同意，那就按这个优先级来")

        timeline: list[str] = []

        class TimelineLLM(StagedProvider):
            def complete(self, prompt, *, timeout_seconds=None):
                timeline.append("deep" if "深入核对" in prompt else "quick")
                return super().complete(prompt, timeout_seconds=timeout_seconds)

        from app.captain import service as captain_service
        from app.captain.worker import captain_worker

        _patch_provider(monkeypatch, TimelineLLM())
        _inject_search(monkeypatch, _fake_search())
        body = _trigger(client, meeting_id).json()
        assert captain_worker.drain(timeout=20.0)
        got = client.get(
            f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
        ).json()

        assert got["status"] == "COMPLETED"
        assert "search" not in timeline, timeline
        assert got["report"]["research_status"] == "not_required"
        assert got["report"]["external_evidence"] == []

    def test_the_status_is_a_member_of_the_declared_vocabulary(
        self, client, monkeypatch, two_stage, external_search
    ):
        from app.captain.schemas import RESEARCH_STATUSES

        _, got = _run_with(client, monkeypatch, StagedProvider(), _fake_search())

        assert got["report"]["research_status"] in RESEARCH_STATUSES


__all__: list[str] = []
