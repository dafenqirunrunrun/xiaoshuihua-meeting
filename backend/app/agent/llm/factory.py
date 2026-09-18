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

from app.config import settings
from .base import LLMConfigurationError, LLMProvider
from .deepseek import DeepSeekProvider
from .fake import FakeProvider

_VALID = frozenset({"fake", "deepseek"})


def get_provider() -> LLMProvider:
    kind = (settings.llm_provider or "").strip().lower()
    if kind == "deepseek":
        return DeepSeekProvider()
    if kind == "fake":
        return FakeProvider()
    raise LLMConfigurationError(
        f"LLM_PROVIDER must be one of {sorted(_VALID)}, got {settings.llm_provider!r}. "
        "Failing closed — refusing to fall back to FakeProvider."
    )


__all__ = ["get_provider"]
