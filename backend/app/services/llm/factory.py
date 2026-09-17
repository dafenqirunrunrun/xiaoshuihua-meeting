"""LLMProvider factory — pick a concrete provider (ADR D14, config §11).

Resolution order:
  1. `provider` passed in (tests / callers inject FakeProvider).  → absolute.
  2. `settings.llm_provider == "fake"` → FakeProvider (default dev/test).
  3. `settings.llm_provider == "deepseek"` → DeepSeekProvider (env-only config).
  4. anything else → FakeProvider (safe default, so a typo never blocks startup).

A production deployment sets LLM_PROVIDER=deepseek before generating.
"""

from __future__ import annotations

from app.config import settings
from app.services.llm.base import LLMProvider
from app.services.llm.deepseek import DeepSeekProvider
from app.services.llm.fake import FakeProvider


def get_provider() -> LLMProvider:
    kind = (settings.llm_provider or "").strip().lower()
    if kind == "deepseek":
        return DeepSeekProvider()
    return FakeProvider()  # "fake" (or any unrecognised value) -> offline default


__all__ = ["get_provider"]
