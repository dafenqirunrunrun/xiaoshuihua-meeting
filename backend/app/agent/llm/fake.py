"""FakeProvider — offline, deterministic LLM for tests and local dev (ADR D14).

The business pipeline must run end-to-end with zero network. Tests inject a
FakeProvider via `summary_service.generate(..., provider=...)` /
`meeting_state_service.process_pending(..., provider=...)` and script it:

  - `responses`  — a FIFO of responses: each `complete()` call pops the next.
  - `error`      — if set, every call raises a SummaryGenerationError with a
                   scripted message (or simulates a timeout / HTTP-style error).

Raw outputs do NOT go through a real model; they are whatever the test needs
to exercise validation, repair, and failure paths deterministically.

Two DEFAULT shapes, because the pipeline has two structured prompts
--------------------------------------------------------------------------
The fake used to return one fixed JSON — the LEGACY SUMMARY shape. That was
fine while the summary was the only LLM consumer, but AGENT-1 validates its own
`MeetingStateUpdate` (``extra="forbid"``) and the legacy shape is not a valid
instance of it, so every default-configured local run failed the state update
and silently degraded to `transcript_fallback`. Local dev therefore never
exercised the AGENT-1 -> AGENT-2 path at all.

The fake now answers each prompt in the shape THAT prompt asks for, picked by
the same delimiter markers the real prompts are built from (no prompt parsing
beyond a substring check, no new framework):

    prompt contains `<meeting_state>`     -> FINAL SUMMARY (V2) JSON
    prompt contains `<discussion_window>` -> CAPTAIN REPORT JSON (ADR-002)
    prompt contains `<new_transcript>`    -> MEETING STATE UPDATE JSON
    otherwise                             -> LEGACY SUMMARY JSON

`<meeting_state>` means "the summary agent was given a meeting state"; the
state-update prompt instead embeds `<current_state>` + `<new_transcript>`, and
the captain prompt embeds `<discussion_window>`.

The markers are IMPORTED from the prompt modules, never re-typed: a hardcoded
copy silently stops matching the moment a delimiter is renamed, and the only
symptom is a shape mismatch far downstream (exactly the bug this fix is for).
The imports are safe — neither prompt module reaches the LLM layer, and both
packages keep import-free `__init__`s.

This is DEVELOPMENT/CI INFRASTRUCTURE ONLY. It does not measure, approximate,
or predict real model quality — it exists so the pipeline can be exercised
offline. `LLM_PROVIDER=deepseek` is what validates the real model.
"""

from __future__ import annotations

from app.agent.final_summary_prompts import STATE_DELIMITER_OPEN as _SUMMARY_MARKER
from app.captain.prompts import WINDOW_DELIMITER_OPEN as _CAPTAIN_MARKER
from app.meeting_state.prompts import BATCH_DELIMITER_OPEN as _STATE_MARKER

from .base import SummaryGenerationError

# The legacy summary shape (Step 7A). Kept byte-identical to the original so
# existing assertions on it do not move.
DEFAULT_JSON = (
    '{"overview": "会议讨论了小水华会议助手 MVP 方案。", '
    '"key_topics": ["实时转写", "会议纪要"], '
    '"conclusions": ["MVP 只跑通两条主链路"], '
    '"open_questions": ["是否接真实 ASR"]}'
)

# A valid MeetingStateDelta. An empty operation list is a legal no-op and keeps
# offline development deterministic.
DEFAULT_STATE_JSON = '{"operations": []}'

# A valid FinalMeetingSummary (V2). Minimal but structurally complete: only
# `overview` is required, every list defaults to empty.
DEFAULT_V2_JSON = '{"overview": "会议讨论了小水华会议助手 MVP 方案。", "key_topics": [], "decisions": [], "action_items": [], "open_questions": [], "risks": [], "closing_summary": ""}'

# A valid CaptainReportContent (ADR-002). `positions` has min_length=1 and
# `uncertainty` is mandatory, so this is the smallest structurally legal report.
# `evidence` is intentionally EMPTY: the fake cannot know the real transcript, so
# citing anything would be a fabricated quote — and `captain/evidence.py` would
# (correctly) drop it. An honest empty citation list is the accurate default.
DEFAULT_CAPTAIN_JSON = (
    '{"discussion_summary": "会议就当前方案进行了讨论，尚未形成一致结论。", '
    '"positions": [{"label": "继续推进", "statement": "主张按现有方案继续推进。", '
    '"source_segment_seqs": []}], '
    '"agreement_points": [], "dispute_points": [], "evidence": [], '
    '"uncertainty": "本次为离线默认输出，未基于真实转写内容判断。", '
    '"suggestion": "建议补充方案的成本与排期信息后再继续讨论。"}'
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
        return self._default_for(prompt)

    def _default_for(self, prompt: str) -> str:
        """Pick the default response shape matching the prompt's contract.

        The summary marker is checked FIRST: the V2 summary prompt embeds a
        `<meeting_state>` block and would otherwise be mistaken for an update.
        The captain marker is distinct from both and cannot collide.
        """
        if _SUMMARY_MARKER in prompt:
            return DEFAULT_V2_JSON
        if _CAPTAIN_MARKER in prompt:
            return DEFAULT_CAPTAIN_JSON
        if _STATE_MARKER in prompt:
            return DEFAULT_STATE_JSON
        return DEFAULT_JSON

    # -- determinism helpers -------------------------------------------------

    def will_error(self, message: str | None = None) -> "FakeProvider":
        self.error = message or "fake provider error"
        return self

    def queue(self, *responses: str) -> "FakeProvider":
        self.responses.extend(responses)
        return self


__all__ = [
    "DEFAULT_JSON",
    "DEFAULT_STATE_JSON",
    "DEFAULT_V2_JSON",
    "DEFAULT_CAPTAIN_JSON",
    "FakeProvider",
]
