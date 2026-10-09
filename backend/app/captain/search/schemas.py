"""Search-layer contracts (Phase 3-2, ADR-003).

Two families of types live here and they are deliberately NOT the same thing:

  `SearchResult` / `SearchResponse`   what a PROVIDER returns. Provider-shaped
                                      but vendor-neutral: the fields are the
                                      ones every web-search API can supply, so
                                      adding Exa later touches only a provider.

  `SearchMetrics`                     what an A/B TEST consumes. Engineering
                                      only — never rendered, never persisted
                                      into the report body (Phase 3-2 §10).

Why `SearchResponse` carries `timed_out` and `error_code` instead of raising
----------------------------------------------------------------------------
Search is ENRICHMENT, not a dependency (Phase 3-2 §1/§3). If a provider failure
were an exception, every call site would need a try/except and the "degrade to
no external evidence" behaviour would be re-implemented — and eventually
mis-implemented — at each one. A timed-out or failed search is a NORMAL result
that carries no results; the Captain pipeline then has exactly one code path for
"no external evidence", whether that is because the policy said NO_SEARCH, the
cache was cold and the provider was slow, or the provider returned a 500.

`error_code` is a short stable SLUG (`timeout` / `http_500` / `invalid_json`),
never a provider message. Those slugs reach engineering logs; provider text
could carry a URL with an API key in it, and that must never leave the provider
module (Phase 3-2 §5: "不要暴露 provider 内部异常").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# How time-sensitive a query is. Drives the cache TTL (Phase 3-2 §9) and is a
# property of the QUERY, not of the search result.
FreshnessClass = Literal["STATIC_FACT", "TECH_FACT", "CURRENT_FACT"]


class SearchResult(BaseModel):
    """One web source, normalised across providers."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=2000)
    # The provider's pre-extracted passage. Phase 3-2 §6 forbids fetching the
    # page ourselves, so this is the ONLY content we ever see — which is also
    # why it is length-capped: it goes straight into the prompt.
    snippet: str = Field(default="", max_length=2000)
    # Derived from `url`, not supplied by the provider (providers disagree on
    # whether to send a domain field at all). Empty when the URL is unusable.
    domain: str = Field(default="", max_length=255)
    retrieved_at: str = ""
    provider: str = ""
    # 1-based position in the provider's own ordering. Kept because "the top
    # result disagreed" is a materially different signal from "an obscure
    # result disagreed".
    rank: int = Field(default=0, ge=0)
    # Authority class, assigned by `research.quality_for` from the host. A
    # HEURISTIC, not a judgement: this system cannot verify a web page. One of
    # official / standard / reputable / secondary, or "" before it is stamped.
    source_quality: str = ""
    # Which claimed question this result answers (`claim_1`, `claim_2`, …).
    # Stamped by the service from the research plan, never by the provider, so
    # a source can be traced to the claim that caused it to be fetched.
    answers_question: str = ""

    # --- Phase C.5 additions ------------------------------------------------
    # These are carried on the INTERMEDIATE model, not only on the report, so
    # the values survive the one conversion point (`external_evidence.
    # to_external_evidence`) instead of being re-derived there. Re-deriving
    # would work today and silently diverge the day one of the two derivations
    # changes — the fields exist once, on the thing the provider call produced.
    #
    # `query` is the question ACTUALLY SENT for this result. A provider response
    # already carries it, but a cache hit does not, and the value must survive
    # de-duplication and the source cap, so it is stamped per result.
    query: str = Field(default="", max_length=300)
    # What KIND of source this is, from `research.source_type_for`. A different
    # axis from `source_quality` (see `ExternalEvidence.source_type`). "" when
    # the URL could not be classified — deliberately not "other", which would
    # claim a classification that never happened.
    source_type: str = Field(default="", max_length=32)
    # How this source bears on the claim it answers: support / contradict /
    # contextual. Almost always `contextual`; see `research.relation_for` for
    # why that is the honest outcome rather than a failure.
    relation: str = Field(default="", max_length=16)
    # A compression of `snippet`, produced by the pipeline (not the model) and
    # length-capped here because it lands in the report body. It is
    # search-result-level grounding only — the page was never fetched.
    evidence_summary: str = Field(default="", max_length=1000)
    # Publication date AS THE PROVIDER REPORTED IT, or "". Never inferred from
    # the URL and never guessed by a model: an invented date is a fabricated
    # credential the reader cannot detect.
    published_at: str = Field(default="", max_length=64)


class SearchResponse(BaseModel):
    """The result of ONE search attempt. Never raises for provider problems."""

    model_config = ConfigDict(extra="forbid")

    query: str = ""
    results: list[SearchResult] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    provider: str = ""
    # True when the attempt exceeded its budget. `results` is then empty by
    # construction — a late answer is discarded, not used (Phase 3-2 §5:
    # "超过 timeout：立即降级，不等待").
    timed_out: bool = False
    # Stable short slug, or None. NEVER a provider message or exception repr.
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        """True when this attempt produced usable sources."""
        return not self.timed_out and self.error_code is None and bool(self.results)


class SearchMetrics(BaseModel):
    """Per-analysis A/B telemetry (Phase 3-2 §10).

    Every field exists to answer a question Phase 3-2E will ask when comparing
    Brave against Exa:

      search_latency_ms        -> P50 / P95 latency
      search_timed_out         -> timeout rate
      search_result_count      -> did the provider return anything at all
      external_evidence_count  -> how much survived into the report
      search_cache_hit         -> does caching actually pay for itself
      captain_total_latency_ms -> end-to-end cost, which is what the user feels
      captain_llm_latency_ms   -> isolates the LLM from the search portion
      search_policy_type       -> which decisions actually trigger search

    `search_result_count` and `external_evidence_count` are SEPARATE on purpose:
    a provider can return 5 results that all fail normalisation or all duplicate
    the same domain, and collapsing the two numbers would hide exactly that.

    This is engineering telemetry. It is logged, never rendered, and never
    written into the report body.
    """

    model_config = ConfigDict(extra="forbid")

    # --- policy ---
    search_triggered: bool = False
    search_policy_type: str = "NO_SEARCH"
    # Why the policy decided what it decided. Ops/debug only — a short slug.
    search_policy_reason: str = ""
    # Number of queries actually SENT to the provider (0..2, Phase 3-2 §4).
    search_query_count: int = Field(default=0, ge=0)

    # --- provider ---
    search_provider: str = ""
    search_latency_ms: int = Field(default=0, ge=0)
    search_timed_out: bool = False
    # Worst `error_code` seen across the queries in this analysis (or None).
    search_error_code: str | None = None
    search_result_count: int = Field(default=0, ge=0)
    # True when EVERY provider call was served from cache.
    search_cache_hit: bool = False

    # --- output ---
    external_evidence_count: int = Field(default=0, ge=0)

    # --- research routing (Phase 3-2 §7) ---
    # True when the router planned at least one question the provider was asked.
    research_needed: bool = False
    # How many specific questions were planned (0..3).
    research_question_count: int = Field(default=0, ge=0)
    # The §7 flag: a question was warranted and REFUSED for being internal.
    # This is the field an operator greps for to see how often the forbidden-
    # category rules fired in production, and it is why the flag exists at all.
    internal_information_required: bool = False
    # Slugs of the forbidden categories that refused a question, in declaration
    # order. Empty when nothing was refused.
    research_blocked_categories: list[str] = Field(default_factory=list)

    # --- end to end ---
    captain_total_latency_ms: int = Field(default=0, ge=0)
    # None when the LLM was never called (e.g. the window was unusable).
    captain_llm_latency_ms: int | None = None


def utc_now_iso() -> str:
    """Timestamp for `SearchResult.retrieved_at`. UTC, second resolution."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def domain_of(url: str) -> str:
    """Best-effort host extraction for display and de-duplication.

    Deliberately dependency-free and forgiving: providers hand back absolute
    URLs, protocol-relative URLs and occasionally bare hosts. Anything
    unparseable yields "" rather than raising, because a malformed URL on one
    result must not discard the whole response.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    without_scheme = raw.split("://", 1)[1] if "://" in raw else raw
    host = without_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split("@", 1)[-1]  # strip userinfo
    host = host.split(":", 1)[0]  # strip port
    return host.lower().removeprefix("www.")


__all__ = [
    "FreshnessClass",
    "SearchResult",
    "SearchResponse",
    "SearchMetrics",
    "domain_of",
    "utc_now_iso",
]
