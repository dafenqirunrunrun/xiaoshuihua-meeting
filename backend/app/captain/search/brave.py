"""BraveSearchProvider — Brave Search API, LLM Context endpoint.

!! PARSER STATUS — READ BEFORE TRUSTING THIS FILE !!
----------------------------------------------------
This parser is written against the Brave **LLM Context** API
(`GET https://api.search.brave.com/res/v1/llm/context`) as documented in the
official API reference, and it has **NOT been validated against a live
response** — the environment it was written in has no outbound network.

    implementation based on current official Brave API docs;
    live contract validation pending Phase 3-2E.

Consequences, all deliberate:

  * The parser is DEFENSIVE. Every field is read through `_first_str` /
    `_as_list` helpers that tolerate a missing key, a null, or an unexpected
    type, and every candidate path for the same value is tried in order. A
    response shape we did not anticipate degrades to fewer results or an
    `invalid_json` error code — never to an exception, and never to a Captain
    FAILED.
  * Brave-specific structure is CONFINED TO THIS MODULE (Phase 3-2 §13: "不要把
    任何 Brave 特定结构泄漏到 Captain 主逻辑"). Nothing outside this file
     knows what a "grounding" or a "sources" object is.
  * TODO(Phase 3-2E): on the first live call, capture a SANITISED response
     fixture (strip the subscription token and any query-bearing URLs), commit
     it under `tests/fixtures/`, and tighten this parser against it. See the
     fixture test in `tests/test_captain_search.py`, which pins the shape this
     parser currently believes in — if the live payload disagrees, that test is
     where the diff shows up.

Why LLM Context and not /res/v1/web/search
------------------------------------------
Phase 3-2 §13. The LLM Context endpoint returns passages already extracted for
grounding, which is precisely the `snippet` this feature needs. The plain web
search endpoint returns titles and descriptions, and closing that gap would mean
fetching the pages ourselves — explicitly forbidden in the first version (§6).

Timeout discipline
------------------
`timeout_ms` is passed to httpx as the request timeout and is the ONLY deadline
mechanism. There is no retry: a retry doubles the worst-case latency for a
feature whose budget is 900ms, and search is enrichment (§5). A `TimeoutException`
becomes `timed_out=True`; every other transport/HTTP problem becomes a short
`error_code` slug. Provider exception text is NEVER propagated — a provider error
string can contain the request URL, and the subscription token travels in a
header that some libraries echo into their error messages.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from app.captain.search.schemas import (
    SearchResponse,
    SearchResult,
    domain_of,
    utc_now_iso,
)
from app.captain.search.provider import SearchProviderError

logger = logging.getLogger(__name__)

BRAVE_LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"

# Cap on how much provider text we ever copy into a snippet. The snippet goes
# straight into the prompt, so this is a token budget, not a display concern.
MAX_SNIPPET_CHARS = 1200


def _first_str(obj: object, *keys: str) -> str:
    """First non-empty string among `keys`. Tolerates non-dicts and bad types."""
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        # Some APIs nest the value one level (`{"name": {"text": ...}}`).
        if isinstance(value, dict):
            for inner in ("text", "value", "name", "content"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _as_list(value: object) -> list:
    """Coerce a JSON value into a list without raising.

    A provider that returns a single object where a list was expected, or null
    where a list was expected, must not crash the parser.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _snippet_of(item: dict) -> str:
    """Best available passage for one source.

    Tries the text-bearing fields in descending order of usefulness. `snippet`
    and `description` are the usual names; `text` / `content` appear on
    LLM-context style payloads. Long values are truncated, not rejected.
    """
    for key in ("snippet", "description", "text", "content", "excerpt", "summary"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_SNIPPET_CHARS]
    return ""


class BraveSearchProvider:
    """Brave Search — LLM Context endpoint. See the module docstring first."""

    name = "brave"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = BRAVE_LLM_CONTEXT_URL,
        max_retries: int = 0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        # Retries stay at 0 by default and there is deliberately no setting for
        # it yet: under a 900ms budget a retry is almost always a slower way to
        # return nothing (Phase 3-2 §5).
        self._max_retries = max(0, max_retries)

    # --- transport ---------------------------------------------------------

    def _request(
        self, query: str, *, max_results: int, timeout_ms: int
    ) -> tuple[dict | None, str | None, bool]:
        """Return (payload, error_code, timed_out). Never raises."""
        if not self._api_key:
            # Not a search failure: an unconfigured provider is a deployment
            # error. Reported as an error_code so the pipeline still degrades
            # gracefully, but logged loudly because someone must fix it.
            logger.warning("brave search provider has no api key configured")
            return None, "not_configured", False

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            # The API's own cap; `max_results` is applied again after parsing so
            # a provider that over-returns cannot blow the prompt budget.
            "count": max(1, min(max_results, 5)),
        }
        timeout = max(0.001, timeout_ms / 1000.0)

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(self._base_url, headers=headers, params=params)
            if response.status_code >= 400:
                # Slug only — the body may echo the request, token included.
                return None, f"http_{response.status_code}", False
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                return None, "invalid_json", False
            if not isinstance(payload, dict):
                return None, "unexpected_payload", False
            return payload, None, False
        except httpx.TimeoutException:
            return None, None, True
        except httpx.HTTPError as exc:
            # Transport-level problem. Log the TYPE only, never str(exc): an
            # httpx error's message includes the full request URL.
            logger.warning("brave search transport error: %s", type(exc).__name__)
            return None, "transport_error", False
        except Exception as exc:  # noqa: BLE001 - search must never raise
            logger.warning("brave search unexpected error: %s", type(exc).__name__)
            return None, "unexpected_error", False

    # --- parsing -----------------------------------------------------------

    def _parse(self, payload: dict, *, query: str) -> list[SearchResult]:
        """Normalise a Brave LLM Context payload into neutral results.

        The LLM Context endpoint documents a `grounding` object carrying the
        passages, with per-URL metadata in `sources`. Both are read
        defensively and cross-referenced, because the ordering and the set of
        URLs are not guaranteed to line up:

          * `grounding.generic`  (list) — the extracted passages
          * `sources`            (dict, keyed by URL) — title/url metadata

        The join prefers the URL as the key and falls back to positional
        matching. When neither side is present we return NO results rather
        than inventing one — an empty result set is an honest "we learned
        nothing", which the pipeline already handles.
        """
        grounding = payload.get("grounding")
        generic = []
        if isinstance(grounding, dict):
            generic = _as_list(grounding.get("generic"))

        sources = payload.get("sources")
        # `sources` may be a dict keyed by URL, or (defensively) a list.
        source_items: list[tuple[str, dict]] = []
        if isinstance(sources, dict):
            for key, value in sources.items():
                if isinstance(value, dict):
                    source_items.append((str(key), value))
        elif isinstance(sources, list):
            for value in sources:
                if isinstance(value, dict):
                    source_items.append((_first_str(value, "url"), value))

        by_url: dict[str, tuple[str, dict]] = {
            url: (url, item) for url, item in source_items if url
        }

        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        seen_domains: set[str] = set()

        # Pass 1: the passages, which carry the content we actually want.
        for index, entry in enumerate(generic):
            if not isinstance(entry, dict):
                continue
            url = _first_str(entry, "url", "source", "link")
            text = _snippet_of(entry)
            meta: dict = {}
            if url and url in by_url:
                meta = by_url[url][1]
            elif not url and index < len(source_items):
                # No URL on the passage: fall back to positional pairing with
                # the sources list. Only safe when the two line up, which is
                # why this is a FALLBACK and not the primary join.
                fallback_url, meta = source_items[index]
                url = fallback_url

            title = _first_str(entry, "title") or _first_str(meta, "title", "name")
            if not url and not text:
                continue
            domain = domain_of(url) or _first_str(meta, "domain", "host")
            if not text and not title:
                continue
            # Same page cited twice adds prompt tokens without adding facts.
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=text,
                    domain=domain,
                    retrieved_at=utc_now_iso(),
                    provider=self.name,
                    rank=len(results) + 1,
                )
            )
            if domain:
                seen_domains.add(domain)

        # Pass 2: sources with no passage. These carry metadata only, but a
        # title plus a domain is still a citable source, and the prompt is told
        # to treat thin sources as uncertain.
        for url, item in source_items:
            if not url or url in seen_urls:
                continue
            title = _first_str(item, "title", "name")
            snippet = _snippet_of(item)
            domain = domain_of(url) or _first_str(item, "domain", "host")
            if not title and not snippet:
                continue
            if url:
                seen_urls.add(url)
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    domain=domain,
                    retrieved_at=utc_now_iso(),
                    provider=self.name,
                    rank=len(results) + 1,
                )
            )

        return results

    # --- public API --------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_ms: int,
    ) -> SearchResponse:
        """Run one query under a hard deadline. Never raises for provider errors."""
        started = time.monotonic()
        payload, error_code, timed_out = self._request(
            query, max_results=max_results, timeout_ms=timeout_ms
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if timed_out:
            return SearchResponse(
                query=query,
                results=[],
                latency_ms=max(elapsed_ms, timeout_ms),
                provider=self.name,
                timed_out=True,
                error_code=None,
            )
        if payload is None:
            return SearchResponse(
                query=query,
                results=[],
                latency_ms=elapsed_ms,
                provider=self.name,
                timed_out=False,
                error_code=error_code or "unknown",
            )

        try:
            results = self._parse(payload, query=query)[: max(0, max_results)]
        except Exception as exc:  # noqa: BLE001 - parsing must never raise
            logger.warning("brave response parse failed: %s", type(exc).__name__)
            return SearchResponse(
                query=query,
                results=[],
                latency_ms=elapsed_ms,
                provider=self.name,
                timed_out=False,
                error_code="parse_failed",
            )

        return SearchResponse(
            query=query,
            results=results,
            latency_ms=elapsed_ms,
            provider=self.name,
            timed_out=False,
            error_code=None,
        )


__all__ = ["BraveSearchProvider", "BRAVE_LLM_CONTEXT_URL", "MAX_SNIPPET_CHARS"]
