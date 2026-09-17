"""LLM provider layer (PRD §8, architecture §11, ADR D14).

The business layer depends only on `LLMProvider` (base.py). Concrete
providers: DeepSeekProvider (real internal LLM, env-only config) and
FakeProvider (offline deterministic default for tests). The factory resolves
which one at runtime via settings.llm_provider.
"""

from app.services.llm.base import LLMProvider, SummaryGenerationError
from app.services.llm.factory import get_provider

__all__ = ["LLMProvider", "SummaryGenerationError", "get_provider"]
