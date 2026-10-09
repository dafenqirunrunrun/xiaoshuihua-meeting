"""水华队长 orchestration (ADR-002).

The referee is an ON-DEMAND consumer of persisted FINAL. It is NOT part of the
meeting-state pipeline and shares nothing with it: no schema, no scheduler, no
lock, no table. The only thing the two have in common is that both read
`transcript_segment`, read-only.

Pipeline shape (Phase A / B / C — same discipline as the Summary and
Meeting State pipelines, for the same reasons)

    Phase A  (meeting_lock, short)   dedupe -> throttle -> INSERT PENDING -> commit
    Phase B  (NO lock, NO txn)       read window -> prompt -> LLM -> Pydantic -> evidence
    Phase C  (meeting_lock, short)   re-read row -> write terminal state -> commit

The separation is load-bearing, not stylistic. `meeting_lock` is the SAME lock
FINAL ingest and the STREAM_END integrity check use; holding it across a
60-second provider call would stall transcript commit for this meeting, which is
the one thing the referee must never do (ADR-002 §10). So the LLM call happens
strictly between two short critical sections.

Failure policy (mirrors `meeting_state/updater.py`)

  `CaptainError`        provider-level -> NOT repairable, terminal FAILED now
  `CaptainSchemaError`  shape-level    -> repairable, bounded by settings

A failure never leaves a partial report: only `status` and `last_error` move,
`report_json` stays exactly as it was (NULL). The reader sees a complete report
or an honest failure, never half of one.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from threading import RLock

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.captain import context as captain_context
from app.captain import evidence as captain_evidence
from app.captain import external_evidence as captain_external
from app.captain import repository as captain_repository
from app.captain.model import (
    STATUS_PENDING,
    CaptainReport as CaptainReportRow,
)
from app.captain.prompts import (
    build_deep_prompt,
    build_quick_prompt,
    build_repair_prompt,
    build_report_prompt,
)
from app.captain.search import service as search_service
from app.captain.schemas import (
    CaptainReportContent,
    CaptainReportPublic,
    DeepReportContent,
    QuickAssessment,
    PointItem,
)
from app.common.exceptions import (
    InvalidMeetingStateTransition,
    MeetingNotFoundError,
)
from app.common.meeting_lock import meeting_lock
from app.config import settings
from app.meeting.meeting_model import Meeting
from app.transcript import repository as transcript_repository

logger = logging.getLogger(__name__)

# Meeting statuses that may be refereed. Mirrors the transcript ingest gate:
# CREATED has nothing to analyse, COMPLETED is a finished meeting. Note that
# this is a READ gate only — an already-generated report stays readable after
# the meeting completes, exactly like the frozen transcript does.
REFEREEABLE_STATUSES = frozenset({"IN_PROGRESS", "FINALIZING"})

# Max raw output accepted before we treat it as runaway/malformed.
MAX_RAW_OUTPUT_CHARS = 64_000

# Throttle state is PROCESS-LOCAL and deliberately non-durable: it exists to
# stop a user hammering the button, not to enforce a contract. A restart
# forgetting it is correct. Same stance as `_last_summary_refresh` in
# meeting_state/service.py.
_throttle_lock = RLock()
_last_trigger_at: dict[str, float] = {}


class CaptainError(Exception):
    """A provider-level failure (NOT repairable) or an unusable window."""


class CaptainSchemaError(Exception):
    """A structured-output shape failure not already covered by Pydantic."""


def reset_throttle(meeting_id: str | None = None) -> None:
    """Clear process-local throttle state (meeting end, shutdown, tests)."""
    with _throttle_lock:
        if meeting_id is None:
            _last_trigger_at.clear()
        else:
            _last_trigger_at.pop(meeting_id, None)


def _throttle_remaining(meeting_id: str) -> float:
    """Seconds left on the throttle window (0 when a trigger is allowed)."""
    interval = max(0.0, settings.captain_min_interval_seconds)
    if interval <= 0:
        return 0.0
    with _throttle_lock:
        last = _last_trigger_at.get(meeting_id)
    if last is None:
        return 0.0
    return max(0.0, interval - (time.monotonic() - last))


def _record_trigger(meeting_id: str) -> None:
    with _throttle_lock:
        _last_trigger_at[meeting_id] = time.monotonic()


# --- parse gate -------------------------------------------------------------

def _prune_code_fence(text: str) -> str:
    """Strip a ```json ... ``` wrapper if the model added one (lenient)."""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return f"validation failed: {exc.errors()[:3]}"
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}"
    return f"schema: {exc}"


def parse_report(raw: str) -> CaptainReportContent:
    """JSON-decode + Pydantic-validate. Validation IS the success criterion.

    Raises json.JSONDecodeError / ValidationError / CaptainSchemaError. There is
    deliberately no fallback to a default report: a silently-empty report would
    look like "the referee found nothing to say" and be indistinguishable from a
    successful analysis, which is exactly the confusion this feature cannot
    afford.
    """
    return _parse_as(CaptainReportContent, raw)


def _parse_as(model, raw: str):
    """The shared parse gate. `model` is the Pydantic class the JSON must satisfy."""
    pruned = _prune_code_fence(raw).strip()
    data = json.loads(pruned)  # JSONDecodeError propagates
    if not isinstance(data, dict):
        raise CaptainSchemaError(f"expected a JSON object, got {type(data).__name__}")
    return model.model_validate(data)


def _call_provider(provider, prompt: str, timeout_seconds: float | None = None) -> str:
    """One provider call with a hard cap on the raw output.

    `timeout_seconds` defaults to the report timeout and is overridden for the
    Quick pass, which has its own much tighter bound. It is passed through
    explicitly rather than read from settings here so the call path cannot
    silently pick up the wrong budget when a second stage is added.
    """
    from app.agent.llm.base import LLMConfigurationError, SummaryGenerationError

    try:
        raw = provider.complete(
            prompt,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else settings.captain_llm_timeout_seconds
            ),
        )
    except (SummaryGenerationError, LLMConfigurationError) as exc:
        # Unknown-provider misconfiguration folds in here on purpose: it is a
        # provider-level failure, so the report is left empty rather than being
        # filled from a fallback.
        raise CaptainError(str(exc)) from exc

    if not raw or not raw.strip():
        raise CaptainError("provider returned empty output")
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise CaptainError(
            f"provider output too large ({len(raw)} chars > {MAX_RAW_OUTPUT_CHARS})"
        )
    return raw


def _generate(
    provider,
    *,
    prompt: str,
    max_attempts: int,
    repair_enabled: bool,
    model=None,
    timeout_seconds: float | None = None,
) -> tuple[CaptainReportContent | DeepReportContent | QuickAssessment, str]:
    """Call the provider and validate, with a bounded repair loop.

    Returns (content, model_name). Provider-level failures are NOT retried —
    retrying a broken transport only burns latency and delays an honest FAILED.

    `model` selects the parse gate. Three callers share this loop — the legacy
    single-pass report (`CaptainReportContent`), the Deep pass
    (`DeepReportContent`) and the Quick pass (`QuickAssessment`) — and the only
    thing that differs between them is the schema the raw JSON must satisfy.
    Parameterising is what makes the repair path identical for all three; three
    copies of this loop would be three places for the retry/repair policy to
    drift.
    """
    schema = model or CaptainReportContent
    raw = ""
    reason: str | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        attempt_prompt = (
            prompt if attempt == 1 else build_repair_prompt(prompt, raw, reason or "")
        )
        try:
            raw = _call_provider(provider, attempt_prompt, timeout_seconds)
            return _parse_as(schema, raw), getattr(provider, "name", None) or "unknown"
        except CaptainError:
            raise
        except (ValidationError, json.JSONDecodeError, CaptainSchemaError) as exc:
            reason = _describe_error(exc)
            if not repair_enabled:
                break
        except Exception as exc:  # noqa: BLE001 - never crash the worker
            logger.exception("unexpected failure while generating captain report")
            raise CaptainError(f"unexpected error: {type(exc).__name__}") from exc

    raise CaptainError(reason or "unknown error")


def _assemble_staged_content(
    deep_result: DeepReportContent,
    *,
    quick: QuickAssessment | None,
) -> CaptainReportContent:
    """Fold one Deep result into the stored report shape. ONE derivation site.

    The Deep pass returns the judgement material (`deep`) plus the two fields
    that describe the whole window (`discussion_summary`, `agreement_points`,
    `uncertainty`). The flat fields the evidence validator and the UI read are
    derived HERE, from that single output, rather than being asked for twice:

      positions       <- deep.claims
      evidence        <- deep.evidence
      suggestion      <- deep.recommendation
      dispute_points  <- the claims, ONLY when there is more than one

    That last rule is the same structural guarantee `dispute_points` has always
    had (ADR-002 禁止事项 §7): a single claim is a position, not a dispute, and
    manufacturing a disagreement out of it is the error the schema was designed
    to make impossible.

    `uncertainty` gains the missing-evidence summary because a reader looking at
    the flat report must see the information gap without expanding `deep`. It is
    appended, not replaced: the Deep pass's own uncertainty wording is the model
    speaking about its confidence, and the list is mechanical.
    """
    content = CaptainReportContent(
        discussion_summary=deep_result.discussion_summary,
        positions=deep_result.deep.claims,
        agreement_points=deep_result.agreement_points,
        evidence=deep_result.deep.evidence,
        uncertainty=deep_result.uncertainty,
        suggestion=deep_result.deep.recommendation,
        quick=quick,
        deep=deep_result.deep,
    )
    if len(deep_result.deep.claims) > 1:
        content = content.model_copy(
            update={
                "dispute_points": [
                    PointItem(
                        text=c.statement, source_segment_seqs=c.source_segment_seqs
                    )
                    for c in deep_result.deep.claims
                ]
            }
        )
    if deep_result.deep.missing_evidence:
        gap = "；".join(m.text for m in deep_result.deep.missing_evidence[:3])
        content = content.model_copy(
            update={"uncertainty": f"{content.uncertainty}（尚缺证据：{gap}）"}
        )
    return content


def _quick_provider(provider):
    """Resolve the tighter-budget provider for the Quick pass.

    Two cases, and the fallback is the important one:

      * An INJECTED provider (tests, and any caller that passed one explicitly)
        is used as-is. Tests pin a deterministic provider and must keep getting
        exactly it — silently swapping in a smaller-budget real provider would
        make the offline suite depend on the network.
      * Otherwise the configured provider is resolved a second time with the
        Quick output budget and timeout. `get_provider` caches by config tuple,
        so this is a cache lookup after the first analysis rather than a new
        client per report (see `agent/llm/factory.py`).

    A resolution failure (unknown LLM_PROVIDER) degrades Quick rather than
    raising: the Deep pass has its own provider and must still run.
    """
    if settings.llm_provider != "deepseek" or provider is None:
        return provider
    from app.agent.llm.factory import get_provider

    try:
        return get_provider(
            max_output_tokens=settings.captain_quick_max_output_tokens,
            thinking_enabled=False,
        )
    except Exception as exc:  # noqa: BLE001 - Quick must never sink the report
        logger.info("captain quick provider unavailable, degrading: %s", str(exc)[:200])
        return None


def _deep_provider(provider):
    """Resolve the provider for the Deep pass.

    Mirrors `_quick_provider`, including the injected-provider short-circuit and
    for the same reason. The Deep budget is separate from (and larger than)
    `captain_max_output_tokens` because the Deep schema is structurally bigger
    than the flat report — a measured run had its JSON cut off mid-string at
    2000 tokens while the model was still writing `citations`.

    Falls back to the caller's provider when the configured one cannot be
    resolved, so an analysis still runs rather than failing on a config typo.
    """
    if settings.llm_provider != "deepseek" or provider is None:
        return provider
    from app.agent.llm.factory import get_provider

    try:
        return get_provider(
            max_output_tokens=settings.captain_deep_max_output_tokens,
            thinking_enabled=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("captain deep provider unavailable, using resolved: %s", str(exc)[:200])
        return provider


def _resolve_search_provider(search_provider=None):
    """The web-search provider for this analysis, or None.

    A single named seam rather than an inline `None` argument at the call site.
    The call site passes `None` and `search/service.enrich` resolves the provider
    itself, which means the only way to substitute one in a test was to patch the
    factory — a patch that sits one module away from the code under test and
    silently stops working if resolution is ever moved. Naming the decision here
    keeps the injection point visible and gives the call site one thing to pass.
    """
    if search_provider is not None:
        return search_provider
    return None


def _generate_two_stage(
    provider,
    *,
    meeting_title: str,
    window,
    window_seconds: float,
    on_quick=None,
    enrich_fn=None,
) -> tuple[CaptainReportContent, str, dict, object]:
    """Run Quick then Deep. Returns (content, model_name, timings, enrichment).

    Two properties are load-bearing and neither is an optimisation:

    **Quick failure cannot sink Deep.** The first pass is an early-answer
    convenience, so every failure mode of it — provider error, bad JSON, repair
    exhausted — is caught and DEGRADED to "no quick result", with the Deep pass
    proceeding on a prompt without the provisional block. The opposite design
    (a shared try/except around both calls) would mean a fast pass that timed
    out costs the user the whole report, which inverts the intended trade.

    **Quick never touches the network, and this function is what guarantees
    it.** Nothing is passed in for Quick to search with: external evidence does
    not exist yet when Quick runs, and it is produced by `enrich_fn` only AFTER
    Quick has been returned to the caller (`on_quick`). A research call in front
    of Quick would put an unbounded web request on the critical path of a 1-2
    second target, and no amount of tuning the search timeout would fix an
    ordering bug — which is why the ordering is enforced HERE, structurally,
    rather than by convention at the call site. The Quick prompt is built with
    no `<external_evidence>` block by construction.

    `enrich_fn` is a THUNK, not a value, and `None` means "no external evidence
    at all" (the single-pass path's arrangement, and the test path). Deferring
    the call is the whole point: `EnrichmentResult` also drives the metrics line
    and the disclosure note, so the caller needs the object whether or not Deep
    is reached — but it must be obtained after Quick, and a thunk is the only
    way to express "after" without duplicating the ordering rule at the call
    site. A failure inside it is the caller's to degrade (`_safe_enrich`), never
    this function's to propagate: no search outcome may cost the report.

    **Deep is not skippable.** If it fails, the whole report FAILS. Quick alone
    is a directional opinion, and storing one as if it were the finished report
    would be exactly the "looks complete, is not" outcome the schema and the
    evidence validator exist to prevent.

    `on_quick` is called with the parsed Quick result the moment it exists and
    BEFORE Deep starts. That ordering is the entire point: it is what lets the
    caller publish a first answer while the second is still being computed. A
    callback failure is swallowed here (it is a delivery concern, not a
    computational one) so a broken subscriber can never cost the Deep pass.
    """
    max_attempts = settings.captain_max_attempts
    repair_enabled = settings.captain_parse_repair
    timings: dict = {}

    t0 = time.monotonic()
    quick: QuickAssessment | None = None
    quick_provider = _quick_provider(provider)
    if quick_provider is not None:
        try:
            quick, _ = _generate(
                quick_provider,
                prompt=build_quick_prompt(
                    meeting_title=meeting_title,
                    window=window,
                    window_seconds=window_seconds,
                    # Deliberately NOT `external_evidence`: see the docstring.
                    external_evidence=None,
                ),
                max_attempts=max_attempts,
                repair_enabled=repair_enabled,
                model=QuickAssessment,
                timeout_seconds=settings.captain_quick_timeout_seconds,
            )
            timings["quick_ok"] = True
        except CaptainError as exc:
            # Degrade, do not propagate. The reason is logged for operators; the
            # report itself does not carry it, because a missing first pass is
            # not a defect the reader needs to act on.
            timings["quick_ok"] = False
            logger.info(
                "captain quick pass degraded (report continues): %s", str(exc)[:200]
            )
    else:
        timings["quick_ok"] = False
    timings["quick_ms"] = int((time.monotonic() - t0) * 1000)
    timings["quick_completed_at"] = time.time()

    if quick is not None and on_quick is not None:
        try:
            on_quick(quick)
        except Exception:  # noqa: BLE001 - delivery must not cost the Deep pass
            logger.exception("captain quick publish callback failed")

    # ---- the ordering boundary: Quick is done, so research may start --------
    # Everything above this line has already been delivered to the client. The
    # search budget — now measured in seconds, not milliseconds — is spent here
    # and nowhere earlier, which is what keeps `trigger_to_quick_ms` independent
    # of the web.
    t_research = time.monotonic()
    external_evidence: list = []
    enrichment = search_service.EnrichmentResult()
    if enrich_fn is not None:
        enrichment = enrich_fn()
        external_evidence = captain_external.to_external_evidence(
            enrichment.evidence
        )
    timings["research_ms"] = int((time.monotonic() - t_research) * 1000)

    t1 = time.monotonic()
    deep, model_name = _generate(
        _deep_provider(provider),
        prompt=build_deep_prompt(
            meeting_title=meeting_title,
            window=window,
            window_seconds=window_seconds,
            external_evidence=external_evidence,
            quick=quick,
        ),
        max_attempts=max_attempts,
        repair_enabled=repair_enabled,
        model=DeepReportContent,
    )
    timings["deep_ms"] = int((time.monotonic() - t1) * 1000)
    timings["deep_ok"] = True
    timings["deep_completed_at"] = time.time()

    # Always present, not only when True. Telemetry consumers graph "how often
    # does Deep move the Quick judgement?", and a key that is absent in the
    # common case forces every consumer to treat missing as "false" — which is
    # exactly how a field gets mis-summed the first time it is aggregated.
    timings["superseded_quick"] = bool(
        quick is not None and deep.deep.supersedes.strip()
    )

    content = _assemble_staged_content(deep, quick=quick)
    return content, model_name, timings, enrichment


# --- trigger (Phase A) ------------------------------------------------------

def _resolve_anchor(
    db: Session, meeting_id: str, anchor: int | None
) -> tuple[int, bool]:
    """The FINAL that triggers this report, plus whether to exclude it.

    Returns `(anchor_segment_seq, exclude_anchor)`. The two are produced together
    because only this function knows WHY the anchor is what it is, and the answer
    decides whether the anchor is discussion content:

      - `anchor` supplied  -> the caller named a specific FINAL. The wake-word
        path names the segment that SPOKE the wake word, which is not content
        and must be excluded. (`exclude_anchor=True`)
      - `anchor` omitted   -> the button path; we take the persisted high-water
        mark, which is an ordinary participant utterance — usually the newest
        and most relevant one. Excluding it drops the last thing said, which is
        exactly the bug this flag fixes. (`exclude_anchor=False`)

    Callers that pass an explicit anchor for a NON-wake reason (a replayed
    backfill, a client that re-triggers a known report) get the stricter
    behaviour; that is the safe default and matches the pre-existing contract.

    Falls back to the persisted high-water mark because that is the segment the
    user was reacting to. A meeting with no FINAL at all cannot be refereed —
    reported as a validation error rather than creating a report that can only
    ever fail.
    """
    if anchor is not None:
        return anchor, True
    return (transcript_repository.max_seq(db, meeting_id) or 0), False


def trigger(
    db: Session,
    meeting_id: str,
    *,
    anchor_segment_seq: int | None = None,
) -> tuple[CaptainReportRow, bool]:
    """Create (or reuse) a report request. Returns (row, created).

    `created=False` means the anchor was already analysed and the EXISTING row
    is returned with ZERO LLM spend — the same idempotency contract the
    transcript FINAL path uses (`FinalApplied` / `None`).

    Raises MeetingNotFoundError(404) / InvalidMeetingStateTransition(409) /
    CaptainError for an unusable trigger. Throttling is enforced by the caller
    so the router can map it to 429.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    if meeting.status not in REFEREEABLE_STATUSES:
        raise InvalidMeetingStateTransition(
            f"meeting {meeting_id} status {meeting.status} cannot be refereed "
            f"(only {sorted(REFEREEABLE_STATUSES)})"
        )

    anchor, exclude_anchor = _resolve_anchor(db, meeting_id, anchor_segment_seq)
    if anchor <= 0:
        raise CaptainError("meeting has no persisted FINAL transcript to analyse")

    # Dedupe + insert share the meeting lock with FINAL ingest. This is what
    # makes "check then insert" safe: without it two simultaneous triggers could
    # both miss the existing row and race on the unique constraint.
    with meeting_lock(meeting_id):
        existing = captain_repository.get_by_anchor(db, meeting_id, anchor)
        if existing is not None:
            return existing, False
        row = captain_repository.insert_pending(
            db,
            report_id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            anchor_segment_seq=anchor,
            exclude_anchor=exclude_anchor,
        )
    _record_trigger(meeting_id)
    return row, True


def throttle_remaining(meeting_id: str) -> float:
    """Public read of the throttle, for the router's 429 decision."""
    return _throttle_remaining(meeting_id)


# --- analysis (Phase B + C) -------------------------------------------------

def run_analysis(
    meeting_id: str,
    report_id: str,
    *,
    provider=None,
    search_provider=None,
) -> str:
    """Execute one report. Runs on a worker thread with its OWN session.

    Returns an outcome code (logged and asserted in tests). NEVER raises: a
    failure is recorded on the row. The referee's problems must not be able to
    reach the transcript path or the worker loop.

    `search_provider` is injectable for the same reason `provider` is: tests pin
    a deterministic provider, and the Phase 3-2E harness swaps Brave for Exa per
    arm. Omitting it resolves the configured one inside `enrich`.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        row = captain_repository.get_by_report_id(db, report_id)
        if row is None:
            return "REPORT_NOT_FOUND"
        if not captain_repository.claim_for_analysis(db, row):
            # Already terminal, or another run claimed it.
            return "SUPERSEDED"

        try:
            analysis_started = time.monotonic()
            if provider is None:
                from app.agent.llm.factory import get_provider

                provider = get_provider(
                    max_output_tokens=settings.captain_max_output_tokens,
                    # A referee report is a bounded structured JSON object, not a
                    # reasoning transcript. With the current DeepSeek gateway the
                    # implicit thinking pass bills to the same max_tokens budget,
                    # so a reasoning-heavy window (a real multi-party discussion)
                    # spends all CAPTAIN_MAX_OUTPUT_TOKENS on reasoning_tokens
                    # and emits no content at all -> empty output -> FAILED.
                    # summary_service.py and meeting_state/service.py both pass
                    # this for the same reason; captain was the one call site
                    # that missed it.
                    thinking_enabled=False,
                )

            # ---- Phase B: no lock, no open transaction ----------------------
            segments = transcript_repository.list_sorted(db, meeting_id)
            # Exclusion is a property of the TRIGGER, not of the anchor field:
            # a wake-word anchor IS the wake phrase and must stay out, while a
            # button anchor is a plain high-water mark whose content is usually
            # the most relevant thing said. See captain/context.py.
            window = captain_context.select_window(
                segments,
                anchor_segment_seq=row.anchor_segment_seq,
                window_seconds=settings.captain_window_seconds,
                max_chars=settings.captain_max_context_chars,
                min_segments=settings.captain_min_segments,
                exclude_anchor=row.exclude_anchor,
            )
            if window.is_empty:
                return _fail(
                    db, row,
                    "window has insufficient transcript to analyse "
                    f"(anchor={row.anchor_segment_seq}, "
                    f"need>={settings.captain_min_segments} segments)",
                )

            # External evidence (Phase 3-2 / Phase C.5). The policy runs BEFORE
            # the LLM and only over the window text: it is a deterministic rule
            # set, not a second model call, so it costs microseconds and can
            # never fail the report (see `search/service.py`). `enrich` is total
            # — a disabled feature, a NO_SEARCH verdict, a timeout and a
            # provider error all come back as a normal `EnrichmentResult` with
            # this analysis's metrics attached.
            #
            # WHERE this runs is the Phase C.5 decision, so it is expressed as a
            # function rather than called here:
            #
            #   two-stage ON   the search happens INSIDE `_generate_two_stage`,
            #                  after Quick has been published. Quick's 1-2s
            #                  target is then independent of the web, because
            #                  the network call has not started yet when Quick
            #                  returns. This is the whole point of the phase.
            #   two-stage OFF  the single-pass referee needs its evidence
            #                  BEFORE its one LLM call, so it runs here.
            #
            # Both paths get the same object from the same code, and `enrich_fn`
            # is what lets that be true without the two-stage path calling it
            # early to satisfy a shared variable.
            def _enrich():
                return _safe_enrich(
                    [(seg.segment_seq, seg.text) for seg in window.segments],
                    provider=_resolve_search_provider(search_provider),
                )

            # The trigger timestamp is read from the DURABLE row, not taken at
            # process start: a report can wait in the queue, and the wait is
            # exactly the part the two-stage latency budget must account for.
            # `created_at` is written by `insert_pending`, so this measures from
            # the moment the user asked for a referee.
            trigger_at = _created_at_epoch(row)
            timings: dict | None = None
            llm_started = time.monotonic()
            if settings.captain_two_stage_enabled:
                quick_started_at = time.time()
                content, model_name, timings, enrichment = _generate_two_stage(
                    provider,
                    meeting_title=_meeting_title(db, meeting_id),
                    window=window,
                    window_seconds=settings.captain_window_seconds,
                    # Publish the first pass the moment it exists, then let Deep
                    # keep running. Best-effort inside `_publish_quick`.
                    on_quick=lambda quick: _publish_quick(
                        meeting_id, report_id, quick
                    ),
                    # Deferred, not pre-computed: it is called after Quick has
                    # already been delivered. See `_generate_two_stage`.
                    enrich_fn=_enrich,
                )
                timings.setdefault("quick_started_at", quick_started_at)
            else:
                enrichment = _enrich()
                external = captain_external.to_external_evidence(
                    enrichment.evidence
                )
                prompt = build_report_prompt(
                    meeting_title=_meeting_title(db, meeting_id),
                    window=window,
                    window_seconds=settings.captain_window_seconds,
                    external_evidence=external,
                )
                content, model_name = _generate(
                    provider,
                    prompt=prompt,
                    max_attempts=settings.captain_max_attempts,
                    repair_enabled=settings.captain_parse_repair,
                )
            llm_latency_ms = int((time.monotonic() - llm_started) * 1000)

            # ONE conversion, used for BOTH the prompt and the report. That is
            # what makes the `[web_n]` ids the model reads identical to the ones
            # stored: a second conversion could renumber, and the model's
            # citations would silently point at the wrong source (see
            # `external_evidence.py`). On the two-stage path the conversion has
            # already happened inside `_generate_two_stage`, so the report gets
            # the SAME objects the prompt did rather than a second conversion of
            # the same inputs.
            external = captain_external.to_external_evidence(enrichment.evidence)

            # Deterministic grounding check — never a model call.
            validated = captain_evidence.validate_evidence(content, window.segments)
            content = _with_notes(validated, extra_note=enrichment.note)

            # The external evidence list is written by the PIPELINE, after the
            # model has spoken, from the provider results actually returned —
            # NOT read out of the model's output. The model was shown `[web_1]`
            # blocks and may refer to them, but a hallucinated `web_9` has
            # nowhere to land: this overwrite is that guarantee, in one line.
            # Order matters: it happens AFTER `_with_notes` so a model-invented
            # entry can never ride along inside the validated content.
            #
            # `research_status` rides in the same overwrite for the same reason:
            # it describes what the RESEARCH did, which the model has no way to
            # know and must not be able to assert.
            content = content.model_copy(
                update={
                    "external_evidence": external,
                    "research_status": enrichment.research_status,
                }
            )

            _log_metrics(
                report_id,
                enrichment.metrics,
                research_status=enrichment.research_status,
                llm_latency_ms=llm_latency_ms,
                total_latency_ms=int((time.monotonic() - analysis_started) * 1000),
                timings=timings,
                trigger_at=trigger_at,
                deep_completed_at=time.time(),
            )

            # ---- Phase C: short critical section ---------------------------
            with meeting_lock(meeting_id):
                fresh = captain_repository.get_by_report_id(db, report_id)
                if fresh is None:
                    return "REPORT_NOT_FOUND"
                written = captain_repository.mark_completed(
                    db,
                    fresh,
                    content=content.model_dump(),
                    model_name=model_name,
                    window_from_seq=window.from_seq,
                    window_to_seq=window.window_to_seq,
                )
            if written is None:
                return "SUPERSEDED"
            # AFTER the commit, never inside the lock: the report is durable, so
            # a slow/failed fan-out can neither roll it back nor hold the lock
            # FINAL ingest needs.
            _publish_completed(meeting_id, to_public(written))
            return "OK"
        except CaptainError as exc:
            return _fail(db, row, str(exc))
        except Exception as exc:  # noqa: BLE001 - worker must survive anything
            logger.exception("captain analysis failed for report %s", report_id)
            return _fail(db, row, f"unexpected error: {type(exc).__name__}")
    finally:
        db.close()


def run_analysis_failure(db: Session, report_id: str, reason: str) -> None:
    """Fail a report that never reached the worker (queue full, disabled).

    Public entry so the router can resolve a PENDING row it just created when
    dispatch is impossible. Without this the row would sit PENDING forever and
    the client would poll a spinner that can never finish.

    Also fans out captain.failed: a client that saw its 202 (or an earlier
    reconnect) must learn the report died without polling forever. The 503 tells
    the CALLER; the event tells every OTHER open tab on this meeting.
    """
    row = captain_repository.get_by_report_id(db, report_id)
    if row is None:
        return
    written = captain_repository.mark_failed(db, row, reason=reason)
    if written is not None:
        _publish_failed(meeting_id=written.meeting_id, row=written)


def _with_notes(
    validated: captain_evidence.EvidenceValidation,
    extra_note: str = "",
) -> CaptainReportContent:
    """Fold evidence-pruning notes and the search disclosure into `uncertainty`.

    The user is told the grounding is partial instead of being shown a report
    whose removed citations are silently gone. This is the report's own field,
    so it costs no schema change.

    `extra_note` (Phase 3-2) carries the search-degradation sentence — "本次外部
    资料未能在时间预算内完成检索…". It rides the same channel for the same
    reason: the report must be honest about what it did NOT have.
    """
    notes = list(validated.notes)
    if extra_note:
        notes.append(extra_note)
    if not notes:
        return validated.content
    suffix = "；".join(notes)
    return validated.content.model_copy(
        update={"uncertainty": f"{validated.content.uncertainty}（{suffix}）"}
    )


def _log_metrics(
    report_id: str,
    metrics,
    *,
    research_status: str,
    llm_latency_ms: int,
    total_latency_ms: int,
    timings: dict | None = None,
    trigger_at: float | None = None,
    deep_completed_at: float | None = None,
) -> None:
    """Emit one structured line of A/B + latency telemetry per analysis.

    A SINGLE JSON line on the `app.captain` logger, NOT a `captain_report`
    column. Two reasons, and the second is decisive:

      1. `create_all()` never ALTERs an existing SQLite table, so a new column
         would silently be absent on every deployed database — telemetry that
         exists only on freshly-created ones is worse than none.
      2. These are ENGINEERING fields (provider, cache hit, query count, stage
         latencies). The report body is the product's data, read back as
         `report_json` and rendered; mixing instrumentation into it would make
         the two impossible to version separately.

    Latency instrumentation (Task 7)
    --------------------------------
    The two-stage design is judged on two numbers, so both are emitted as
    DERIVED milliseconds rather than left for a log reader to subtract:

      trigger_to_quick_ms   what the user waits for the first answer
      trigger_to_deep_ms    what the user waits for the full report

    They are derived from the four absolute wall-clock stamps the spec names
    (`trigger_at`, `quick_started_at`, `quick_completed_at`, `deep_completed_at`)
    because absolute stamps are what make a gap ATTRIBUTABLE — `quick_ms` alone
    cannot distinguish a slow model from a report that waited in the queue, and
    the two have opposite fixes. The absolute stamps ride along in the payload;
    the derived values are what an operator actually graphs.

    `trigger_at` comes from the durable row's `created_at`, not from this
    process, for exactly that reason.

    Phase 3-2E reads this line to compare Brave against Exa. If a future phase
    wants it queryable, that is a deliberate schema migration, not an accident
    of `extra="forbid"` accidentally admitting a field.
    """
    payload = metrics.model_dump()
    payload["report_id"] = report_id
    # The one Phase C.5 telemetry field that is NOT on `SearchMetrics`, because
    # it belongs to the RESEARCH OUTCOME rather than to the provider call: it is
    # the same value the report body carries, passed in from the `EnrichmentResult`
    # so the log line and the durable report cannot disagree about whether the
    # research completed. It graphed alongside `research_needed` (the plan) and
    # `search_query_count` (the attempt) — the three together are what Phase D
    # reads to separate "the policy never asked" from "we asked and the web did
    # not answer".
    payload["research_status"] = research_status
    payload["captain_llm_latency_ms"] = llm_latency_ms
    payload["captain_total_latency_ms"] = total_latency_ms
    if timings:
        payload.update(timings)
    if deep_completed_at is not None:
        payload["deep_completed_at"] = deep_completed_at
    # `trigger_at` arrives as an argument rather than through `timings`, so it is
    # placed before the rounding pass below.
    if trigger_at is not None:
        payload["trigger_at"] = trigger_at
    # The four absolute stamps are floated through JSON, so they are rounded to
    # MILLISECONDS *before* anything is derived from them. Two reasons, and the
    # second is the load-bearing one:
    #
    #   1. Sub-millisecond precision in a latency stamp is noise being carried in
    #      a log line. Milliseconds are the unit every consumer graphs in.
    #   2. The derived values below must be reproducible from the payload by a
    #      reader who only has the payload — "trigger_to_quick_ms equals
    #      quick_completed_at minus trigger_at, times 1000" is the whole point of
    #      publishing both. Deriving from the unrounded floats makes that
    #      identity false by up to a millisecond (the subtraction is exact, the
    #      multiplication and truncation are not), so the log line would
    #      occasionally contradict itself. Rounding once, here, makes the
    #      relationship exact by construction rather than by luck.
    for stamp in ("trigger_at", "quick_started_at", "quick_completed_at", "deep_completed_at"):
        value = payload.get(stamp)
        if isinstance(value, (int, float)):
            payload[stamp] = round(float(value), 3)
    if payload.get("trigger_at") is not None:
        trigger_at_value = payload["trigger_at"]
        for stamp, key in (
            ("quick_completed_at", "trigger_to_quick_ms"),
            ("deep_completed_at", "trigger_to_deep_ms"),
        ):
            value = payload.get(stamp)
            if isinstance(value, (int, float)):
                payload[key] = int((value - trigger_at_value) * 1000)
    logger.info(
        "captain_search_metrics %s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def _safe_enrich(segments, *, provider=None):
    """`search_service.enrich` with a total-failure guard on the CALLER's side.

    `enrich` is written to be total — it documents that it cannot make a report
    fail, and it keeps that promise for every failure mode it can see. This
    wrapper exists for the one it cannot: a bug inside `enrich` itself, a future
    provider that raises past `_safe_search`, or an import-time problem in the
    search package. The spec is unconditional ("检索失败/超时必须不能让整个队长
    失败"), and an unconditional requirement cannot rest on the callee's internal
    discipline — that is the same reasoning `_safe_search` already applies one
    level down ("the guard belongs on the caller's side of the contract, not on
    the implementer's honour").

    Returns an EMPTY enrichment on failure, which is the correct degraded state:
    no external evidence, no note. Deliberately not a disclosure note — a crash
    inside our own search layer is an operator's problem, and telling the reader
    "本次外部资料检索未完成" would present an internal defect as a routine
    timeout. The exception is logged at ERROR so it is not silent.
    """
    try:
        return search_service.enrich(segments, provider=provider)
    except Exception:  # noqa: BLE001 - search must never fail the referee
        logger.exception("captain search enrichment crashed; continuing without it")
        return search_service.EnrichmentResult()


def _created_at_epoch(row) -> float | None:
    """The row's `created_at` as a POSIX timestamp, or None when unparseable.

    Returns None rather than raising or defaulting to "now": a missing stamp
    would make `trigger_to_quick_ms` silently measure from the wrong instant, and
    a wrong latency number is worse than an absent one. Telemetry must never be
    able to break an analysis.
    """
    raw = getattr(row, "created_at", None)
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # SQLite stores a naive UTC string (see `captain/repository.py`).
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _publish_quick(meeting_id: str, report_id: str, quick: QuickAssessment) -> None:
    """Fan out captain.quick_assessment. Best-effort, never fails the analysis.

    Published while the row is STILL ANALYZING — see
    `sse_service.publish_captain_quick_assessment` for why that is correct
    rather than a commit-before-publish violation. Swallowed here for the same
    reason `_publish_completed` swallows: a delivery problem must not be recorded
    as the report's outcome.
    """
    try:
        from app.realtime import sse_service

        sse_service.publish_captain_quick_assessment(
            meeting_id, report_id=report_id, quick=quick.model_dump()
        )
    except Exception:  # noqa: BLE001 - SSE must never affect the report outcome
        logger.exception("captain.quick_assessment publish failed for %s", report_id)


def _fail(db: Session, row: CaptainReportRow, reason: str) -> str:
    """Record a terminal failure. Never raises, never touches report_json.

    `mark_failed` returns None when the row is ALREADY terminal, and that is not
    a no-op we can ignore: publishing unconditionally would emit a second
    captain.failed for one report, so a client showing "referee failed" would
    re-announce it. Publish only when this call is the one that moved the row.
    """
    fresh = captain_repository.get_by_report_id(db, row.report_id)
    if fresh is None:
        return "REPORT_NOT_FOUND"
    written = captain_repository.mark_failed(db, fresh, reason=reason)
    logger.info("captain report %s FAILED: %s", row.report_id, reason[:200])
    if written is not None:
        _publish_failed(meeting_id=written.meeting_id, row=written)
    return "FAILED"


def _publish_completed(meeting_id: str, report: CaptainReportPublic) -> None:
    """Fan out captain.completed. Best-effort: a publish problem is NOT a failure.

    The report is already durable, so an exception here must not be recorded as
    the report's outcome — that would turn a delivery hiccup into a phantom
    FAILED for a report the user can actually read. Swallowed, like every other
    sidecar best-effort path (see `_maybe_trigger_captain` in transcript).
    """
    try:
        from app.realtime import sse_service

        payload = (report.report.model_dump() if report.report is not None else {})
        sse_service.publish_captain_completed(
            meeting_id,
            report={
                "report_id": report.report_id,
                "anchor_segment_seq": report.anchor_segment_seq,
                "positions": payload.get("positions", []),
                "dispute_points": payload.get("dispute_points", []),
                "uncertainty": payload.get("uncertainty", ""),
                # Phase C.5. `build_captain_completed` reads these two; they
                # were previously NOT passed, so the event reported the default
                # (`not_required` / 0) even when research had actually run. The
                # report body on the event is a SUBSET by design, but the
                # research OUTCOME must be truthful here: it is the only
                # fast-path signal a client has, and reading it beats
                # string-matching the Chinese disclosure sentence in
                # `uncertainty`. Both are defaulted so a pre-C.5 row (whose
                # `report_json` predates the fields) still yields a
                # well-formed event.
                "research_status": payload.get("research_status", "not_required"),
                "external_evidence": payload.get("external_evidence", []),
            },
        )
    except Exception:  # noqa: BLE001 - SSE must never affect the report outcome
        logger.exception("captain.completed publish failed for %s", report.report_id)


def _publish_failed(*, meeting_id: str, row: CaptainReportRow) -> None:
    """Fan out captain.failed. Best-effort, same contract as the completed twin.

    `last_error` is NOT forwarded: it exists for operators (router body / DB),
    and while it is already truncated, the SSE surface carries a fixed short
    code instead so a future change to the error text can never leak a provider
    detail into the browser.
    """
    try:
        from app.realtime import sse_service

        sse_service.publish_captain_failed(
            meeting_id,
            report_id=row.report_id,
            anchor_seq=row.anchor_segment_seq,
            reason="analysis failed",
        )
    except Exception:  # noqa: BLE001 - SSE must never affect the report outcome
        logger.exception("captain.failed publish failed for %s", row.report_id)


def _meeting_title(db: Session, meeting_id: str) -> str:
    meeting = db.get(Meeting, meeting_id)
    return meeting.title if meeting is not None else ""


# --- reads ------------------------------------------------------------------

def get_report(db: Session, report_id: str) -> CaptainReportPublic | None:
    """Projection for the read API. None when the report does not exist."""
    row = captain_repository.get_by_report_id(db, report_id)
    return to_public(row) if row is not None else None


def get_latest_report(db: Session, meeting_id: str) -> CaptainReportPublic | None:
    """Most recent report for a meeting — the reconnect/recovery read."""
    row = captain_repository.get_latest(db, meeting_id)
    return to_public(row) if row is not None else None


def to_public(row: CaptainReportRow) -> CaptainReportPublic:
    """Row -> product projection.

    A corrupt `report_json` degrades to FAILED rather than raising: the read API
    is what the product UI calls, and turning a data-integrity problem into a
    500 reads as "the app is broken". Same stance as
    `meeting_state.service.get_public_state`, and it does NOT write back — a GET
    must never launder a bad row.
    """
    status = row.status
    content: CaptainReportContent | None = None

    if status == "COMPLETED":
        decoded = row.report()
        if decoded is None:
            status = "FAILED"
        else:
            try:
                content = CaptainReportContent.model_validate(decoded)
            except ValidationError:
                logger.warning(
                    "captain report %s has unreadable report_json", row.report_id
                )
                status = "FAILED"
                content = None
    return CaptainReportPublic(
        report_id=row.report_id,
        meeting_id=row.meeting_id,
        status=status,
        anchor_segment_seq=row.anchor_segment_seq,
        window_from_seq=row.window_from_seq,
        window_to_seq=row.window_to_seq,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        report=content,
    )


__all__ = [
    "MAX_RAW_OUTPUT_CHARS",
    "REFEREEABLE_STATUSES",
    "CaptainError",
    "CaptainSchemaError",
    "get_latest_report",
    "get_report",
    "parse_report",
    "reset_throttle",
    "run_analysis",
    "run_analysis_failure",
    "throttle_remaining",
    "to_public",
    "trigger",
]
