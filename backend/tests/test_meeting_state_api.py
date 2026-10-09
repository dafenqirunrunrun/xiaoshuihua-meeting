"""Step UI-INTEL-1 regression suite: public Meeting State API + state SSE event.

The step adds two things the frontend depends on:

  1. a READ-ONLY PRODUCT projection, `GET /api/meetings/{id}/state`, which is the
     source of truth the Intelligence Panel renders; and
  2. `meeting.state.updated` over the EXISTING `/events` SSE — a pure
     invalidation notice meaning "re-read (1)".

What this suite has to prove, beyond the happy path:

  * the public projection never leaks engineering-only fields (`model_name`);
  * every status a panel must render is reachable and correctly shaped
    (no-state / EMPTY / ACTIVE / FAILED / FROZEN);
  * a CORRUPT `state_json` degrades to FAILED instead of a product 500, keeps the
    readable scalars, and — importantly — is NOT laundered back into the DB;
  * the event fires on every PRODUCT-VISIBLE durable change, which is explicitly
    NOT the same as "version incremented": a failure changes the state while
    bumping no version, so a version-gated publish would silently hide it;
  * no event is emitted when nothing changed (NO_WORK / SUPERSEDED / a repeated
    failure) — otherwise the panel would re-fetch forever for no reason;
  * a publish failure can never damage the durable state nor the transcript.

Everything runs in-process on a fresh SQLite schema with a scripted provider.
The SSE assertions read the EventBroker directly rather than opening a real
stream: the broker is exactly the boundary where a publish either happened or
did not, and reading it keeps these tests deterministic.
"""

from __future__ import annotations

import json

import pytest

from app.meeting_state import service as state_service

PAYLOAD_FIELDS = ("topics", "decisions", "action_items", "open_questions", "risks")
PUBLIC_FIELDS = ("meeting_id", "version", "last_processed_segment_seq", "status",
                 "updated_at", "live_summary", "current_topic", *PAYLOAD_FIELDS)

PUBLIC_URL = "/api/meetings/{}/state"
INTERNAL_URL = "/api/internal/meetings/{}/state"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class ScriptedProvider:
    """Deterministic state-update provider (same shape as the AGENT-1 suite)."""

    name = "scripted-provider"

    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        from app.agent.llm.base import SummaryGenerationError

        self.calls.append(prompt)
        if self.error:
            raise SummaryGenerationError(self.error)
        seqs = _seqs_in_batch(prompt)
        return json.dumps(
            {
                "live_summary": "当前正在讨论上线安排，并逐步确认时间与后续事项。" if seqs else None,
                "current_topic": "上线安排" if seqs else None,
                "topics": [{"title": "上线安排", "summary": "s",
                            "source_segment_seqs": list(seqs)}] if seqs else [],
                "decisions": [{"text": "下周三上线", "source_segment_seqs": [seqs[-1]]}] if seqs else [],
                "action_items": [],
                "open_questions": [],
                "risks": [],
            },
            ensure_ascii=False,
        )


def _seqs_in_batch(prompt: str) -> list[int]:
    import re

    body = prompt.split("<new_transcript>")[1].split("</new_transcript>")[0]
    return [int(m) for m in re.findall(r"^\[(\d+)\]", body, re.MULTILINE)]


def _start_meeting(client, title: str = "上线安排") -> str:
    mid = client.post("/api/meetings", json={"title": title}).json()["id"]
    assert client.post(f"/api/meetings/{mid}/start").status_code == 200
    return mid


def _final(client, meeting_id: str, seq: int, text: str, speaker: str = "speaker_0"):
    response = client.post(
        f"/api/internal/meetings/{meeting_id}/transcript-events",
        json={
            "event_id": f"evt_{meeting_id}_{seq}",
            "meeting_id": meeting_id,
            "utterance_id": f"utt_{seq}",
            "segment_seq": seq,
            "speaker_id": speaker,
            "text": text,
            "status": "FINAL",
            "start_ms": seq * 1000,
            "end_ms": seq * 1000 + 900,
            "emitted_at": 1_789_000_000_000 + seq,
        },
    )
    from app.meeting_state.scheduler import meeting_state_scheduler

    assert meeting_state_scheduler.wait_for_idle(meeting_id, timeout_seconds=2.0)
    return response


def _stream_end(client, meeting_id: str, **over):
    """Close the transcript stream (the durable freeze boundary)."""
    body = {
        "event_id": f"end_{meeting_id}",
        "meeting_id": meeting_id,
        "event_type": "TRANSCRIPT_STREAM_END",
    }
    body.update(over)
    return client.post(
        f"/api/internal/meetings/{meeting_id}/stream-end", json=body
    )


def _public(client, meeting_id: str) -> dict:
    resp = client.get(PUBLIC_URL.format(meeting_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _db_row(meeting_id: str):
    from app.db import SessionLocal
    from app.meeting_state import repository as repo

    db = SessionLocal()
    try:
        return repo.get(db, meeting_id)
    finally:
        db.close()


def _set_state_json(meeting_id: str, raw: str) -> None:
    """Corrupt / rewrite the persisted payload directly (data-integrity fixture)."""
    from app.db import SessionLocal
    from app.meeting_state import repository as repo

    db = SessionLocal()
    try:
        row = repo.get(db, meeting_id)
        row.state_json = raw
        db.commit()
    finally:
        db.close()


class StateEventRecorder:
    """Capture `meeting.state.updated` publishes from the real EventBroker.

    Wrapping `publish` (not `build_*`) keeps the assertions on the same path
    production uses, and records the envelope so the payload/id contract is
    checked as actually emitted rather than as reconstructed by the test.
    """

    def __init__(self) -> None:
        self.events: list = []

    def install(self, monkeypatch) -> None:
        from app.realtime.event_broker import event_broker

        original = event_broker.publish

        def spy(meeting_id, event):
            if event.event_type == "meeting.state.updated":
                self.events.append(event)
            return original(meeting_id, event)

        monkeypatch.setattr(event_broker, "publish", spy)

    @property
    def versions(self) -> list[int]:
        return [e.payload["version"] for e in self.events]

    @property
    def statuses(self) -> list[str]:
        return [e.payload["status"] for e in self.events]


@pytest.fixture
def agent(monkeypatch) -> ScriptedProvider:
    provider = ScriptedProvider()
    monkeypatch.setattr("app.agent.llm.factory.get_provider", lambda **_policy: provider)
    return provider


@pytest.fixture
def state_events(monkeypatch) -> StateEventRecorder:
    rec = StateEventRecorder()
    rec.install(monkeypatch)
    return rec


def _drive_to_active(client, monkeypatch, agent, *, n: int = 3) -> str:
    """Get one meeting to a persisted ACTIVE state at the given batch size."""
    monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", n)
    mid = _start_meeting(client)
    for seq in range(1, n + 1):
        assert _final(client, mid, seq, f"line {seq}").status_code == 200
    assert agent.calls, "threshold must have triggered a state update"
    return mid


# ==========================================================================
# 1. public API — shape and leak-prevention
# ==========================================================================

class TestPublicApiShape:
    def test_unknown_meeting_is_404(self, client):
        assert client.get(PUBLIC_URL.format("nope")).status_code == 404

    def test_public_response_has_exactly_the_contract_fields(self, client):
        mid = _start_meeting(client)
        assert set(_public(client, mid)) == set(PUBLIC_FIELDS)

    def test_public_api_never_exposes_model_name(self, client, agent, monkeypatch):
        mid = _drive_to_active(client, monkeypatch, agent)
        body = _public(client, mid)
        assert "model_name" not in body
        # ...while the internal debug surface still reports it (it is not removed).
        internal = client.get(INTERNAL_URL.format(mid)).json()
        assert internal["model_name"] == "scripted-provider"

    def test_public_api_never_exposes_last_error(self, client, agent, monkeypatch):
        """A FAILED state records a reason internally; the product view omits it."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "provider blew up: secret-ish internal detail"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        body = _public(client, mid)
        assert body["status"] == "FAILED"
        assert "last_error" not in body
        serialized = json.dumps(body, ensure_ascii=False)
        assert "secret-ish internal detail" not in serialized
        # The internal surface keeps the diagnostic detail.
        internal = client.get(INTERNAL_URL.format(mid)).json()
        assert internal["status"] == "FAILED"
        assert _db_row(mid).last_error  # durable, for operators

    def test_no_state_written_by_a_read(self, client):
        """GET must stay read-only: no row is materialised by reading."""
        mid = _start_meeting(client)
        _public(client, mid)
        assert _db_row(mid) is None


# ==========================================================================
# 2. status matrix — every status a panel must render
# ==========================================================================

class TestStatusMatrix:
    def test_no_state_yet_is_empty_not_an_error(self, client):
        """Early in a meeting (below batch thresholds) there is simply nothing yet.

        This is the most common state a user sees, so it MUST be a 200 with an
        explicit EMPTY, never a 404 or an error.
        """
        mid = _start_meeting(client)
        body = _public(client, mid)
        assert body["status"] == "EMPTY"
        assert body["version"] == 0
        assert body["last_processed_segment_seq"] == 0
        assert body["live_summary"] is None
        assert body["current_topic"] is None
        for field in PAYLOAD_FIELDS:
            assert body[field] == []

    def test_active_state_exposes_structured_content(self, client, agent, monkeypatch):
        mid = _drive_to_active(client, monkeypatch, agent)
        body = _public(client, mid)
        assert body["status"] == "ACTIVE"
        assert body["version"] >= 1
        assert body["last_processed_segment_seq"] == 3
        assert body["live_summary"] == "当前正在讨论上线安排，并逐步确认时间与后续事项。"
        assert body["current_topic"] == "上线安排"
        assert body["decisions"][0]["text"] == "下周三上线"
        assert body["decisions"][0]["source_segment_seqs"] == [3]
        assert body["topics"][0]["title"] == "上线安排"
        assert body["updated_at"]

    def test_failed_state_keeps_the_previous_content(self, client, agent, monkeypatch):
        """The non-destructive failure rule, observed through the product API.

        A failed update moves only status/last_error, so the panel keeps showing
        the last good content alongside a degraded notice — it must NOT blank out.
        """
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200
        assert _public(client, mid)["status"] == "ACTIVE"
        version_before = _public(client, mid)["version"]

        agent.error = "boom"
        for seq in (3, 4):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        body = _public(client, mid)
        assert body["status"] == "STALE"
        assert body["version"] == version_before, "a failure must not bump the version"
        assert body["decisions"], "last good content must be preserved"
        assert body["live_summary"] == "当前正在讨论上线安排，并逐步确认时间与后续事项。"
        assert body["current_topic"] == "上线安排"

    def test_failed_then_recovers_to_active(self, client, agent, monkeypatch):
        """FAILED -> ACTIVE must be reachable, and the version resumes climbing."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "boom"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200
        assert _public(client, mid)["status"] == "FAILED"

        agent.error = None  # provider healthy again; the same segments are replayed
        for seq in (3, 4):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        body = _public(client, mid)
        assert body["status"] == "ACTIVE"
        assert body["version"] >= 1

    def test_frozen_after_the_transcript_freezes(self, client, agent, monkeypatch):
        """FROZEN is the archived working memory, and its content is preserved.

        Canonical order: the meeting finishes (FINALIZING) and only then does the
        stream-end arrive — stream-end is gated on FINALIZING, and FROZEN is
        asserted by the flush that runs once nothing is left to absorb.
        """
        mid = _drive_to_active(client, monkeypatch, agent)
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200
        assert _stream_end(client, mid).status_code == 200

        body = _public(client, mid)
        assert body["status"] == "FROZEN"
        assert body["decisions"], "freezing must not clear the archived content"
        assert body["current_topic"] == "上线安排"

    def test_meeting_completed_with_failed_state_is_a_legal_combination(
        self, client, agent, monkeypatch
    ):
        """COMPLETED meeting + FAILED state must render, not read as a conflict.

        The meeting lifecycle and the agent's working status are independent
        dimensions: the summary can fail too, and the meeting still completes.
        """
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "boom"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        # Freeze the stream so the finalizer can run to completion.
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200
        assert _stream_end(client, mid).status_code == 200

        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "COMPLETED"
        body = _public(client, mid)
        assert body["status"] == "FAILED"
        assert body["version"] == 0


# ==========================================================================
# 3. corrupt state — degrade, never 500, never write back
# ==========================================================================

class TestCorruptState:
    def test_corrupt_json_degrades_to_failed_not_500(self, client, agent, monkeypatch):
        mid = _drive_to_active(client, monkeypatch, agent)
        _set_state_json(mid, "{not valid json")

        resp = client.get(PUBLIC_URL.format(mid))
        assert resp.status_code == 200, "a corrupt row must not become a product 500"
        body = resp.json()
        assert body["status"] == "FAILED"
        # Scalars we can still read are preserved (the panel shows version/updated_at).
        assert body["version"] >= 1
        assert body["last_processed_segment_seq"] == 3
        assert body["updated_at"]
        # Content is empty rather than stale/garbage, and never ACTIVE (which
        # would falsely tell the user the AI is working normally).
        for field in PAYLOAD_FIELDS:
            assert body[field] == []
        assert body["live_summary"] is None
        assert body["current_topic"] is None

    def test_corrupt_json_is_not_laundered_into_the_database(self, client, agent, monkeypatch):
        """The read path must not 'fix' the row — that would erase evidence."""
        mid = _drive_to_active(client, monkeypatch, agent)
        _set_state_json(mid, "{not valid json")
        before = _db_row(mid).state_json

        client.get(PUBLIC_URL.format(mid))
        client.get(PUBLIC_URL.format(mid))

        assert _db_row(mid).state_json == before

    def test_corrupt_json_does_not_leak_the_parser_error(self, client, agent, monkeypatch):
        mid = _drive_to_active(client, monkeypatch, agent)
        _set_state_json(mid, "{not valid json")
        serialized = json.dumps(_public(client, mid), ensure_ascii=False)
        assert "invalid JSON" not in serialized
        assert "Traceback" not in serialized

    def test_schema_mismatch_also_degrades(self, client, agent, monkeypatch):
        """Readable JSON of the wrong shape is corruption too."""
        mid = _drive_to_active(client, monkeypatch, agent)
        _set_state_json(mid, json.dumps({"decisions": "not-a-list"}))
        assert _public(client, mid)["status"] == "FAILED"


# ==========================================================================
# 4. the event itself — contract
# ==========================================================================

class TestStateEventContract:
    def test_event_is_published_on_a_successful_update(self, client, agent, monkeypatch, state_events):
        mid = _drive_to_active(client, monkeypatch, agent)
        assert state_events.events, "a durable content change must be announced"
        event = state_events.events[-1]
        assert event.event_type == "meeting.state.updated"
        assert event.meeting_id == mid
        assert event.payload == {"version": 1, "status": "ACTIVE"}

    def test_payload_carries_only_version_and_status(self, client, agent, monkeypatch, state_events):
        """The event is an invalidation signal, NOT a state carrier."""
        mid = _drive_to_active(client, monkeypatch, agent)
        payload = state_events.events[-1].payload
        assert set(payload) == {"version", "status"}
        for field in PAYLOAD_FIELDS:
            assert field not in payload
        assert "live_summary" not in payload
        assert "model_name" not in payload

    def test_payload_never_leaks_internal_detail(self, client, agent, monkeypatch, state_events):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "provider blew up: secret-ish internal detail"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        serialized = json.dumps([e.to_sse_data() for e in state_events.events], ensure_ascii=False)
        assert "secret-ish internal detail" not in serialized
        assert "Traceback" not in serialized

    def test_sse_id_is_stable_and_distinguishes_status_at_equal_version(
        self, client, agent, monkeypatch, state_events
    ):
        """The id must not collide for two different states at the same version.

        ACTIVE@1 and STALE@1 are genuinely different durable states (a failure
        does not bump the version), so a version-only id would make them
        indistinguishable to a dedup-ing client.
        """
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200
        agent.error = "boom"
        for seq in (3, 4):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        statuses = state_events.statuses
        assert statuses == ["ACTIVE", "STALE"]
        ids = [e.sse_id for e in state_events.events]
        assert ids[0] == f"state:{mid}:1:active"
        assert ids[1] == f"state:{mid}:1:stale"
        assert ids[0] != ids[1], "equal version must not mean equal id"

    def test_event_envelope_renders_as_a_named_sse_frame(self, client, agent, monkeypatch, state_events):
        """Regression guard for the named-frame trap (8C-3): if this stops being
        a named frame, every frontend subscription silently stops working."""
        from app.realtime.sse_service import _render_frame

        _drive_to_active(client, monkeypatch, agent)
        frame = _render_frame(state_events.events[-1])
        assert "event: meeting.state.updated" in frame
        assert f"id: state:" in frame
        assert frame.endswith("\n\n"), "frames must be blank-line terminated"


# ==========================================================================
# 5. publish matrix — WHEN it fires (and when it must not)
# ==========================================================================

class TestPublishMatrix:
    def test_failure_is_announced_even_though_the_version_does_not_move(
        self, client, agent, monkeypatch, state_events
    ):
        """The core correction to a naive 'version changed' trigger.

        A failed update changes what the product must show (AI unavailable) while
        leaving `version` untouched, so gating the publish on a version bump would
        hide exactly the transition the panel needs to display.
        """
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "boom"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        assert state_events.statuses == ["FAILED"]
        assert state_events.versions == [0], "no version bump, yet an event must fire"

    def test_recovery_is_announced(self, client, agent, monkeypatch, state_events):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "boom"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        # Once the provider recovers, the SAME segments (still pending, because a
        # failure never advances the cursor) are replayed and now succeed — and
        # the coalesced recovery success is announced once.
        agent.error = None
        for seq in (3, 4):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        assert state_events.statuses == ["FAILED", "ACTIVE"]
        assert state_events.versions == [0, 1]
        assert state_events.events[0].sse_id == f"state:{mid}:0:failed"
        assert state_events.events[-1].sse_id == f"state:{mid}:1:active"

    def test_freeze_is_announced(self, client, agent, monkeypatch, state_events):
        mid = _drive_to_active(client, monkeypatch, agent)
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200
        state_events.events.clear()
        assert _stream_end(client, mid).status_code == 200

        assert state_events.statuses == ["FROZEN"]

    def test_no_event_when_nothing_changed(self, client, agent, monkeypatch, state_events):
        """NO_WORK must stay silent: the panel would otherwise re-fetch on every
        FINAL that does not yet reach a batch threshold."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 5)
        mid = _start_meeting(client)
        _final(client, mid, 1, "line 1")

        state_events.events.clear()
        # A duplicate delivery of an already-persisted FINAL: nothing new to fold in.
        assert _final(client, mid, 1, "line 1").status_code == 200
        assert state_events.events == []

    def test_repeated_failure_does_not_spam(self, client, agent, monkeypatch, state_events):
        """Each failed run is a real attempt, but the DURABLE state does not change
        from FAILED to FAILED — so the panel must not be woken again."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "boom"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200
        assert state_events.statuses == ["FAILED"]

        state_events.events.clear()
        # Same segments still pending -> another failed attempt.
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            state_service.process_pending(db, mid)
        finally:
            db.close()
        assert state_events.events == [], "FAILED -> FAILED is not a product-visible change"

    def test_superseded_run_publishes_nothing(self, client, agent, monkeypatch, state_events):
        """A run whose result is discarded must not announce a version it never wrote."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        # Drive a SUPERSEDED outcome by advancing the persisted cursor underneath
        # a run that already computed its update: emulate by calling process_pending
        # with a stale cursor view (Phase C staleness check).
        from app.db import SessionLocal
        from app.meeting_state import repository as repo

        db = SessionLocal()
        try:
            row = repo.get(db, mid)
            row.last_processed_segment_seq = 999  # what a concurrent run would have written
            db.commit()
        finally:
            db.close()

        state_events.events.clear()
        db = SessionLocal()
        try:
            report = state_service.process_pending(db, mid)
        finally:
            db.close()
        assert report["outcome"] in (state_service.OUTCOME_NO_WORK,
                                     state_service.OUTCOME_SUPERSEDED)
        assert state_events.events == []

    def test_publish_failure_does_not_damage_the_state_or_the_transcript(
        self, client, agent, monkeypatch
    ):
        """SSE is not a fact source: a broken broker must not corrupt anything."""
        from app.realtime.event_broker import event_broker

        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)

        original = event_broker.publish

        def explode_on_state_event(meeting_id, event):
            # Only the NEW event type is sabotaged: the transcript / meeting.status
            # paths must keep working, because the claim under test is precisely
            # that the state notification is isolated from them.
            if event.event_type == "meeting.state.updated":
                raise RuntimeError("broker is down")
            return original(meeting_id, event)

        monkeypatch.setattr(event_broker, "publish", explode_on_state_event)

        mid = _start_meeting(client)
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        # The state update still landed durably...
        body = _public(client, mid)
        assert body["status"] == "ACTIVE"
        assert body["version"] == 1
        # ...and the transcript path is entirely unaffected.
        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["segment_seq"] for s in segments] == [1, 2]
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "IN_PROGRESS"

    def test_agent_failure_does_not_fail_the_transcript(
        self, client, agent, monkeypatch
    ):
        """The headline independence claim: Agent FAILED != Transcript FAILED."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        agent.error = "boom"
        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["segment_seq"] for s in segments] == [1, 2]
        assert client.get(f"/api/meetings/{mid}").json()["status"] == "IN_PROGRESS"
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200


# ==========================================================================
# 6. legacy SSE surface is untouched
# ==========================================================================

class TestLegacySseUnaffected:
    def test_existing_event_types_are_still_published(self, client, monkeypatch):
        """Widening the Literal must not disturb the existing events."""
        from app.realtime.event_broker import event_broker

        seen: list[str] = []
        original = event_broker.publish
        monkeypatch.setattr(
            event_broker, "publish",
            lambda mid, ev: (seen.append(ev.event_type), original(mid, ev))[1],
        )

        mid = _start_meeting(client)
        _final(client, mid, 1, "line 1")
        client.post(f"/api/meetings/{mid}/finish")

        assert "meeting.status" in seen
        assert "transcript.final" in seen

    def test_transcript_final_catchup_shape_is_unchanged(self, client):
        """The recovery cursor contract must survive this step untouched."""
        mid = _start_meeting(client)
        _final(client, mid, 1, "line 1")
        snapshot = client.get(f"/api/meetings/{mid}/transcript").json()
        assert snapshot["segments"][0]["segment_seq"] == 1

    def test_event_type_literal_contains_every_published_type(self):
        """Every event type we publish must be declared in the Literal.

        `SSEEventEnvelope.event_type` is a closed Literal, so this is also a
        typing-level guard: an undeclared publish is a construction error rather
        than a silent runtime surprise.

        This set is the BACKEND leg of the CLAUDE.md three-place sync rule. When
        a new type is added here, the frontend whitelist
        (`utils/sse.ts::SSE_EVENT_TYPES`) and type (`types/sse.ts::SseEventType`)
        must be updated in the same change, or the frontend silently drops the
        event and `npm run typecheck` fails on the exhaustiveness guard.
        """
        from typing import get_args

        from app.realtime.schemas import SSE_EVENT_TYPES

        declared = set(get_args(SSE_EVENT_TYPES))
        assert declared == {
            "transcript.partial",
            "transcript.final",
            "meeting.status",
            "summary.started",
            "summary.completed",
            "summary.failed",
            "meeting.state.updated",
            # ADR-002 Phase 2-A (水华队长)
            "captain.completed",
            "captain.failed",
            # Task 6: the fast first pass of the two-stage referee. Declared as
            # its own event rather than a sub-field of captain.completed so that
            # "the report is durable" and "a first answer exists" stay
            # distinguishable at the transport layer.
            "captain.quick_assessment",
            "heartbeat",
        }
