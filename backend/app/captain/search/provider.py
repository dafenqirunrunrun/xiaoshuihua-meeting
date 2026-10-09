"""WebSearchProvider — the vendor-neutral search contract (Phase 3-2 §3/§11).

Mirrors `app/agent/llm/base.py`: the business layer depends on this Protocol and
never on a vendor, and the factory (not the caller) decides which concrete
provider runs. That is what makes the Phase 3-2E A/B a CONFIG change
(`CAPTAIN_SEARCH_PROVIDER=brave` -> `exa`) rather than a code change.

Stated as an anti-pattern, because it is the tempting shortcut: the Captain
service must never contain

    if provider == "brave": ...
    elif provider == "exa": ...

Each branch there would mean every future provider has to edit the referee's
core, and the two would drift until the A/B compares apples to oranges.

The return type is `SearchResponse`, never an exception
-------------------------------------------------------
`search()` is expected to RETURN failures (timeout, HTTP error, malformed body)
as a `SearchResponse` with `timed_out` / `error_code` set. Raising is reserved
for genuine programming errors. Rationale in `schemas.py`: search is enrichment,
so "it did not work" is a normal outcome the pipeline has one code path for.

Timeout semantics
-----------------
`timeout_ms` is a HARD deadline. An implementation that gets an answer after the
deadline must discard it rather than return it late — a late answer costs the
user latency but delivers nothing, because the Captain LLM has already started
without it. Implementations should therefore prefer a transport-level timeout
over a post-hoc elapsed-time check.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.captain.search.schemas import SearchResponse


class SearchProviderError(Exception):
    """A search provider's configuration is unusable (missing key, bad name).

    Deliberately distinct from a failed search: an unconfigured provider is a
    DEPLOYMENT problem worth logging loudly, whereas a timed-out search is a
    normal Tuesday. Neither may fail a Captain report.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


@runtime_checkable
class WebSearchProvider(Protocol):
    """Minimal search contract. Implementations must not raise for timeouts."""

    name: str

    def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_ms: int,
    ) -> SearchResponse:
        """Run one query under a hard deadline. Never raises for provider errors."""
        ...


__all__ = ["WebSearchProvider", "SearchProviderError"]
