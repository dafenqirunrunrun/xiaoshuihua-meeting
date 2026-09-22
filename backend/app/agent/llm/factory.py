"""LLMProvider factory — pick a concrete provider (ADR D14, config §11).

Resolution (fail-closed, §1/§24):
  1. `provider` passed in (tests / callers inject FakeProvider).  → absolute.
  2. `settings.llm_provider == "fake"`     → FakeProvider (explicit dev/test).
  3. `settings.llm_provider == "deepseek"` → DeepSeekProvider (env-only config).
  4. ANY other value → `LLMConfigurationError`. A typo / unknown value must
     NEVER silently fall back to FakeProvider — that would let a production
     misconfiguration pass as a working offline pipeline and generate a fake
     summary. Fail closed, surface loudly.

Fake is only used when:
  - a test explicitly injects it via `generate(..., provider=FakeProvider())`,
    or
  - the deployment env EXPLICITLY sets LLM_PROVIDER=fake (dev).

DeepSeek API key stays lazy (D14): constructing DeepSeekProvider never
touches the network — a missing key only fails on the first `complete()`, so
app startup is never blocked, and Fake in tests is the safe default there.
"""

from __future__ import annotations

from threading import RLock

from app.config import settings
from .base import LLMConfigurationError, LLMProvider
from .deepseek import DeepSeekProvider
from .fake import FakeProvider

_VALID = frozenset({"fake", "deepseek"})
_provider_lock = RLock()
_deepseek_providers: dict[tuple, DeepSeekProvider] = {}


def get_provider(
    model: str | None = None,
    *,
    max_retries: int | None = None,
    retry_timeouts: bool = True,
    max_output_tokens: int | None = None,
    thinking_enabled: bool | None = None,
) -> LLMProvider:
    """Resolve the provider. `model` optionally overrides the provider's model.

    The override exists for the Final Summary, which may legitimately want a
    different (e.g. stronger) model than the incremental Meeting State updates
    (``FINAL_SUMMARY_MODEL``). It defaults to None, so every existing caller —
    including AGENT-1's state updates and the test-injection path — is
    unaffected. Only DeepSeek uses it; Fake has no model to select.
    """
    kind = (settings.llm_provider or "").strip().lower()
    if kind == "deepseek":
        resolved_model = model or settings.deepseek_model
        resolved_retries = settings.llm_max_retries if max_retries is None else max_retries
        resolved_output_tokens = (
            settings.llm_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        key = (
            settings.deepseek_base_url,
            settings.deepseek_api_key,
            resolved_model,
            settings.llm_timeout_seconds,
            resolved_retries,
            retry_timeouts,
            settings.llm_temperature,
            resolved_output_tokens,
            thinking_enabled,
        )
        with _provider_lock:
            provider = _deepseek_providers.get(key)
            if provider is None:
                provider = DeepSeekProvider(
                    model=resolved_model,
                    max_retries=resolved_retries,
                    retry_timeouts=retry_timeouts,
                    max_output_tokens=resolved_output_tokens,
                    thinking_enabled=thinking_enabled,
                )
                _deepseek_providers[key] = provider
            return provider
    if kind == "fake":
        return FakeProvider()
    raise LLMConfigurationError(
        f"LLM_PROVIDER must be one of {sorted(_VALID)}, got {settings.llm_provider!r}. "
        "Failing closed — refusing to fall back to FakeProvider."
    )


def close_cached_providers() -> None:
    with _provider_lock:
        providers = list(_deepseek_providers.values())
        _deepseek_providers.clear()
    for provider in providers:
        provider.close()


__all__ = ["get_provider", "close_cached_providers"]
