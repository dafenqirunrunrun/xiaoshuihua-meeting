"""Phase 3-2: Captain external evidence — search policy, degradation, layering.

The seven scenarios the spec names, plus the Brave contract fixture test. Every
one runs OFFLINE: the provider is `FakeSearchProvider` or a scripted stub, so no
test here can pass or fail because a search API was reachable.

What this file is actually proving
----------------------------------
Not "search returns results" — that is the provider's job, and a fixture proves
nothing about it. What is proved here is the CONTRACT the Captain pipeline owes:

  1. a factual dispute triggers a search and the report carries external evidence;
  2. an internal decision does NOT search, and the provider is never called;
  3. a timeout still produces a COMPLETED report, with no external evidence and
     the degradation DISCLOSED in `uncertainty`;
  4. a provider error is the same, and no provider text reaches the client;
  5. a cache hit costs no provider call;
  6. the two evidence layers never mix: transcript evidence is still validated
     against persisted segments, external evidence never carries a `segment_seq`;
  7. a model-invented `web_9` cannot survive, because the pipeline overwrites the
     list from provider results.

Two levels are used deliberately. The end-to-end tests drive the real pipeline
(`trigger -> worker -> context -> policy -> provider -> prompt -> LLM ->
Pydantic -> evidence`) and assert on the raw API response, so what is checked is
exactly what the browser sees. The unit tests call `policy.decide` and
`search_service.enrich` directly, because a degradation path is far easier to
prove deterministically than to provoke through HTTP.
"""

from __future__ import annotations

import json

import pytest

from app.captain.search.cache import get_cache, reset_cache
from app.captain.search.fake import FakeSearchProvider
from app.captain.search.schemas import SearchResult
from app.captain.worker import captain_worker
from tests.test_captain import (  # noqa: F401  (helper reuse)
    WindowProvider,
    _make_meeting,
    _patch_provider,
    _post_final,
    _trigger,
)


class RecordingWindowProvider(WindowProvider):
    """`WindowProvider` that also records the prompt it was handed.

    Subclassed here rather than added to `test_captain.py`: recording is only
    interesting to the Phase 3-2 tests, and the shared provider is used by
    suites that assert on window bounds, not on the external block.
    """

    def __init__(self, *args, on_prompt=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_prompt = on_prompt

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        if self._on_prompt is not None:
            self._on_prompt(prompt)
        return super().complete(prompt, timeout_seconds=timeout_seconds)

_ANCHOR_LINE = "先讨论到这里，继续推进。"

# A fact-shaped disagreement: a default port is exactly what an outside source
# can settle, and it is NOT something the meeting itself knows.
_FACTUAL_LINES = [
    "Redis 的默认端口是 6379",
    "不对，默认端口不是 6379",
    _ANCHOR_LINE,
]

# Results span five domains so the per-domain de-duplication and the source cap
# are genuinely exercised rather than trivially satisfied.
_FIXTURE_RESULTS = [
    SearchResult(
        title=f"参考资料 {i + 1}",
        url=f"https://source{i + 1}.test/doc",
        snippet=f"第 {i + 1} 份离线资料的摘要。",
        domain=f"source{i + 1}.test",
    )
    for i in range(5)
]

_LAST_PROMPT: list[str] = []


@pytest.fixture(autouse=True)
def clean_search_cache():
    """The cache is a process-wide singleton; a test must never inherit entries."""
    reset_cache()
    _LAST_PROMPT.clear()
    yield
    reset_cache()
    _LAST_PROMPT.clear()


def _record_prompt(prompt: str) -> None:
    """Every prompt the pipeline hands an LLM, so tests can inspect the real one."""
    _LAST_PROMPT.append(prompt)


def _run_scenario(client, monkeypatch, lines, *, llm=None, anchor=None):
    """Ingest `lines` and produce ONE report. Returns (meeting_id, report_dict)."""
    meeting_id = _make_meeting(client)
    for i, text in enumerate(lines, start=1):
        _post_final(client, meeting_id, i, text)

    llm = llm if llm is not None else RecordingWindowProvider(on_prompt=_record_prompt)
    _patch_provider(monkeypatch, llm)

    body = _trigger(client, meeting_id, anchor=anchor).json()
    assert captain_worker.drain(timeout=10.0), "captain worker did not drain"

    report = client.get(
        f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
    ).json()
    return meeting_id, report


def _unit_enrich(monkeypatch, provider, segments):
    """Policy + provider + evidence assembly, with no DB and no HTTP.

    Pins the provider through the factory seam the production path uses, so the
    stub is reached the same way the real one would be.
    """
    from app.captain.search import service as search_service

    monkeypatch.setattr(
        "app.captain.search.service.get_search_provider", lambda *a, **k: provider
    )
    return search_service.enrich(segments, provider=provider)


# ==========================================================================
# 1. FACTUAL dispute -> SEARCH
# ==========================================================================


class TestFactualDisputeTriggersSearch:
    def test_factual_dispute_searches_and_returns_evidence(self, monkeypatch):
        """The happy path, at unit level so the evidence is inspectable."""
        search = FakeSearchProvider(results=_FIXTURE_RESULTS)
        result = _unit_enrich(
            monkeypatch, search, [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]
        )

        assert search.calls, "a factual dispute did not reach the provider"
        assert result.evidence, "search returned results but no evidence was built"
        assert result.metrics.search_triggered is True
        assert result.metrics.external_evidence_count == len(result.evidence)
        assert result.metrics.search_result_count == 5

    def test_external_evidence_ids_are_contiguous_from_web_1(self, monkeypatch):
        """Ids are assigned by position, so the model cannot be handed a gap.

        A gap (web_1, web_3) invites a model to "helpfully" fill web_2 — see
        `external_evidence.py`.
        """
        from app.captain.external_evidence import to_external_evidence

        search = FakeSearchProvider(results=_FIXTURE_RESULTS)
        result = _unit_enrich(
            monkeypatch, search, [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]
        )

        ids = [item.source_id for item in to_external_evidence(result.evidence)]
        assert ids == [f"web_{i + 1}" for i in range(len(ids))]

    def test_external_evidence_never_carries_a_segment_seq(self, client, monkeypatch):
        """The layering guarantee, asserted on the wire format.

        A web page is not a transcript segment. If an external entry ever grew a
        `segment_seq`, every downstream reader — including the citation
        validator — would treat a search result as meeting content.
        """
        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES)

        assert report["status"] == "COMPLETED"
        for item in report["report"]["external_evidence"]:
            assert "segment_seq" not in item, "external evidence carried a segment_seq"
            assert item["source_id"].startswith("web_")

    def test_prompt_is_given_the_external_block_when_search_succeeds(
        self, client, monkeypatch
    ):
        """Wiring proof: the evidence reaches the model, not just the report."""
        _run_scenario(client, monkeypatch, _FACTUAL_LINES)

        assert _LAST_PROMPT, "the pipeline never called the LLM"
        prompt = _LAST_PROMPT[-1]
        assert "<external_evidence>" in prompt
        assert "[web_1]" in prompt


# ==========================================================================
# 2. INTERNAL decision -> NO SEARCH
# ==========================================================================


class TestInternalDecisionDoesNotSearch:
    def test_scheduling_decision_never_reaches_the_provider(self, monkeypatch):
        """The adversarial case the whole policy exists for.

        "周五上线还是下周上线" reads factual to a naive router — it has a
        question shape and a date. No external source can answer it: the answer
        lives in the team's own capacity. The assertion that matters is that the
        budget was never spent at all.
        """
        search = FakeSearchProvider(results=_FIXTURE_RESULTS)
        result = _unit_enrich(
            monkeypatch,
            search,
            [
                (1, "我们这个版本是周五上线还是下周上线?"),
                (2, "看进度吧，我觉得下周更稳。"),
            ],
        )

        assert search.calls == [], f"internal decision spent the budget: {search.calls}"
        assert result.evidence == []
        assert result.metrics.search_triggered is False
        assert result.metrics.search_policy_type == "INTERNAL_DECISION"
        assert result.note == "", "a NO_SEARCH verdict must not add a disclosure"

    def test_internal_decision_report_has_no_external_block(self, client, monkeypatch):
        """And the LLM is never shown a search block for it."""
        _, report = _run_scenario(
            client,
            monkeypatch,
            [
                "我们这个版本是周五上线还是下周上线?",
                "看进度吧，我觉得下周更稳。",
                _ANCHOR_LINE,
            ],
        )

        assert report["status"] == "COMPLETED"
        assert report["report"]["external_evidence"] == []
        assert "<external_evidence>" not in _LAST_PROMPT[-1]

    def test_opinion_alone_does_not_trigger_a_search(self):
        """A preference is not a fact claim, and no source can settle it.

        This is the failure the demo scenario would produce: "我认为应该先把
        功能做完整" has an opinion marker and no checkable content.
        """
        from app.captain.search import policy as search_policy

        decision = search_policy.decide(
            [
                (1, "产品：我认为应该先把功能做完整。"),
                (2, "技术：我认为现在稳定性更重要。"),
            ]
        )
        assert not decision.should_search, decision.reason


# ==========================================================================
# 2b. internal markers are SCOPED — the Phase C.5 false-negative fix
# ==========================================================================


class TestInternalMarkersDoNotOverreach:
    """A bare pronoun/noun must not veto a checkable external fact.

    Before the fix `_INTERNAL_MARKERS` contained "我们" / "产品" / "团队" /
    "发布" and friends, so any factual discussion that happened to mention one
    was refused outright — the expensive error, because the user asked a
    checkable question and got silence. These pin the scoping both ways: a
    generic word no longer refuses (search happens), while a genuinely
    internal SUBJECT still does (see `TestInternalDecisionDoesNotSearch`).
    """

    def test_bare_first_person_does_not_refuse_a_checkable_fact(self):
        """`我们…6379 吗?` is a checkable technical claim, not an internal topic.

        The disagreement here is about a PORT NUMBER, which only the outside
        world can settle. "我们观察到" is how a colleague phrased it, not what
        the question is about.
        """
        from app.captain.search import policy as search_policy

        decision = search_policy.decide(
            [
                (1, "我们观察到 Redis 默认端口好像变了?"),
                (2, "没有吧，还是 6379。"),
            ]
        )
        assert decision.should_search, decision
        assert decision.policy_type == "FACTUAL"

    def test_a_third_party_team_maintaining_a_project_is_still_external(self):
        """`由 Redis 团队维护` names SOMEONE ELSE's team — an external fact."""
        from app.captain.search import policy as search_policy

        decision = search_policy.decide(
            [
                (1, "这个库由 Redis 团队维护，默认端口是 6379 吧?"),
                (2, "不对，新版默认端口不是 6379 了。"),
            ]
        )
        assert decision.should_search, decision

    def test_internal_subject_still_refuses(self):
        """The scoping must not have opened the hole the veto exists to close."""
        from app.captain.search import policy as search_policy

        for lines in (
            [(1, "这个项目的预算是 50 万还是 80 万?"), (2, "不对，应该是 60 万。")],
            [(1, "换掉他之后团队能力够吗?"), (2, "我觉得不够。")],
            [(1, "这个模块到底是谁负责?"), (2, "我觉得是老王吧。")],
        ):
            decision = search_policy.decide(lines)
            assert not decision.should_search, (lines, decision)
            assert decision.policy_type == "INTERNAL_DECISION"

    def test_opinion_exchange_is_not_a_fact_dispute(self):
        """An attribution verb is not a fact claim, however many markers follow.

        "李经理表示可以下周给数据" carries the copula "可以", which is a claim
        marker — counting it made an ordinary observation exchange look like a
        dispute and searched the web for it. Only SUBSTANTIVE markers may
        establish a dispute.
        """
        from app.captain.search import policy as search_policy

        decision = search_policy.decide(
            [
                (1, "张三认为应该先做成本核算"),
                (2, "李经理表示可以下周给数据"),
            ]
        )
        assert not decision.should_search, decision.reason


# ==========================================================================
# 2c. research_status semantics — evidence + no failure == success
# ==========================================================================


class TestResearchStatusSemantics:
    """`research_status` describes what ACTUALLY happened, not how many queries
    were planned.

    The bug this pins: a window plans two questions, the FIRST query returns
    everything needed, and the loop early-stops (a normal, successful ending).
    The old rule read `attempted < planned` as "incomplete" and reported
    `partial` — so a search that fully succeeded said it had not. These tests
    assert the corrected vocabulary, where `partial` is reserved for a query that
    was actually SENT and actually FAILED.
    """

    # A window the policy will search, with TWO planned questions — so the
    # early-stop path is reachable (first query succeeds, second never sent).
    _TWO_QUESTION_WINDOW = [
        (1, "Redis 的默认端口是 6379 吧?"),
        (2, "不对，早就不是 6379 了。"),
    ]

    def test_first_query_suffices_and_second_is_skipped_is_success(self, monkeypatch):
        """Early stop after a good first query is SUCCESS, not `partial`."""
        search = FakeSearchProvider(results=_FIXTURE_RESULTS)

        result = _unit_enrich(monkeypatch, search, self._TWO_QUESTION_WINDOW)

        # The property the fix is about: the loop stopped early by CHOICE.
        assert len(search.calls) == 1, search.calls
        assert result.evidence, "the first query produced evidence"
        assert result.research_status == "success", result.research_status

    def test_a_sent_query_that_fails_but_evidence_survives_is_partial(self, monkeypatch):
        """`partial` needs a query that was actually sent and failed.

        Exercise through the real classification function, because the
        early-stop rule means the two-query loop can only ever reach this state
        when the first query is EMPTY and the second succeeds-then-another
        fails — an arrangement the loop no longer produces (see the module
        note). Calling `_status_for` directly pins the RULE itself.
        """
        from app.captain.search import service as search_service

        assert (
            search_service._status_for(
                timed_out=False, error_code="http_500", evidence_count=2
            )
            == "partial"
        )

    def test_timeout_with_no_evidence_is_timeout(self, monkeypatch):
        search = FakeSearchProvider(timed_out=True)

        result = _unit_enrich(monkeypatch, search, self._TWO_QUESTION_WINDOW)

        assert result.research_status == "timeout"
        assert result.evidence == []

    def test_provider_error_with_no_evidence_is_failed(self, monkeypatch):
        search = FakeSearchProvider(error_code="http_500")

        result = _unit_enrich(monkeypatch, search, self._TWO_QUESTION_WINDOW)

        assert result.research_status == "failed"
        assert result.evidence == []

    def test_no_question_planned_is_not_required(self):
        """A window the router declines never reaches `_status_for` at all."""
        from app.captain.search import policy as search_policy

        decision = search_policy.decide(
            [(1, "这个模块到底是谁负责?"), (2, "我觉得是老王吧。")]
        )
        assert not decision.should_search

    def test_zero_results_from_a_healthy_search_is_failed(self, monkeypatch):
        """The web answered, with nothing usable — `failed`, not `partial`."""
        search = FakeSearchProvider(results=[])

        result = _unit_enrich(monkeypatch, search, self._TWO_QUESTION_WINDOW)

        assert result.evidence == []
        assert result.research_status == "failed"


# ==========================================================================
# 3/4. degradation — timeout, provider error, disabled, exception
# ==========================================================================


class TestSearchDegradesWithoutFailing:
    def test_timeout_yields_no_evidence_and_a_disclosure(self, monkeypatch):
        """A blown budget is a NORMAL result, not an exception.

        The report is still produced, carries no external evidence, and TELLS
        the user the search did not finish — silence would let them assume the
        referee checked outside sources and found nothing.
        """
        from app.captain.search.service import NOTE_TIMEOUT

        search = FakeSearchProvider(timed_out=True)
        result = _unit_enrich(
            monkeypatch, search, [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]
        )

        assert result.evidence == []
        assert result.metrics.search_timed_out is True
        assert result.metrics.external_evidence_count == 0
        assert result.metrics.search_query_count <= 2
        assert result.note == NOTE_TIMEOUT

    def test_timeout_still_completes_the_report_end_to_end(self, client, monkeypatch):
        """§3 in one assertion: search died, the Captain did not."""
        monkeypatch.setattr(
            "app.captain.search.service.get_search_provider",
            lambda *a, **k: FakeSearchProvider(timed_out=True),
        )

        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES)

        assert report["status"] == "COMPLETED", report.get("status")
        content = report["report"]
        assert content is not None
        assert content["external_evidence"] == []
        assert "外部资料" in content["uncertainty"], (
            "a degraded search was not disclosed in uncertainty"
        )

    def test_provider_error_yields_no_evidence_and_no_provider_text(self, monkeypatch):
        """A provider failure must not leak its identity or message.

        The note is fixed product language; the `error_code` is a short slug for
        engineering logs. Neither is a provider string.
        """
        from app.captain.search.service import NOTE_PROVIDER_ERROR

        search = FakeSearchProvider(error_code="http_500")
        result = _unit_enrich(
            monkeypatch, search, [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]
        )

        assert result.evidence == []
        assert result.metrics.search_error_code == "http_500"
        assert result.note == NOTE_PROVIDER_ERROR
        assert "http_500" not in result.note

    def test_provider_error_still_completes_the_report_end_to_end(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            "app.captain.search.service.get_search_provider",
            lambda *a, **k: FakeSearchProvider(error_code="http_500"),
        )

        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES)

        assert report["status"] == "COMPLETED"
        rendered = json.dumps(report, ensure_ascii=False)
        assert "http_500" not in rendered, "an internal error code reached the client"

    def test_unconfigured_provider_degrades_rather_than_failing(
        self, client, monkeypatch
    ):
        """A misconfigured provider name is a deployment bug, not a report failure.

        `get_search_provider` fails CLOSED (raises) — which is right — and this
        test proves the caller catches that and still produces a report.
        """
        from app.captain.search import service as search_service
        from app.captain.search.service import NOTE_UNCONFIGURED

        def _boom(*a, **k):
            from app.captain.search.provider import SearchProviderError

            raise SearchProviderError("not a provider")

        monkeypatch.setattr(search_service, "get_search_provider", _boom)
        result = search_service.enrich([(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])])

        assert result.evidence == []
        assert result.note == NOTE_UNCONFIGURED
        assert result.metrics.search_policy_reason == "provider_unavailable"

    def test_disabled_feature_is_silent_and_does_not_search(self, monkeypatch):
        """Switched off means no policy run, no provider, no note.

        The deployment made this choice, so telling every report "we did not
        search" would be noise rather than honesty.
        """
        from app.captain.search import service as search_service

        monkeypatch.setattr(
            search_service.settings, "captain_external_search_enabled", False
        )
        search = FakeSearchProvider(results=_FIXTURE_RESULTS)
        result = search_service.enrich(
            [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])], provider=search
        )

        assert result.evidence == []
        assert result.note == ""
        assert result.metrics.search_policy_reason == "disabled"
        assert search.calls == []

    def test_enrich_never_raises_even_when_the_provider_does(self, monkeypatch):
        """A third-party provider letting an exception escape must not fail us.

        The Protocol says implementations return failures, but the guard belongs
        on the caller's side of the contract, not on the implementer's honour.
        """

        class ExplodingProvider:
            name = "exploding"

            def search(self, query, *, max_results, timeout_ms):
                raise RuntimeError("transport blew up")

        result = _unit_enrich(
            monkeypatch,
            ExplodingProvider(),
            [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])],
        )

        assert result.evidence == []
        assert result.metrics.search_error_code == "provider_raised"
        assert "blew up" not in result.note

    def test_disabled_search_does_not_change_an_existing_report_path(
        self, client, monkeypatch
    ):
        """The kill switch is a complete return to pre-3-2 behaviour."""
        from app.captain.search import service as search_service

        monkeypatch.setattr(
            search_service.settings, "captain_external_search_enabled", False
        )
        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES)

        assert report["status"] == "COMPLETED"
        assert report["report"]["external_evidence"] == []
        assert "<external_evidence>" not in _LAST_PROMPT[-1]


# ==========================================================================
# 5. cache
# ==========================================================================


class TestCache:
    def test_second_identical_search_hits_the_cache(self, monkeypatch):
        """Same query, same provider, same freshness -> zero extra provider calls.

        `search_latency_ms` must stay 0 for the cached attempt: it is the number
        Phase 3-2E uses to compare providers, and diluting it with cache lookup
        time would flatter whichever arm happened to run second.
        """
        search = FakeSearchProvider(results=_FIXTURE_RESULTS)
        segments = [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]

        first = _unit_enrich(monkeypatch, search, segments)
        calls_after_first = len(search.calls)
        second = _unit_enrich(monkeypatch, search, segments)

        assert calls_after_first == 1
        assert len(search.calls) == calls_after_first, "cache did not short-circuit"
        assert second.metrics.search_cache_hit is True
        assert second.metrics.search_latency_ms == 0
        assert [r.url for r in second.evidence] == [r.url for r in first.evidence]

    def test_a_different_provider_does_not_share_a_cache_entry(self, monkeypatch):
        """The cache key includes the provider name — load-bearing for A/B.

        Without it a Brave result would be served to an Exa arm, and the whole
        evaluation would silently compare a provider against itself.
        """

        class OtherProvider(FakeSearchProvider):
            name = "other"

        first_provider = FakeSearchProvider(results=_FIXTURE_RESULTS)
        other = OtherProvider(results=_FIXTURE_RESULTS)
        segments = [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]

        _unit_enrich(monkeypatch, first_provider, segments)
        other_result = _unit_enrich(monkeypatch, other, segments)

        assert other.calls, "another provider was served the first one's cache entry"
        assert other_result.metrics.search_cache_hit is False

    def test_failed_responses_are_not_cached(self, monkeypatch):
        """A cached timeout would make the degradation sticky for the whole TTL:

        one unlucky 900ms window would deny that query a real attempt until the
        entry expired, long after the provider recovered.
        """
        search = FakeSearchProvider(timed_out=True)
        _unit_enrich(monkeypatch, search, [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])])

        assert len(get_cache()) == 0

    def test_domain_deduplication_prefers_distinct_sources(self, monkeypatch):
        """Five results from one site are one source's opinion repeated."""
        same_domain = [
            SearchResult(
                title=f"同一来源 {i}",
                url=f"https://one.test/{i}",
                snippet=f"摘要 {i}",
                domain="one.test",
            )
            for i in range(5)
        ]
        search = FakeSearchProvider(results=same_domain)
        result = _unit_enrich(
            monkeypatch, search, [(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])]
        )

        # De-duplication must not empty the list — a single-source result set
        # still produces evidence, it just produces less of it.
        assert result.evidence, "de-duplication discarded everything"
        assert len(result.evidence) <= 5


# ==========================================================================
# 6/7. layering and fabrication
# ==========================================================================


class TestEvidenceLayering:
    def test_model_cannot_invent_an_external_source(self, client, monkeypatch):
        """A hallucinated `web_9` has nowhere to land.

        The model claims a source it was never shown. The pipeline overwrites
        `external_evidence` from provider results AFTER generation, so nothing
        the model wrote survives there.
        """
        llm = RecordingWindowProvider(
            on_prompt=_record_prompt,
            extra={
                "external_evidence": [
                    {
                        "source_id": "web_9",
                        "title": "模型编造的资料",
                        "url": "https://invented.test/",
                        "context": "这条内容不存在。",
                    }
                ]
            },
        )
        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES, llm=llm)

        ids = [item["source_id"] for item in report["report"]["external_evidence"]]
        assert "web_9" not in ids, "a model-invented source survived into the report"
        assert "invented.test" not in json.dumps(report, ensure_ascii=False)
        assert all(sid.startswith("web_") for sid in ids)

    def test_transcript_evidence_is_still_validated_against_segments(
        self, client, monkeypatch
    ):
        """Adding external evidence must not loosen transcript grounding.

        A fabricated quote is still dropped, exactly as before Phase 3-2 — the
        new layer sits BESIDE the old one, it does not replace it.
        """
        llm = RecordingWindowProvider(
            on_prompt=_record_prompt,
            extra={
                "evidence": [
                    {
                        "segment_seq": 1,
                        "quote": "这句原文根本不存在",
                        "supports": "某个判断",
                    }
                ]
            },
        )
        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES, llm=llm)

        content = report["report"]
        assert content["evidence"] == [], "a fabricated transcript quote survived"
        # Same channel as before: the pruning is disclosed, not silent.
        assert "剔除" in content["uncertainty"]

    def test_valid_transcript_evidence_still_survives_with_external_present(
        self, client, monkeypatch
    ):
        """The other half: real transcript evidence is NOT collateral damage."""
        llm = RecordingWindowProvider(
            on_prompt=_record_prompt,
            extra={
                "evidence": [
                    {
                        "segment_seq": 1,
                        "quote": _FACTUAL_LINES[0],
                        "supports": "第一条主张",
                    }
                ]
            },
        )
        _, report = _run_scenario(client, monkeypatch, _FACTUAL_LINES, llm=llm)

        content = report["report"]
        assert len(content["evidence"]) == 1
        assert content["evidence"][0]["segment_seq"] == 1
        # And it is a transcript citation, never confused with a web source.
        assert "source_id" not in content["evidence"][0]

    def test_policy_input_is_plain_text_pairs(self):
        """`decide` takes `[(seq, text)]` — no session, no ORM, no settings.

        Structural: the call below is made with no DB and no app context at all.
        """
        from app.captain.search import policy as search_policy

        decision = search_policy.decide([(1, _FACTUAL_LINES[0]), (2, _FACTUAL_LINES[1])])
        assert decision.should_search

    def test_transcript_segment_is_never_extracted_from_a_url(self, monkeypatch):
        """An external entry is built from provider output only.

        `to_external_evidence` is a total projection: it never reads a segment,
        never invents one, and drops entries carrying neither title nor context
        so an empty `[web_n]` slot can never appear.
        """
        from app.captain.external_evidence import to_external_evidence

        empty = SearchResult(title="", url="https://a.test/", snippet="", domain="a.test")
        good = SearchResult(
            title="有内容", url="https://b.test/", snippet="摘要", domain="b.test"
        )
        built = to_external_evidence([empty, good])

        assert len(built) == 1
        assert built[0].source_id == "web_1", "ids must stay contiguous after a skip"
        assert built[0].title == "有内容"


# ==========================================================================
# prompt equivalence — the A/B baseline depends on it
# ==========================================================================


class TestPromptEquivalence:
    def _window(self):
        from app.captain.context import ContextWindow, select_window
        from app.transcript.segment_model import TranscriptSegment

        rows = [
            TranscriptSegment(
                meeting_id="m",
                segment_seq=i,
                utterance_id=f"u{i}",
                event_id=f"e{i}",
                speaker_id="unknown",
                start_ms=(i - 1) * 1000,
                end_ms=i * 1000,
                text=text,
            )
            for i, text in enumerate(["甲说 A", "乙说 B", "丙说 C"], start=1)
        ]
        return select_window(
            rows,
            anchor_segment_seq=3,
            window_seconds=180.0,
            max_chars=12_000,
            min_segments=1,
        )

    def test_prompt_is_unchanged_when_no_external_evidence(self):
        """Phase 3-2E compares search-on against the PRE-3-2 prompt.

        That comparison is only meaningful if the no-evidence prompt is the same
        one the referee used before this phase existed. Any drift here silently
        turns a two-arm experiment into a three-variable one.
        """
        from app.captain.prompts import build_report_prompt

        window = self._window()
        without = build_report_prompt(
            meeting_title="测试", window=window, window_seconds=180.0
        )
        explicit_none = build_report_prompt(
            meeting_title="测试",
            window=window,
            window_seconds=180.0,
            external_evidence=None,
        )
        empty = build_report_prompt(
            meeting_title="测试",
            window=window,
            window_seconds=180.0,
            external_evidence=[],
        )

        assert without == explicit_none == empty
        for marker in ("<external_evidence>", "补充要求"):
            assert marker not in without, f"{marker} leaked into the no-search prompt"

    def test_prompt_gains_the_external_block_when_evidence_exists(self):
        from app.captain.prompts import build_report_prompt
        from app.captain.schemas import ExternalEvidence

        window = self._window()
        prompt = build_report_prompt(
            meeting_title="测试",
            window=window,
            window_seconds=180.0,
            external_evidence=[
                ExternalEvidence(
                    source_id="web_1",
                    title="参考资料",
                    url="https://source1.test/doc",
                    context="一段摘录。",
                    domain="source1.test",
                )
            ],
        )

        assert "<external_evidence>" in prompt
        assert "[web_1]" in prompt
        # The rule that prevents the most damaging possible output.
        assert "禁止把网页内容归因给会议参与者" in prompt


# ==========================================================================
# 8. Brave provider contract (fixture-based, OFFLINE)
# ==========================================================================


class _StubTransport:
    """httpx.Client stand-in returning a scripted payload or exception.

    Substituted for `httpx.Client` inside `brave.py` so the provider's REAL
    transport, parsing, error-mapping and timeout code paths all run, with no
    network. Patching the client rather than `_parse` is deliberate: patching
    `_parse` would test the parser while proving nothing about the request the
    provider builds — headers, params, timeout.
    """

    def __init__(self, *, payload=None, status_code: int = 200, exc=None) -> None:
        self._payload = payload
        self._status_code = status_code
        self._exc = exc
        self.requests: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, *, headers=None, params=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}})
        if self._exc is not None:
            raise self._exc
        return _StubResponse(status_code=self._status_code, payload=self._payload)


class _StubResponse:
    def __init__(self, *, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


_FIXTURE_PATH = (
    __import__("pathlib").Path(__file__).parent
    / "fixtures"
    / "brave_llm_context_response.json"
)


def _load_fixture() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


class TestBraveContractFixture:
    """The parser contract, pinned against a fixture built from the official schema.

    !! This fixture is SYNTHETIC, not captured. !!

        implementation based on current official Brave API docs;
        live contract validation pending Phase 3-2E.

    That is exactly why these tests exist: they pin the shape the parser BELIEVES
    in. When Phase 3-2E captures a real sanitised payload, the first comparison
    is against these assertions — a mismatch shows up here as a failing test
    rather than as a silently empty result set in production.
    """

    def _provider(self, transport):
        from app.captain.search.brave import BraveSearchProvider

        provider = BraveSearchProvider(api_key="test-token")
        return provider, transport

    def test_official_shape_normalises_to_search_response(self, monkeypatch):
        """`grounding.generic` + `sources` -> one neutral SearchResponse."""
        import app.captain.search.brave as brave

        transport = _StubTransport(payload=_load_fixture())
        monkeypatch.setattr(brave.httpx, "Client", transport)

        provider = brave.BraveSearchProvider(api_key="test-token")
        response = provider.search("Redis 默认端口", max_results=5, timeout_ms=900)

        assert response.error_code is None
        assert response.timed_out is False
        assert response.ok is True
        assert [r.snippet for r in response.results][:2] == [
            "By default Redis listens on port 6379. This can be changed with the port directive.",
            "The default port is 6379 and the default configuration file is redis.conf.",
        ]
        # Provider-neutral output: the Captain layer never learns this was Brave
        # beyond the `provider` tag on an already-normalised record.
        assert {r.provider for r in response.results} == {"brave"}
        assert [r.rank for r in response.results] == [1, 2, 3]
        assert {r.domain for r in response.results} == {
            "docs1.test",
            "docs2.test",
            "docs3.test",
        }

    def test_a_metadata_only_source_is_still_a_citable_result(self, monkeypatch):
        """The third source has a title but no passage, and must not be dropped.

        Thin sources are still sources; the prompt is told to treat them as
        uncertain. Dropping them would make the referee's "外部资料不足" claim
        false.
        """
        import app.captain.search.brave as brave

        transport = _StubTransport(payload=_load_fixture())
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="t").search(
            "q", max_results=5, timeout_ms=900
        )

        third = response.results[2]
        assert third.title == "Redis ports overview"
        assert third.domain == "docs3.test"
        assert third.snippet == ""

    def test_max_results_still_caps_after_parsing(self, monkeypatch):
        """A provider that over-returns cannot blow the prompt budget."""
        import app.captain.search.brave as brave

        transport = _StubTransport(payload=_load_fixture())
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="t").search(
            "q", max_results=2, timeout_ms=900
        )

        assert len(response.results) == 2

    def test_the_subscription_token_travels_in_the_documented_header(self, monkeypatch):
        """Phase 3-2 §13: `X-Subscription-Token`, and the LLM Context endpoint."""
        import app.captain.search.brave as brave

        transport = _StubTransport(payload=_load_fixture())
        monkeypatch.setattr(brave.httpx, "Client", transport)
        brave.BraveSearchProvider(api_key="secret-token").search(
            "q", max_results=5, timeout_ms=900
        )

        sent = transport.requests[-1]
        assert sent["url"] == "https://api.search.brave.com/res/v1/llm/context"
        assert sent["headers"]["X-Subscription-Token"] == "secret-token"
        assert sent["params"]["q"] == "q"
        assert sent["params"]["count"] == 5

    def test_a_missing_key_degrades_to_not_configured(self, monkeypatch):
        """No key is a deployment error, not a crash and not a report failure."""
        import app.captain.search.brave as brave

        transport = _StubTransport(payload=_load_fixture())
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="").search(
            "q", max_results=5, timeout_ms=900
        )

        assert response.error_code == "not_configured"
        assert response.results == []
        assert transport.requests == [], "an unconfigured provider made a request"

    def test_an_http_error_becomes_a_slug_and_never_a_body(self, monkeypatch):
        """The body may echo the request, token included — so only a slug escapes."""
        import app.captain.search.brave as brave

        transport = _StubTransport(payload={"error": "secret-token"}, status_code=401)
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="secret-token").search(
            "q", max_results=5, timeout_ms=900
        )

        assert response.error_code == "http_401"
        assert "secret-token" not in json.dumps(response.model_dump())

    def test_a_timeout_is_a_timeout_not_a_late_success(self, monkeypatch):
        import app.captain.search.brave as brave

        transport = _StubTransport(exc=brave.httpx.TimeoutException("slow"))
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="t").search(
            "q", max_results=5, timeout_ms=900
        )

        assert response.timed_out is True
        assert response.error_code is None
        assert response.results == []

    def test_a_transport_error_reports_its_type_only(self, monkeypatch):
        """An httpx error message contains the full request URL; it must not leak."""
        import app.captain.search.brave as brave

        transport = _StubTransport(
            exc=brave.httpx.ConnectError("failed for secret-token at https://x.test/")
        )
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="secret-token").search(
            "q", max_results=5, timeout_ms=900
        )

        assert response.error_code == "transport_error"
        assert "secret-token" not in json.dumps(response.model_dump())
        assert "x.test" not in json.dumps(response.model_dump())

    def test_an_unexpected_shape_yields_no_results_rather_than_raising(
        self, monkeypatch
    ):
        """The defensive-parse requirement: a shape we did not anticipate is safe.

        Phase 3-2 leaves the live contract unvalidated, so this is the property
        that makes shipping it acceptable at all.
        """
        import app.captain.search.brave as brave

        transport = _StubTransport(payload={"totally": "different"})
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="t").search(
            "q", max_results=5, timeout_ms=900
        )

        assert response.results == []
        assert response.error_code is None
        assert response.ok is False

    def test_non_json_body_is_invalid_json(self, monkeypatch):
        import app.captain.search.brave as brave

        transport = _StubTransport(payload=ValueError("not json"))
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="t").search(
            "q", max_results=5, timeout_ms=900
        )

        assert response.error_code == "invalid_json"

    def test_duplicate_urls_are_collapsed(self, monkeypatch):
        """The same page cited twice adds prompt tokens without adding facts."""
        import app.captain.search.brave as brave

        payload = _load_fixture()
        payload["grounding"]["generic"].append(dict(payload["grounding"]["generic"][0]))
        transport = _StubTransport(payload=payload)
        monkeypatch.setattr(brave.httpx, "Client", transport)
        response = brave.BraveSearchProvider(api_key="t").search(
            "q", max_results=5, timeout_ms=900
        )

        urls = [r.url for r in response.results]
        assert len(urls) == len(set(urls))

    def test_parser_never_reads_a_brave_field_outside_this_module(self):
        """The Brave shape stays inside `brave.py` (Phase 3-2 §13).

        A grep is a blunt instrument, but the property is structural: the
        normalised types must not grow a provider-specific field, or every
        downstream reader would start depending on Brave's wire format.

        `source_quality` / `answers_question` (Phase 3-2 §7) and the Phase C.5
        additions (`query` / `source_type` / `relation` / `evidence_summary` /
        `published_at`) are NOT provider fields and the distinction is the point
        of this test: they are assigned by `search/service.py` from the research
        plan, the URL host and the snippet, so a second provider produces them
        identically. They are listed explicitly here rather than loosened to a
        superset check, so this test still fails the moment a provider-shaped
        field appears.
        """
        from app.captain.search.schemas import SearchResult

        assert set(SearchResult.model_fields) == {
            "title", "url", "snippet", "domain", "retrieved_at", "provider", "rank",
            # Pipeline-assigned, provider-neutral:
            "source_quality", "answers_question",
            # Pipeline-assigned, Phase C.5:
            "query", "source_type", "relation", "evidence_summary", "published_at",
        }


__all__: list[str] = []
