"""In-memory TTL cache for search results (Phase 3-2 §9).

Deliberately NOT Redis. The referee is a single-process sidecar (ADR-001 D8) and
its search volume is a handful of queries per meeting, so a distributed cache
would add an operational dependency and a network round trip to save a search
that the local dict already answers. The interface is narrow enough that a Redis
implementation can replace this one without touching the caller.

Cache key
---------
`(provider, normalized_query, freshness_class)`. Three parts, each load-bearing:

  provider        an A/B run must not serve Brave results to an Exa arm (or
                  vice versa) — that would silently compare a provider against
                  itself and make the whole evaluation worthless.
  normalized_query  case/whitespace/punctuation-insensitive, so "Redis 默认端口"
                  and "redis 默认端口" share one entry.
  freshness_class   the TTL decision. The same words can be a STATIC_FACT in one
                  context and a CURRENT_FACT in another, and they must not share
                  an entry, or a 24h cache would pin a stale "current" answer.

TTL by class (Phase 3-2 §9)
---------------------------
  STATIC_FACT   24h   "鸡和蛋哪个先出现" — the answer does not move
  TECH_FACT      1h   "Redis 默认端口"    — stable, but docs/versions shift
  CURRENT_FACT   5m   "现在是否支持 X"    — freshness is the whole point

Bound on size
-------------
A plain dict would grow without bound over a long-lived process. Entries are
evicted on write once `maxsize` is exceeded, oldest-expiry first — not LRU,
because expiry order is the property that actually matters here (an LRU could
evict a fresh STATIC_FACT to keep an expiring CURRENT_FACT).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from app.captain.search.schemas import FreshnessClass, SearchResponse

# TTL per freshness class, in seconds.
TTL_SECONDS: dict[str, float] = {
    "STATIC_FACT": 24 * 60 * 60,
    "TECH_FACT": 60 * 60,
    "CURRENT_FACT": 5 * 60,
}

_NORMALIZE_RE = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】]+")


def normalize_query(query: str) -> str:
    """Case- and punctuation-insensitive cache identity for a query."""
    return _NORMALIZE_RE.sub("", (query or "").strip().lower())


@dataclass
class _Entry:
    response: SearchResponse
    expires_at: float


class SearchCache:
    """Process-local TTL cache. Thread-safe (the worker is its own thread)."""

    def __init__(self, *, maxsize: int = 256, clock=time.monotonic) -> None:
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._lock = threading.RLock()
        self._maxsize = max(1, maxsize)
        # Injectable clock so tests can prove expiry without sleeping.
        self._clock = clock
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(
        provider: str, query: str, freshness: FreshnessClass
    ) -> tuple[str, str, str]:
        return (provider, normalize_query(query), freshness)

    def get(
        self, *, provider: str, query: str, freshness: FreshnessClass
    ) -> SearchResponse | None:
        """A live cached response, or None. Expired entries are dropped."""
        key = self._key(provider, query, freshness)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return entry.response

    def put(
        self,
        *,
        provider: str,
        query: str,
        freshness: FreshnessClass,
        response: SearchResponse,
    ) -> None:
        """Store a response. Failed/dead responses are never cached.

        Caching a timeout would make the degradation sticky: one unlucky 900ms
        window would deny that query a real attempt for the whole TTL, and the
        user would keep seeing "search did not finish" long after the provider
        recovered.
        """
        if response.timed_out or response.error_code is not None:
            return
        if not response.results:
            # An honest empty result is cheap to re-ask and often a transient
            # provider hiccup; not worth pinning.
            return
        ttl = TTL_SECONDS.get(freshness, TTL_SECONDS["TECH_FACT"])
        key = self._key(provider, query, freshness)
        with self._lock:
            self._evict_if_needed()
            self._entries[key] = _Entry(
                response=response, expires_at=self._clock() + ttl
            )

    def _evict_if_needed(self) -> None:
        if len(self._entries) < self._maxsize:
            return
        # Drop whatever expires soonest, including already-expired entries.
        ordered = sorted(self._entries.items(), key=lambda kv: kv[1].expires_at)
        for key, _entry in ordered[: max(1, len(ordered) // 4)]:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# Process-wide cache. Same non-durable stance as the throttle in
# `captain/service.py`: forgetting it on restart is correct, not a bug.
_search_cache = SearchCache()


def get_cache() -> SearchCache:
    return _search_cache


def reset_cache() -> None:
    """Clear the process cache (tests / shutdown)."""
    _search_cache.clear()


__all__ = [
    "SearchCache",
    "TTL_SECONDS",
    "get_cache",
    "normalize_query",
    "reset_cache",
]
