"""LLM provider abstraction (PRD §8 / §42, architecture §11).

The business layer (summary_service) only depends on this protocol — never on
httpx / a concrete vendor. `complete(prompt) -> str` is the single primitive;
structured-output validation is a business (summary_service) concern, so the
provider is deliberately prompt-shaped, not schema-shaped.

Providers:
  - DeepSeekProvider  (app.services.llm.deepseek): env-only config, OpenAI-
                      compatible /chat/completions via httpx.
  - FakeProvider      (app.services.llm.fake): offline, deterministic, the
                      default test infrastructure (ADR D14). Test suites inject
                      scripted responses / errors instead of hitting a network.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal provider contract for summary generation."""

    name: str

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        """Return the raw model text for `prompt`. Raises SummaryGenerationError."""
        ...


class SummaryGenerationError(Exception):
    """A provider-level failure: missing config, timeout, HTTP error, bad body.

    Maps to `summary.status = FAILED` (never raises to the API layer during
    Step 7A internal generation). Distinct from a parse failure, which is a
    validation concern handled by `MeetingSummarySchema`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LLMConfigurationError(Exception):
    """The LLM provider configuration is invalid / unknown (fail-closed).

    Raised by `get_provider()` when `settings.llm_provider` is not one of the
    known values ("fake" / "deepseek"). A typo must NEVER silently fall back to
    FakeProvider — that would let a production misconfiguration pass as a
    working offline pipeline and generate a fake summary.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


__all__ = ["LLMProvider", "SummaryGenerationError", "LLMConfigurationError"]
