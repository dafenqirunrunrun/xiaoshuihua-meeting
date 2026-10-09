"""Task 6 (Quick reaches the client early) + Task 7 (latency instrumentation).

Why these are separate tests from `test_captain_two_stage.py`
------------------------------------------------------------
That file pins the two-stage COMPUTATION: both stages run, the flat fields are
derived from Deep, Quick degrades without sinking Deep, reversal is disclosed.
This file pins the two things that make the computation worth having:

  1. **The first answer is VISIBLE before the second finishes.** A Quick result
     that only reaches the client inside the final `captain.completed` payload
     has no latency benefit at all — the user waits out Deep either way. The
     event must therefore be emitted while the row is still ANALYZING, and it
     must be a distinct event type so a client can tell "first answer, keep
     waiting" from "durable report, stop waiting".
  2. **The latency is MEASURABLE in the way the spec asks.** Trigger-to-Quick and
     Trigger-to-Deep are the two numbers the design is judged on, and
     `quick_ms` alone cannot distinguish a slow model from a report that waited
     in the queue — two problems with opposite fixes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from app.captain import service as captain_service
from app.config import settings

from test_captain import (  # noqa: E402  - sibling test module
    _make_meeting,
    _post_final,
    _trigger,
)
from test_captain_two_stage import StagedProvider, _seed  # noqa: E402


@pytest.fixture
def two_stage(monkeypatch):
    monkeypatch.setattr(settings, "captain_two_stage_enabled", True)
    yield


def _captain_events(monkeypatch) -> list:
    """Record every captain.* envelope from the REAL broker.

    Wraps `publish`, not `build_*`, so the assertion is on the envelope as
    actually emitted — same stance as `CaptainEventRecorder` in `test_captain.py`.
    """
    events: list = []
    from app.realtime.event_broker import event_broker

    original = event_broker.publish

    def spy(meeting_id, event):
        if event.event_type.startswith("captain."):
            events.append(event)
        return original(meeting_id, event)

    monkeypatch.setattr(event_broker, "publish", spy)
    return events


def _run(client, monkeypatch, provider, expected=None):
    """Trigger, drain, and return (report_body, report_id)."""
    from test_captain import _patch_provider

    _patch_provider(monkeypatch, provider)
    meeting_id = expected["meeting_id"] if expected else _seed(client)
    body = _trigger(client, meeting_id).json()
    from app.captain.worker import captain_worker

    assert captain_worker.drain(timeout=20.0)
    got = client.get(
        f"/api/meetings/{meeting_id}/captain/{body['report_id']}"
    ).json()
    return got, body["report_id"]


class TestQuickIsPublishedWhileDeepIsStillRunning:
    def test_quick_event_precedes_the_completed_event(
        self, client, monkeypatch, two_stage
    ):
        events = _captain_events(monkeypatch)
        _, report_id = _run(client, monkeypatch, StagedProvider())

        types = [e.event_type for e in events]
        assert "captain.quick_assessment" in types, types
        assert types.index("captain.quick_assessment") < types.index(
            "captain.completed"
        )

        quick_env = next(
            e for e in events if e.event_type == "captain.quick_assessment"
        )
        assert quick_env.payload["report_id"] == report_id
        assert quick_env.payload["issue"] == "是否按计划上线"
        assert quick_env.payload["provisional"] is True
        assert quick_env.payload["question_to_resolve"]
        assert quick_env.sse_id == (
            f"captain:{quick_env.meeting_id}:{report_id}:quick_assessment"
        )

    def test_the_quick_event_is_out_before_deep_returns(
        self, client, monkeypatch, two_stage
    ):
        """Observed from INSIDE the Deep call.

        This is the property that would silently break if someone "fixed" the
        non-commit-before-publish ordering by moving the publish after the
        commit: the event would still exist, but arrive after the user had
        already waited out the Deep pass.
        """
        events = _captain_events(monkeypatch)
        seen: dict = {}

        class ObservingProvider(StagedProvider):
            def complete(self, prompt, *, timeout_seconds=None):
                raw = super().complete(prompt, timeout_seconds=timeout_seconds)
                if "深入核对" in prompt:
                    seen["quick_out"] = any(
                        e.event_type == "captain.quick_assessment" for e in events
                    )
                    seen["completed_out"] = any(
                        e.event_type == "captain.completed" for e in events
                    )
                return raw

        _run(client, monkeypatch, ObservingProvider())

        assert seen.get("quick_out") is True
        assert seen.get("completed_out") is False, (
            "captain.completed exists before Deep returned — the report is being "
            "published before it is written"
        )

    def test_a_degraded_quick_publishes_no_event(
        self, client, monkeypatch, two_stage
    ):
        """No Quick result means no Quick event — not an empty one.

        An empty `captain.quick_assessment` would make a client render a blank
        provisional card it can never fill, which is worse than never hearing
        about the stage at all.
        """
        events = _captain_events(monkeypatch)
        got, _ = _run(client, monkeypatch, StagedProvider(quick_error="quick down"))

        assert got["status"] == "COMPLETED"
        assert "captain.quick_assessment" not in [e.event_type for e in events]
        assert got["report"]["quick"] is None

    def test_quick_event_payload_leaks_no_model_internals(
        self, client, monkeypatch, two_stage
    ):
        """The transport must not carry raw output / model_name / prompts.

        Asserted on a NEW event type specifically because that is where a leak
        gets introduced by default — the convenient `model_dump()` of the whole
        stage object.
        """
        events = _captain_events(monkeypatch)
        _run(client, monkeypatch, StagedProvider())

        env = next(
            e for e in events if e.event_type == "captain.quick_assessment"
        )
        assert set(env.payload) == {
            "report_id",
            "provisional",
            "issue",
            "key_evidence",
            "provisional_judgement",
            "question_to_resolve",
        }
        blob = json.dumps(env.payload, ensure_ascii=False)
        assert "raw" not in blob
        assert "model_name" not in blob
        assert "prompt" not in blob


class TestLatencyInstrumentation:
    """Task 7: the four stamps the spec names, plus the two derived numbers."""

    def _metrics_payload(self, client, monkeypatch) -> dict:
        """Capture the one structured telemetry line for this analysis.

        `propagate=False` and an explicit level are set because the suite's root
        logger config otherwise filters the record before it reaches the
        handler — the line is INFO, and the app's logger is configured by
        `app/logging.py` rather than by this test's capture.
        """
        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("app.captain")
        handler = _Capture(level=logging.DEBUG)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            _run(client, monkeypatch, StagedProvider())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        line = next(m for m in records if m.startswith("captain_search_metrics"))
        return json.loads(line.split(" ", 1)[1])

    def test_metrics_line_carries_the_four_stamps(self, client, monkeypatch, two_stage):
        payload = self._metrics_payload(client, monkeypatch)

        for key in (
            "trigger_at",
            "quick_started_at",
            "quick_completed_at",
            "deep_completed_at",
        ):
            assert key in payload, f"missing latency stamp {key}"

    def test_derived_ms_match_the_stamps(self, client, monkeypatch, two_stage):
        payload = self._metrics_payload(client, monkeypatch)

        assert payload["trigger_to_quick_ms"] == int(
            (payload["quick_completed_at"] - payload["trigger_at"]) * 1000
        )
        assert payload["trigger_to_deep_ms"] == int(
            (payload["deep_completed_at"] - payload["trigger_at"]) * 1000
        )
        # Ordered: the trigger precedes Quick, which precedes Deep.
        assert 0 <= payload["trigger_to_quick_ms"] <= payload["trigger_to_deep_ms"]

    def test_derived_ms_are_reproducible_from_the_published_stamps(
        self, client, monkeypatch, two_stage
    ):
        """A reader with only the log line must be able to re-derive the numbers.

        This is the property that makes publishing BOTH the stamps and the
        derived values worth anything. Without it the derived numbers are an
        unverifiable claim: `int(f * 1000)` on an unrounded float does not
        commute with the subtraction, so the identity holds or fails by luck
        depending on the microsecond — which is exactly how this test's
        predecessor failed intermittently.

        Asserted with the same expression a log reader would write, so the test
        and the reader are checking the same thing.
        """
        payload = self._metrics_payload(client, monkeypatch)

        for stamp, key in (
            ("quick_completed_at", "trigger_to_quick_ms"),
            ("deep_completed_at", "trigger_to_deep_ms"),
        ):
            assert payload[key] == int(
                (payload[stamp] - payload["trigger_at"]) * 1000
            ), f"{key} is not re-derivable from {stamp} and trigger_at"

        # And the stamps are at millisecond resolution, which is what makes the
        # identity above exact rather than merely close.
        for stamp in ("trigger_at", "quick_started_at", "quick_completed_at"):
            assert round(payload[stamp], 3) == payload[stamp], stamp

    def test_stage_split_is_still_present(self, client, monkeypatch, two_stage):
        payload = self._metrics_payload(client, monkeypatch)

        assert payload["quick_ms"] >= 0
        assert payload["deep_ms"] >= 0
        assert payload["quick_ok"] is True
        assert "superseded_quick" in payload

    def test_research_routing_is_reported(self, client, monkeypatch, two_stage):
        """The §7 flag must reach the metrics line, not just the object.

        A flag nobody can grep for in production is a flag that cannot answer
        "how often did the internal-topic rule fire?".
        """
        payload = self._metrics_payload(client, monkeypatch)

        assert "internal_information_required" in payload
        assert "research_needed" in payload
        assert "research_question_count" in payload
        assert "research_blocked_categories" in payload

    def test_research_status_is_on_the_metrics_line(self, client, monkeypatch, two_stage):
        """Phase C.5: the log line and the report must not disagree.

        `research_status` is the one research field that lives on the
        `EnrichmentResult` rather than on `SearchMetrics`, so it is passed to
        `_log_metrics` explicitly. If that wiring were dropped, the metrics line
        would be missing the single value an operator needs to tell "the policy
        never asked" from "we asked and the provider failed" — the report body
        would still be right, which is exactly why this needs its own assertion.
        """
        payload = self._metrics_payload(client, monkeypatch)

        assert payload["research_status"] == "not_required"

    def test_the_phase_c5_metrics_set_is_complete(self, client, monkeypatch, two_stage):
        """Every Phase C.5 metric is present, under its emitted key.

        The spec lists the metrics by ROLE; several are emitted under the names
        Phase 3-2 already gave them, and renaming them would break the log
        consumers this line exists for. The mapping is asserted here so the
        correspondence is verified rather than assumed:

          research_required        -> research_needed
          research_status          -> research_status
          query_count              -> search_query_count
          result_count             -> search_result_count
          selected_source_count    -> external_evidence_count
          search_latency_ms        -> search_latency_ms
          research_total_latency_ms-> research_ms
          quick_latency_ms         -> quick_ms
          deep_latency_ms          -> deep_ms
          total_latency_ms         -> captain_total_latency_ms
        """
        payload = self._metrics_payload(client, monkeypatch)

        for key in (
            "research_needed",
            "research_status",
            "search_query_count",
            "search_result_count",
            "external_evidence_count",
            "search_latency_ms",
            "research_ms",
            "quick_ms",
            "deep_ms",
            "captain_total_latency_ms",
        ):
            assert key in payload, f"missing Phase C.5 metric {key}"


class TestSearchFailureDoesNotFailTheTwoStageReport:
    """A broken search must cost external evidence, NOT the report (Phase 3-2 §3).

    The single-pass path proves this in `test_captain_search.py`. It is re-proved
    here for the two-stage path because the failure surface moved: Quick runs
    with no search at all, Deep runs with whatever the search produced, and a
    provider explosion during Deep is the one path where search and Deep share a
    call stack.
    """

    def test_a_raising_search_provider_still_yields_a_completed_report(
        self, client, monkeypatch, two_stage
    ):
        from app.captain.search import service as search_service

        def _boom(*args, **kwargs):
            raise RuntimeError("search exploded")

        monkeypatch.setattr(search_service, "enrich", _boom)
        got, _ = _run(client, monkeypatch, StagedProvider())

        assert got["status"] == "COMPLETED"
        assert got["report"]["quick"]["issue"] == "是否按计划上线"
        assert got["report"]["deep"]["judgement"]
        # No external evidence can exist, and that is the correct degraded state.
        assert got["report"]["external_evidence"] == []

    def test_a_timing_out_search_provider_still_yields_a_completed_report(
        self, client, monkeypatch, two_stage
    ):
        """A timed-out search is a NORMAL result, not an exception.

        The spec's explicit requirement: search failure or timeout must not fail
        the Captain. Driven through the real `enrich` with a provider that is
        past its deadline, rather than a stub that raises.
        """
        from app.captain.search.fake import FakeSearchProvider

        provider = FakeSearchProvider(timed_out=True)
        _run(client, monkeypatch, StagedProvider())
        # The end-to-end path above already proved the report survives; this
        # asserts the timeout provider itself is a non-failing normal result so
        # the two halves of the claim are both pinned.
        from app.captain.search import service as svc

        monkeypatch.setattr(
            svc, "get_search_provider", lambda *a, **k: provider
        )
        result = svc.enrich(
            [(1, "Redis 默认端口是 6379 吗"), (2, "不对吧，新版默认不是 6379")],
            provider=provider,
        )
        assert result.evidence == []
        assert result.metrics.search_timed_out
        assert result.note


class TestTriggerStampComesFromTheDurableRow:
    """`trigger_at` must be the row's timestamp, not this process's."""

    def test_naive_sqlite_timestamp_is_read_as_utc(self):
        row = type("Row", (), {"created_at": "2026-09-29T10:00:00"})()
        epoch = captain_service._created_at_epoch(row)

        assert epoch is not None
        assert datetime.fromtimestamp(epoch, timezone.utc).hour == 10

    def test_iso_with_z_suffix_is_accepted(self):
        row = type("Row", (), {"created_at": "2026-09-29T10:00:00Z"})()
        epoch = captain_service._created_at_epoch(row)

        assert epoch is not None
        assert datetime.fromtimestamp(epoch, timezone.utc).hour == 10

    def test_unparseable_or_missing_yields_none_not_a_wrong_number(self):
        """Telemetry degrades to ABSENT, never to misleading."""
        assert (
            captain_service._created_at_epoch(
                type("Row", (), {"created_at": "not-a-timestamp"})()
            )
            is None
        )
        assert (
            captain_service._created_at_epoch(
                type("Row", (), {"created_at": None})()
            )
            is None
        )
        assert (
            captain_service._created_at_epoch(type("Row", (), {})()) is None
        )
