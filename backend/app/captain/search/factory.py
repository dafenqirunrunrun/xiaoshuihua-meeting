"""Search provider factory (Phase 3-2 §11).

Config-driven selection, mirroring `app/agent/llm/factory.py` — including its
fail-closed rule: an unrecognised `CAPTAIN_SEARCH_PROVIDER` raises rather than
silently falling back to a working-but-wrong provider. A typo that silently
selected `fake` would make an A/B run compare real-provider telemetry against
fabricated results.

The `force_fake` switch
-----------------------
`CAPTAIN_SEARCH_FORCE_FAKE` exists so a test or CI can prove the whole pipeline
without network even when the deployment is configured for a real provider.
Without it, a suite that wants determinism would have to patch the factory
(which tests do anyway) or point at an unreachable host (which turns a passing
suite into a flaky one).
"""

from __future__ import annotations

from app.captain.search.brave import BraveSearchProvider
from app.captain.search.fake import FakeSearchProvider
from app.captain.search.provider import SearchProviderError, WebSearchProvider
from app.config import settings

_VALID = frozenset({"fake", "brave"})


def get_search_provider(name: str | None = None) -> WebSearchProvider:
    """Resolve a search provider. Raises SearchProviderError when unknown.

    `name` overrides the setting (tests / the A/B harness); when omitted the
    configured `captain_search_provider` is used.
    """
    if settings.captain_search_force_fake:
        return FakeSearchProvider(generate=True, result_count=3)

    kind = (name or settings.captain_search_provider or "").strip().lower()
    if kind == "fake":
        return FakeSearchProvider(generate=True, result_count=3)
    if kind == "brave":
        # Lazy key check: constructing is side-effect free, and a missing key
        # surfaces as an `not_configured` error_code on the first search rather
        # than as a startup crash. Same lazy stance as DeepSeekProvider (D14).
        return BraveSearchProvider(api_key=settings.brave_search_api_key)
    raise SearchProviderError(
        f"CAPTAIN_SEARCH_PROVIDER must be one of {sorted(_VALID)}, got {kind!r}. "
        "Failing closed — refusing to fall back to a provider you did not choose."
    )


__all__ = ["get_search_provider"]
