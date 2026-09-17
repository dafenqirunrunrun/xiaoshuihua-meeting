"""LLM provider layer (PRD §8, architecture §11, ADR D14).

The business layer depends only on `LLMProvider` (base.py). Concrete
providers: DeepSeekProvider (real internal LLM, env-only config) and
FakeProvider (offline deterministic default for tests). The factory resolves
which one at runtime via settings.llm_provider; an unknown value raises
LLMConfigurationError (fail-closed, never falls back to Fake silently).
"""

from app.services.llm.base import LLMConfigurationError, LLMProvider, SummaryGenerationError
from app.services.llm.factory import get_provider

__all__ = ["LLMProvider", "SummaryGenerationError", "LLMConfigurationError", "get_provider"]
