"""External evidence search for the referee (Phase 3-2, ADR-003).

Pluggable web search that ENRICHES a Captain report with outside facts without
becoming a dependency of it.

The one rule that shapes every module here
------------------------------------------
**A search can never fail a Captain report.** Search is enrichment: if the
provider is slow, broken, unconfigured or wrong about its own response format,
the referee still produces a report from the meeting content alone and SAYS SO
in `uncertainty`. Every layer is built to make that unavoidable rather than
merely intended — failures are values (`SearchResponse.timed_out` /
`error_code`), not exceptions, and `service.enrich()` has no raise path.

Layout
------
    policy.py    pure rules: does this discussion need an external fact?
    provider.py  the vendor-neutral `WebSearchProvider` Protocol
    schemas.py   SearchResult / SearchResponse / SearchMetrics
    cache.py     in-memory TTL cache keyed by (provider, query, freshness)
    brave.py     BraveSearchProvider (LLM Context) — vendor detail stops here
    fake.py      FakeSearchProvider — offline determinism for tests
    factory.py   config -> provider, fail-closed on an unknown name
    service.py   policy -> cache -> provider -> evidence + metrics (never raises)

Not an agent
------------
There is no router LLM, no ReAct loop, no page fetching and no reranker (Phase
3-2 §1/§6/§14). The referee still spends exactly ONE LLM call; search only
changes what that one call is given to read.
"""

__all__: list[str] = []
