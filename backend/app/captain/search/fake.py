"""FakeSearchProvider — offline, deterministic search (tests / local dev).

The search-layer twin of `app/agent/llm/fake.py`, and it exists for the same
reason: the whole pipeline must be exercisable with ZERO network. Every Phase
3-2 test scenario (timeout, provider error, cache hit, policy NO_SEARCH) is
driven through this provider rather than through monkeypatched HTTP.

It can simulate the four outcomes the pipeline must survive:

  normal        -> `results` (or a deterministic generated set)
  timeout       -> `delay_ms` exceeds the deadline, or `timed_out=True`
  provider      -> `error_code` set (e.g. "http_500"), no results
  empty         -> a successful response with zero results

`enforce_timeout` matters: the real providers get their deadline from the
transport, so the fake must too, or a test would pass here and fail live. When
the simulated work exceeds the deadline the fake returns a TIMED-OUT response —
not a late success — which is exactly what the real implementation must do.

`max_results` is honoured as well. A fake that ignored it would let a caller
forget to cap results and still see green tests, while the live provider
returned five sources into a prompt budgeted for three.
"""

from __future__ import annotations

import time

from app.captain.search.schemas import (
    SearchResponse,
    SearchResult,
    domain_of,
    utc_now_iso,
)


class FakeSearchProvider:
    name = "fake"

    def __init__(
        self,
        *,
        results: list[SearchResult] | None = None,
        error_code: str | None = None,
        timed_out: bool = False,
        delay_ms: int = 0,
        generate: bool = False,
        result_count: int = 3,
    ) -> None:
        self._results = list(results) if results else []
        self._error_code = error_code
        self._timed_out = timed_out
        self._delay_ms = max(0, delay_ms)
        self._generate = generate
        self._result_count = max(0, result_count)
        # Every query this provider was asked for. Tests assert on this to prove
        # the cache short-circuited a call (`provider.calls == []`) and that a
        # NO_SEARCH decision never reached the provider at all.
        self.calls: list[str] = []

    def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_ms: int,
    ) -> SearchResponse:
        self.calls.append(query)
        started = time.monotonic()

        if self._delay_ms:
            time.sleep(min(self._delay_ms, timeout_ms) / 1000.0)

        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Simulated work exceeded the hard deadline: return a DEAD response.
        # Not a late success — see the module docstring.
        if self._timed_out or self._delay_ms > timeout_ms:
            return SearchResponse(
                query=query,
                results=[],
                latency_ms=max(elapsed_ms, timeout_ms),
                provider=self.name,
                timed_out=True,
                error_code=None,
            )

        if self._error_code is not None:
            return SearchResponse(
                query=query,
                results=[],
                latency_ms=elapsed_ms,
                provider=self.name,
                timed_out=False,
                error_code=self._error_code,
            )

        results = self._materialise(query, max_results)
        return SearchResponse(
            query=query,
            results=results,
            latency_ms=elapsed_ms,
            provider=self.name,
            timed_out=False,
            error_code=None,
        )

    def _materialise(self, query: str, max_results: int) -> list[SearchResult]:
        """Explicit results first (scripted), else deterministically generated."""
        if self._results:
            pool = self._results
        elif self._generate:
            pool = [
                SearchResult(
                    title=f"{query} — 参考资料 {i + 1}",
                    url=f"https://example{i + 1}.test/{query[:12]}",
                    snippet=f"关于「{query}」的离线示例资料第 {i + 1} 段。",
                    domain=f"example{i + 1}.test",
                    retrieved_at=utc_now_iso(),
                    provider=self.name,
                    rank=i + 1,
                )
                for i in range(self._result_count)
            ]
        else:
            return []
        return self._normalise(pool, max_results)

    def _normalise(self, pool: list[SearchResult], max_results: int) -> list[SearchResult]:
        """Re-stamp provider/retrieved_at/domain/rank, then cap.

        Mirrors what a real provider wrapper does, so a test that scripts bare
        results still gets the same envelope the live path produces.
        """
        capped = pool[: max(0, max_results)]
        stamped: list[SearchResult] = []
        stamp = utc_now_iso()
        for index, item in enumerate(capped, start=1):
            stamped.append(
                item.model_copy(
                    update={
                        "provider": self.name,
                        "retrieved_at": item.retrieved_at or stamp,
                        "domain": item.domain or domain_of(item.url),
                        "rank": item.rank or index,
                    }
                )
            )
        return stamped


__all__ = ["FakeSearchProvider"]
