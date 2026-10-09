"""External-evidence enrichment — the one entry point the referee calls.

    window  ->  policy  ->  cache  ->  provider  ->  external evidence + metrics

The contract this module upholds, in one sentence: **it cannot make a Captain
report fail.** It returns an `EnrichmentResult` for every input, including a
disabled feature, an unusable window, a policy NO_SEARCH, a provider timeout, a
provider error and an unparseable response. It never raises, and it never lets a
provider's identity, message or exception cross its boundary.

That is why the return type is a dataclass with an always-present evidence list
and an always-present metrics object rather than `list | None`: callers have no
branch to forget.

Degradation is always DISCLOSED, never silent (§5)
--------------------------------------------------
When a search was attempted and produced nothing usable, the reason is recorded
on the result (`note`) so the pipeline can fold it into the report's
`uncertainty` — the user is told the judgement rests on meeting content alone,
instead of being left to assume the referee checked and found nothing. The note
is fixed product language; it never contains a provider error string.

Budget accounting
-----------------
`search_latency_ms` sums the PROVIDER time only; a cache hit contributes 0. That
is the number Phase 3-2E needs to compare Brave against Exa, so it must not be
diluted by cache or policy overhead. Total analysis latency is measured by the
caller (`captain_total_latency_ms`), which is what the user actually feels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.captain.search import policy as search_policy
from app.captain.search import research as search_research
from app.captain.search.cache import get_cache
from app.captain.search.factory import get_search_provider
from app.captain.search.provider import SearchProviderError, WebSearchProvider
from app.captain.search.schemas import SearchMetrics, SearchResponse, SearchResult
from app.config import settings

logger = logging.getLogger(__name__)

# Fixed product language for the degraded cases. Kept here (not in the prompt
# module) because it is a fact about the SEARCH, and the caller decides where it
# lands in the report.
NOTE_TIMEOUT = "本次外部资料未能在时间预算内完成检索，以下判断主要依据会议内容。"
NOTE_PROVIDER_ERROR = "本次外部资料检索未能完成，以下判断主要依据会议内容。"
NOTE_UNCONFIGURED = "外部资料检索服务当前不可用，以下判断主要依据会议内容。"
NOTE_NO_RESULTS = "外部资料检索未找到可用来源，以下判断主要依据会议内容。"
NOTE_DISABLED = "本次未启用外部资料检索，以下判断主要依据会议内容。"

# Local spellings of the `ResearchStatus` members used below. The vocabulary
# itself is declared once in `captain/schemas.py`; these are aliases so the
# search layer does not import the report module for a string constant.
SEARCH_RESEARCH_NOT_REQUIRED = "not_required"
SEARCH_RESEARCH_SUCCESS = "success"
SEARCH_RESEARCH_PARTIAL = "partial"
SEARCH_RESEARCH_TIMEOUT = "timeout"
SEARCH_RESEARCH_FAILED = "failed"


@dataclass
class EnrichmentResult:
    """External evidence plus the telemetry for this analysis."""

    evidence: list[SearchResult] = field(default_factory=list)
    metrics: SearchMetrics = field(default_factory=SearchMetrics)
    # Disclosure to fold into `uncertainty`, or "" when nothing needs saying.
    note: str = ""
    # Machine-readable outcome of this stage, one of `RESEARCH_STATUSES`. The
    # prose `note` above is for the reader; this is for the pipeline and the
    # client, which must be able to tell "we looked and found nothing" from
    # "we could not look" without string-matching Chinese prose.
    #
    # Only the terminal branches of `enrich` set it; every early return leaves
    # the default, because every early return means the same thing: no external
    # research was required. A refusal for internal material (`refused`) is
    # `not_required` too, deliberately: the correct product statement is "this
    # window needs no outside facts", and the fact that a candidate question was
    # refused is telemetry (`internal_information_required`), not a user-facing
    # research outcome.
    research_status: str = SEARCH_RESEARCH_NOT_REQUIRED


def _stamp(
    results: list[SearchResult],
    *,
    question_id: str,
    query: str = "",
    claim: str = "",
) -> list[SearchResult]:
    """Attach the pipeline-owned per-result fields.

    Done HERE rather than in the provider because every one of these values is a
    property of the ROUTER's decision, not of the search API: the provider knows
    the query string but not which meeting claim it came from, and it must stay
    vendor-neutral (`provider.py`). A provider that returned these fields itself
    would let two providers disagree about the same source's quality — and would
    put the system's own heuristics inside a third-party module.

    Four things are derived from one place:

      `source_quality`  from the URL host (coarse trust tier)
      `source_type`     from the URL host (kind of source — a separate axis)
      `relation`        from snippet-vs-claim overlap (almost always contextual)
      `evidence_summary` from the snippet, with the claim's own wording removed

    `SearchResult` is frozen-shaped (`extra="forbid"`) but mutable, so this uses
    `model_copy` — a rebuild, never an in-place edit, so a cached response's
    objects are never mutated out from under another caller.
    """
    stamped: list[SearchResult] = []
    for item in results:
        stamped.append(
            item.model_copy(
                update={
                    "source_quality": search_research.quality_for(
                        item.url, title=item.title
                    ),
                    "source_type": search_research.source_type_for(
                        item.url, title=item.title
                    ),
                    "relation": search_research.relation_for(item.snippet, claim),
                    "evidence_summary": _derive_evidence_summary(
                        item.snippet, claim
                    ),
                    "answers_question": question_id,
                    "query": query,
                }
            )
        )
    return stamped


def _derive_evidence_summary(snippet: str, claim: str) -> str:
    """The snippet, minus the claim's own words. No model involved.

    This is the pipeline's honest substitute for asking the LLM to write a
    summary of a page it has never seen (Phase C.5 forbids fetching the page, so
    a model-written "summary" could only restate the snippet or invent beyond
    it). Removing the CLAIM's vocabulary leaves what the snippet contributes
    that the meeting had not already said, which is exactly the signal the
    reader needs — "does this source add anything?" — computed by subtraction
    rather than generated.

    Empty string when the subtraction leaves nothing, which is itself the
    finding: the snippet only echoed the claim. Bounded so a pathological
    snippet cannot fill the report body.
    """
    snippet = (snippet or "").strip()
    if not snippet:
        return ""
    stripped = snippet
    for phrase in _distinctive_phrases(claim):
        stripped = stripped.replace(phrase, " ")
    stripped = " ".join(stripped.split())
    return stripped[: _EVIDENCE_SUMMARY_LIMIT]


# How much of the snippet is allowed into `evidence_summary`. The report body
# carries this per source and there can be three sources, so the cap is what
# keeps external evidence from crowding out the meeting's own content.
_EVIDENCE_SUMMARY_LIMIT = 400

# Below this length a shared string is a common word, not the claim's
# vocabulary, and removing it would shred an unrelated snippet.
_PHRASE_MIN_LEN = 2


def _distinctive_phrases(claim: str, *, limit: int = 24) -> list[str]:
    """Substrings of `claim` long enough to be worth removing from a snippet.

    Comma-split clauses, then the whole clause, longest first — so removing a
    full clause also removes the shorter pieces inside it. No NLP here on
    purpose: this is a diff, and a diff does not need a parser.
    """
    claim = (claim or "").strip()
    if not claim:
        return []
    parts = [
        part.strip()
        for part in claim.replace("?", " ").replace("？", " ").split()
    ]
    for sep in ("，", "。", "、", "；", "：", ",", ".", ";", ":"):
        expanded: list[str] = []
        for part in parts:
            expanded.extend(piece.strip() for piece in part.split(sep))
        parts = expanded
    unique: list[str] = []
    for part in sorted(set(parts), key=len, reverse=True):
        if len(part) >= _PHRASE_MIN_LEN and part not in unique:
            unique.append(part)
        if len(unique) >= limit:
            break
    return unique


def _cap_sources(
    results: list[SearchResult], limit: int
) -> list[SearchResult]:
    """Take the top `limit`, de-duplicated by domain.

    Per-domain de-duplication before the cap, not after: five results from one
    site are one source's opinion repeated, and letting them fill the budget
    would crowd out the second perspective a referee most needs. Falls back to
    filling from the skipped duplicates when de-duplication leaves too few, so a
    single-domain result set still produces evidence rather than nothing.
    """
    limit = max(0, limit)
    if limit == 0:
        return []
    picked: list[SearchResult] = []
    seen: set[str] = set()
    overflow: list[SearchResult] = []
    for item in results:
        domain = item.domain or ""
        if domain and domain in seen:
            overflow.append(item)
            continue
        seen.add(domain)
        picked.append(item)
        if len(picked) == limit:
            return picked
    for item in overflow:
        if len(picked) == limit:
            break
        picked.append(item)
    return picked


def enrich(
    segments: list[tuple[int, str]],
    *,
    provider: WebSearchProvider | None = None,
    force_provider_name: str | None = None,
) -> EnrichmentResult:
    """Run policy -> search -> external evidence. NEVER raises.

    `segments` is `[(segment_seq, text), ...]` in window order — the same input
    `search_policy.decide` takes. Text only: the policy has no business reading
    ORM rows, and the caller already has the window in hand.

    `provider` is injectable for tests and for the Phase 3-2E harness, which
    swaps providers per arm. When omitted the configured one is resolved here,
    so production call sites stay one line.
    """
    metrics = SearchMetrics()

    if not settings.captain_external_search_enabled:
        metrics.search_policy_type = "NO_SEARCH"
        metrics.search_policy_reason = "disabled"
        # No note: with the feature switched off, saying "we did not search" on
        # every report would be noise, and the deployment made this choice.
        return EnrichmentResult([], metrics, "")

    decision = search_policy.decide(segments)
    metrics.search_triggered = decision.should_search
    metrics.search_policy_type = decision.policy_type
    metrics.search_policy_reason = decision.reason

    # The §7 marker is computed BEFORE the policy verdict is honoured, so a
    # window the policy already ruled INTERNAL_DECISION reports it too. The flag
    # answers "would outside research have helped if it had been allowed?", and
    # that question has the same answer whether the policy refused for being
    # internal or the router refused the individual questions — collapsing the
    # two into one flag is what makes it greppable in production. The expensive
    # part (a provider call) is still gated below, so this costs nothing.
    if not decision.should_search:
        blocked = search_research.blocking_categories(
            " ".join(text for _seq, text in segments)
        )
        if blocked:
            metrics.internal_information_required = True
            metrics.research_blocked_categories = [c.slug for c in blocked]
        return EnrichmentResult([], metrics, "")

    # Turn the policy's coarse "search is warranted" into SPECIFIC questions,
    # and refuse on the way any question whose answer lives inside the
    # organisation (Phase 3-2 §7). This is the ONLY place the forbidden-category
    # check gates a network call, and it runs before a provider is even
    # resolved: an internal question must never cost a search, both because the
    # answer is not there and because the attempt itself is the thing being
    # forbidden.
    research_plan = search_research.plan(segments)
    metrics.research_needed = research_plan.need_research
    metrics.research_question_count = len(research_plan.questions)
    metrics.internal_information_required = (
        research_plan.internal_information_required
    )
    metrics.research_blocked_categories = [
        category.slug for category in research_plan.internal_categories
    ]
    if research_plan.internal_information_required:
        # The reason is recorded for the log; deliberately NOT a note on the
        # report. See `research.py`'s "Marking is not disclosure".
        metrics.search_policy_reason = "internal_information_required"
        return EnrichmentResult([], metrics, "")
    if not research_plan.need_research or not research_plan.questions:
        metrics.search_policy_reason = research_plan.reason or "no_question"
        return EnrichmentResult([], metrics, "")

    # Resolve the provider. A misconfigured provider name is a deployment bug;
    # it degrades this report rather than failing it (§3), but it is logged at
    # warning level because a human has to fix it.
    try:
        active = provider or get_search_provider(force_provider_name)
    except SearchProviderError as exc:
        logger.warning("search provider unavailable: %s", exc)
        metrics.search_policy_type = "NO_SEARCH"
        metrics.search_policy_reason = "provider_unavailable"
        return EnrichmentResult(
            [], metrics, NOTE_UNCONFIGURED, SEARCH_RESEARCH_FAILED
        )

    metrics.search_provider = active.name
    cache = get_cache()

    # Phase 3-2 §4: at most 2 queries, and only ever the first unless the first
    # produced nothing. A second query costs a second 900ms budget. The queries
    # come from the RESEARCH PLAN, not from the policy's raw keyword mash — a
    # question the router refused for being internal never reaches here.
    queries = [item.question for item in research_plan.questions][
        : search_policy.MAX_QUERIES
    ]
    question_ids = [
        item.supports_query_id for item in research_plan.questions
    ][: search_policy.MAX_QUERIES]
    # The claim TEXT behind each question, used for `relation` and to subtract
    # the meeting's own wording out of `evidence_summary`. Kept as a parallel
    # list rather than folded into the question objects because the router
    # already carries it and this layer must not mutate router output.
    claims = [item.origin_claim for item in research_plan.questions][
        : search_policy.MAX_QUERIES
    ]
    collected: list[SearchResult] = []
    cache_hits = 0
    attempts = 0
    total_latency = 0
    any_timeout = False
    error_code: str | None = None

    for index, query in enumerate(queries):
        if index > 0 and collected:
            break
        attempts += 1
        claim = claims[index] if index < len(claims) else ""
        cached = cache.get(
            provider=active.name, query=query, freshness=decision.freshness
        )
        if cached is not None:
            cache_hits += 1
            # Cached results were stamped when first fetched, but the QUESTION
            # ID is per-plan, so it is re-stamped here: a cache entry shared
            # across two windows must not carry the first window's claim handle.
            # `relation` and `evidence_summary` are per-plan too (they depend on
            # THIS window's claim wording) so they are recomputed for the same
            # reason — the cache stores what the provider said, not our reading
            # of it.
            collected.extend(
                _stamp(
                    cached.results,
                    question_id=question_ids[index],
                    query=query,
                    claim=claim,
                )
            )
            continue

        response = _safe_search(
            active,
            query,
            max_results=settings.captain_search_max_results,
            timeout_ms=int(settings.captain_search_timeout_ms),
        )
        total_latency += response.latency_ms
        if response.timed_out:
            any_timeout = True
            break  # no second query: the budget is already blown
        if response.error_code is not None:
            error_code = response.error_code
            break
        cache.put(
            provider=active.name,
            query=query,
            freshness=decision.freshness,
            response=response,
        )
        collected.extend(
            _stamp(
                response.results,
                question_id=question_ids[index],
                query=query,
                claim=claim,
            )
        )

    metrics.search_query_count = attempts
    metrics.search_latency_ms = total_latency
    metrics.search_timed_out = any_timeout
    metrics.search_error_code = error_code
    metrics.search_result_count = len(collected)
    metrics.search_cache_hit = attempts > 0 and cache_hits == attempts

    evidence = _cap_sources(collected, int(settings.captain_search_max_sources_for_llm))
    metrics.external_evidence_count = len(evidence)

    note = _note_for(
        timed_out=any_timeout,
        error_code=error_code,
        provider=active.name,
        result_count=len(collected),
        evidence_count=len(evidence),
    )
    return EnrichmentResult(
        evidence,
        metrics,
        note,
        _status_for(
            timed_out=any_timeout,
            error_code=error_code,
            evidence_count=len(evidence),
        ),
    )


def _status_for(
    *,
    timed_out: bool,
    error_code: str | None,
    evidence_count: int,
) -> str:
    """Classify how the research stage ended. Never returns `not_required`.

    Reached only after the router planned at least one question, so every
    outcome here is one where research WAS required — which is why this function
    cannot return `not_required` and the dataclass default covers that case.

    The three inputs are all about what ACTUALLY happened, never about how many
    queries were PLANNED. That distinction is the whole point: the loop stops
    early in two very different situations — "the first query was enough" and
    "a query failed" — and a rule keyed on `attempted < planned` cannot tell them
    apart. It labelled the first case `partial`, so a search that fully succeeded
    on its first query reported itself as incomplete. The vocabulary is not a
    bool because `partial` is real, but it is reserved for the case that
    genuinely is partial: evidence obtained, AND a query that was actually sent
    failed.

      timeout   a call timed out and no usable evidence survived
      failed    a call errored (or returned nothing usable) and no evidence survived
      partial   evidence survived, but some ACTUALLY-SENT query failed
      success   evidence survived and no sent query failed
    """
    if timed_out:
        return SEARCH_RESEARCH_TIMEOUT
    if error_code is not None and evidence_count == 0:
        return SEARCH_RESEARCH_FAILED
    if error_code is not None:
        # Sources from the query that DID succeed, plus a failure afterwards.
        return SEARCH_RESEARCH_PARTIAL
    if evidence_count == 0:
        # Every query answered, nothing usable came back. The search worked; the
        # web had nothing. That is `failed` for the reader's purpose ("no
        # external support for this judgement") even though no error occurred.
        return SEARCH_RESEARCH_FAILED
    # Evidence in hand and no failure occurred. A planned-but-never-sent query
    # (early stop after the first query sufficed) is a NORMAL way for a
    # successful search to end, not a shortfall — so it is `success`, not
    # `partial`. See the docstring: this is the case the old rule misread.
    return SEARCH_RESEARCH_SUCCESS


def _safe_search(
    provider: WebSearchProvider, query: str, *, max_results: int, timeout_ms: int
) -> SearchResponse:
    """Call a provider, converting ANY exception into a dead response.

    The Protocol says implementations return failures rather than raising, but
    this wrapper assumes they might not. A third-party provider written later
    that lets an httpx error escape must not be able to fail a Captain report —
    the guard belongs on the caller's side of the contract, not on the
    implementer's honour.
    """
    try:
        response = provider.search(
            query, max_results=max_results, timeout_ms=timeout_ms
        )
    except Exception as exc:  # noqa: BLE001 - search must never break the referee
        logger.warning("search provider raised: %s", type(exc).__name__)
        return SearchResponse(
            query=query,
            results=[],
            latency_ms=0,
            provider=getattr(provider, "name", ""),
            timed_out=False,
            error_code="provider_raised",
        )
    if not isinstance(response, SearchResponse):
        logger.warning("search provider returned a non-SearchResponse")
        return SearchResponse(
            query=query,
            results=[],
            latency_ms=0,
            provider=getattr(provider, "name", ""),
            timed_out=False,
            error_code="bad_response_type",
        )
    return response


def _note_for(
    *,
    timed_out: bool,
    error_code: str | None,
    provider: str,
    result_count: int,
    evidence_count: int,
) -> str:
    """The one disclosure sentence for a degraded search, or "" when fine.

    `evidence_count` (what survives the cap) is what matters to the reader, not
    `result_count` — but zero results and zero survivors are different failures
    and get different sentences.
    """
    if timed_out:
        return NOTE_TIMEOUT
    if error_code == "not_configured":
        return NOTE_UNCONFIGURED
    if error_code is not None:
        return NOTE_PROVIDER_ERROR
    if result_count == 0:
        return NOTE_NO_RESULTS
    if evidence_count == 0:
        return NOTE_NO_RESULTS
    return ""


__all__ = [
    "EnrichmentResult",
    "NOTE_DISABLED",
    "NOTE_NO_RESULTS",
    "NOTE_PROVIDER_ERROR",
    "NOTE_TIMEOUT",
    "NOTE_UNCONFIGURED",
    "SEARCH_RESEARCH_FAILED",
    "SEARCH_RESEARCH_NOT_REQUIRED",
    "SEARCH_RESEARCH_PARTIAL",
    "SEARCH_RESEARCH_SUCCESS",
    "SEARCH_RESEARCH_TIMEOUT",
    "enrich",
]
