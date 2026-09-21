"""Structured-output LLM updater for Meeting State (Step AGENT-1).

Mirrors the *shape* of the Step 7A summary pipeline — Pydantic as the success
criterion, a bounded repair prompt on a parse failure, provider-level failures
mapping straight to FAILED — without sharing its implementation. The two have
different input/output contracts (whole-transcript -> fixed string lists vs.
incremental batch -> nested state object) and the Summary pipeline is verified
and in production use, so a shared abstraction would mean editing working
behaviour for no present benefit. The invariants below are the contract that
must hold in both places:

  1. Pydantic validation IS the success criterion. Never a free-text scrape.
  2. A parse failure is repairable, at most `meeting_agent_max_attempts` times.
  3. A PROVIDER failure (timeout / HTTP / missing config / unknown provider) is
     NOT repairable — retrying a broken transport just burns latency.
  4. Failures never fabricate content: the caller keeps the previous state.

Two failure types, deliberately distinct, because they drive different policy:

  `StateUpdateError`   provider-level  -> no repair, mark FAILED immediately
  `StateSchemaError`   shape-level     -> repairable (bounded)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, NamedTuple

from pydantic import ValidationError

from app.config import settings
from app.meeting_state.schemas import MeetingStateUpdate
from app.agent.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Max raw output accepted before we treat it as runaway/malformed.
MAX_RAW_OUTPUT_CHARS = 64_000


class StateUpdateError(Exception):
    """A provider-level failure while updating meeting state (NOT repairable).

    Mirrors `summary_service.SummaryErrorBase`: it is what makes the caller stop
    and mark the update FAILED (leaving the previous state intact) instead of
    retrying a transport that is not going to work.
    """


class StateSchemaError(Exception):
    """A structured-output shape failure not already covered by Pydantic.

    Repairable: the model returned valid JSON of the wrong KIND (e.g. a list),
    which a repair prompt can fix.
    """


class UpdateResult(NamedTuple):
    """Outcome of one update attempt sequence."""

    raw_output: str
    parsed: MeetingStateUpdate | None
    reason: str | None  # set iff parsed is None

    @property
    def ok(self) -> bool:
        return self.parsed is not None


def _prune_code_fence(text: str) -> str:
    """Strip a ```json ... ``` wrapper if the model added one (lenient).

    A local copy of the summary pipeline's helper rather than an import: it is
    a 3-line formatting leniency, and reaching into another domain's private
    helper to save those lines would couple the two pipelines more tightly than
    the shared behaviour warrants.
    """
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text


def parse_update(raw: str) -> MeetingStateUpdate:
    """JSON-decode + Pydantic-validate (the success criterion).

    Raises json.JSONDecodeError / ValidationError / StateSchemaError. There is
    deliberately no fallback to a default state: a silently-empty update would
    look like "the meeting said nothing" and is indistinguishable from success.
    """
    pruned = _prune_code_fence(raw).strip()
    data = json.loads(pruned)  # JSONDecodeError propagates
    if not isinstance(data, dict):
        raise StateSchemaError(f"expected a JSON object, got {type(data).__name__}")
    return MeetingStateUpdate.model_validate(data)


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return f"validation failed: {exc.errors()[:3]}"
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}"
    return f"schema: {exc}"


def call_provider(provider: LLMProvider, prompt: str) -> str:
    """One provider call with a hard cap on the raw output; raises StateUpdateError."""
    from app.agent.llm.base import LLMConfigurationError, SummaryGenerationError

    try:
        raw = provider.complete(
            prompt,
            timeout_seconds=settings.meeting_agent_llm_timeout_seconds,
        )
    except (SummaryGenerationError, LLMConfigurationError) as exc:
        # Unknown-provider misconfiguration folds in here on purpose: it is a
        # provider-level failure, so the state is left untouched rather than
        # being filled from a fallback.
        raise StateUpdateError(str(exc)) from exc

    if not raw or not raw.strip():
        raise StateUpdateError("provider returned empty output")
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise StateUpdateError(
            f"provider output too large ({len(raw)} chars > {MAX_RAW_OUTPUT_CHARS})"
        )
    return raw


class RepairPromptBuilder:
    """Callable signature for `run_update(..., build_repair_prompt=...)`."""

    def __call__(self, original: str, raw: str, error: str) -> str: ...  # pragma: no cover


def run_update(
    provider: LLMProvider,
    prompt: str,
    *,
    build_repair_prompt: RepairPromptBuilder,
    max_attempts: int,
    repair_enabled: bool,
) -> UpdateResult:
    """Call the provider and validate, with a bounded repair loop.

    The repair builder is injected (rather than imported) so this function is
    testable with a stub and so it has no opinion about prompt text.
    """
    raw = ""
    reason: str | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        attempt_prompt = (
            prompt if attempt == 1 else build_repair_prompt(prompt, raw, reason or "")
        )
        try:
            raw = call_provider(provider, attempt_prompt)
            parsed = parse_update(raw)
            return UpdateResult(raw_output=raw, parsed=parsed, reason=None)
        except StateUpdateError as exc:
            # Provider-level: not repairable.
            return UpdateResult(raw_output=raw, parsed=None, reason=str(exc))
        except (ValidationError, json.JSONDecodeError, StateSchemaError) as exc:
            reason = _describe_error(exc)
            if not repair_enabled:
                break  # repair disabled -> FAILED after the first bad output
        except Exception as exc:  # noqa: BLE001 - never crash the ingest path
            logger.exception("unexpected failure while updating meeting state")
            return UpdateResult(
                raw_output=raw,
                parsed=None,
                reason=f"unexpected updater error: {type(exc).__name__}",
            )

    return UpdateResult(raw_output=raw, parsed=None, reason=reason or "unknown error")


__all__ = [
    "MAX_RAW_OUTPUT_CHARS",
    "RepairPromptBuilder",
    "StateSchemaError",
    "StateUpdateError",
    "UpdateResult",
    "call_provider",
    "parse_update",
    "run_update",
]
