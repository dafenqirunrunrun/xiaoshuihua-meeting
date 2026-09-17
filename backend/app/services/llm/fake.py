"""FakeProvider — offline, deterministic LLM for tests (ADR D14 default).

The business pipeline must run end-to-end with zero network. Tests inject a
FakeProvider via `summary_service.generate(..., provider=...)` and script it:

  - `scripted`   — map prompt_fragment -> raw output (valid JSON or invalid).
  - `responses`  — a FIFO of responses: each `complete()` call pops the next.
  - `error`      — if set, every call raises a SummaryGenerationError with a
                   scripted message (or simulates a timeout / HTTP-style error).

Raw outputs do NOT go through a real model; they are whatever the test needs
to exercise validation, repair, and failure paths deterministically.
"""

from __future__ import annotations

from app.services.llm.base import SummaryGenerationError

DEFAULT_JSON = (
    '{"overview": "会议讨论了小水华会议助手 MVP 方案。", '
    '"key_topics": ["实时转写", "会议纪要"], '
    '"conclusions": ["MVP 只跑通两条主链路"], '
    '"open_questions": ["是否接真实 ASR"]}'
)


class FakeProvider:
    name = "fake-provider"

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        error: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.responses = list(responses) if responses else []
        self.error = error
        self.error_message = error_message or ("fake provider error")
        self.calls: list[str] = []

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        self.calls.append(prompt)
        if self.error:
            raise SummaryGenerationError(self.error_message)
        if self.responses:
            return self.responses.pop(0)
        return DEFAULT_JSON

    # -- determinism helpers -------------------------------------------------

    def will_error(self, message: str | None = None) -> "FakeProvider":
        self.error = message or "fake provider error"
        return self

    def queue(self, *responses: str) -> "FakeProvider":
        self.responses.extend(responses)
        return self


__all__ = ["FakeProvider", "DEFAULT_JSON"]
