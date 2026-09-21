"""Step AGENT-1 regression suite: Meeting State Agent.

Covers the unit + integration matrix from the step spec:

  unit          empty state / first batch / second incremental batch / duplicate
                segment / old-segment replay / forced flush / JSON validation
                failure / persistence restore / state version increment
  integration   FINAL x N -> batches -> state -> more FINAL -> incremental ->
                finish -> forced flush -> final state
  policy        agent is idempotent, non-blocking, and failure never pollutes
                the persisted state nor blocks the transcript

Everything runs in-process (TestClient) on a fresh SQLite schema, with a
scripted FakeProvider. No network, no real ASR, no real LLM.

Note on the FakeProvider: TestClient runs BackgroundTasks synchronously before
the request returns, so a FINAL POST -> agent run -> state update is observable
immediately after the call. That is what lets these tests be deterministic
without polling.
"""

from __future__ import annotations

import json

import pytest

from app.meeting_state import service as state_service
from app.meeting_state.schemas import MeetingStateContent, MeetingStateUpdate

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

PAYLOAD_FIELDS = (
    "topics",
    "decisions",
    "action_items",
    "open_questions",
    "risks",
)


class ScriptedProvider:
    """Deterministic provider: builds a state update from the batch it sees.

    This is what makes the replay assertions meaningful without pinning model
    wording: the provider reads the `[seq] <speaker> text` lines out of the
    prompt it was handed and emits an update citing exactly those seqs. So the
    tests assert pipeline behaviour (batching, cursor, merge, evidence trace),
    not prose.

    `raw` overrides the emitted text entirely — used for the validation-failure
    cases. `error` makes every call raise a provider-level failure.
    """

    name = "scripted-provider"

    def __init__(self, *, raw: str | None = None, error: str | None = None) -> None:
        self.raw = raw
        self.error = error
        self.calls: list[str] = []
        self.seqs_seen: list[list[int]] = []

    def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        from app.agent.llm.base import SummaryGenerationError

        self.calls.append(prompt)
        if self.error:
            raise SummaryGenerationError(self.error)

        seqs = _seqs_in_batch(prompt)
        self.seqs_seen.append(seqs)
        if self.raw is not None:
            return self.raw

        return json.dumps(
            {
                "live_summary": f"已处理到片段 {seqs[-1]}，会议讨论正在持续推进。" if seqs else None,
                "current_topic": f"topic-{seqs[-1]}" if seqs else None,
                "topics": [{"title": f"议题{seqs[0]}", "summary": "s",
                            "source_segment_seqs": list(seqs)}] if seqs else [],
                "decisions": [{"text": f"决策{seqs[-1]}",
                               "source_segment_seqs": [seqs[-1]]}] if seqs else [],
                "action_items": [],
                "open_questions": [],
                "risks": [],
            },
            ensure_ascii=False,
        )


def _seqs_in_batch(prompt: str) -> list[int]:
    """Extract the segment_seq numbers from the `<new_transcript>` block."""
    import re

    body = prompt.split("<new_transcript>")[1].split("</new_transcript>")[0]
    return [int(m) for m in re.findall(r"^\[(\d+)\]", body, re.MULTILINE)]


def _start_meeting(client, title: str = "上线安排") -> str:
    mid = client.post("/api/meetings", json={"title": title}).json()["id"]
    assert client.post(f"/api/meetings/{mid}/start").status_code == 200
    return mid


def _final(client, meeting_id: str, seq: int, text: str, speaker: str = "speaker_0"):
    """Ingest one FINAL through the INTERNAL ingress (seq is producer-owned here)."""
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


def _state(client, meeting_id: str) -> dict:
    resp = client.get(f"/api/internal/meetings/{meeting_id}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _db_state(meeting_id: str):
    """Read the raw persisted row (bypassing the API) for durability assertions."""
    from app.db import SessionLocal
    from app.meeting_state import repository as repo

    db = SessionLocal()
    try:
        return repo.get(db, meeting_id)
    finally:
        db.close()


@pytest.fixture
def agent(monkeypatch) -> ScriptedProvider:
    """Install a scripted provider on the SAME resolution path production uses.

    Patches the factory the service calls when no provider is injected, so the
    tests exercise the real code path rather than a test-only hook. Tests mutate
    `agent.raw` / `agent.error` mid-test to script a failure on the next call.
    """
    provider = ScriptedProvider()
    monkeypatch.setattr("app.agent.llm.factory.get_provider", lambda **_policy: provider)
    return provider


# ==========================================================================
# unit
# ==========================================================================

class TestEmptyState:
    def test_state_is_empty_before_anything_is_processed(self, client):
        mid = _start_meeting(client)
        body = _state(client, mid)
        assert body["version"] == 0
        assert body["last_processed_segment_seq"] == 0
        assert body["status"] == "EMPTY"
        assert body["current_topic"] is None
        for field in PAYLOAD_FIELDS:
            assert body[field] == []

    def test_state_for_unknown_meeting_is_404(self, client):
        assert client.get("/api/internal/meetings/nope/state").status_code == 404

    def test_no_row_is_created_for_an_empty_meeting(self, client):
        """Reading an empty state must not write a row (read stays read-only)."""
        mid = _start_meeting(client)
        _state(client, mid)
        assert _db_state(mid) is None


class TestBatching:
    def test_first_batch_flushes_at_the_segment_threshold(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 3)
        provider = agent
        mid = _start_meeting(client)

        for seq in (1, 2):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200
        assert provider.calls == [], "must not call the LLM below the threshold"

        _final(client, mid, 3, "line 3")
        assert len(provider.calls) == 1, "third segment reaches the threshold"
        assert provider.seqs_seen[0] == [1, 2, 3]

        body = _state(client, mid)
        assert body["last_processed_segment_seq"] == 3
        assert body["version"] == 1

    def test_batch_flushes_at_the_char_threshold(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 99)
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_chars", 10)
        provider = agent
        mid = _start_meeting(client)

        _final(client, mid, 1, "x" * 10)
        assert len(provider.calls) == 1
        assert provider.seqs_seen[0] == [1]

    def test_batch_never_splits_a_segment_and_keeps_order(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        provider = agent
        mid = _start_meeting(client)

        for seq in range(1, 6):
            _final(client, mid, seq, f"line {seq}")
        assert provider.seqs_seen == [[1, 2], [3, 4]], "batches in seq order, capped at 2"

        body = _state(client, mid)
        assert body["last_processed_segment_seq"] == 4
        assert body["version"] == 2

    def test_select_batches_separates_ready_from_remainder(self):
        class S:
            def __init__(self, seq, text):
                self.segment_seq = seq
                self.text = text

        segs = [S(i, "a" * 4) for i in range(1, 6)]

        ready, remainder = state_service.select_batches(segs, max_segments=2, max_chars=1000)
        assert [[s.segment_seq for s in b] for b in ready] == [[1, 2], [3, 4]]
        assert [s.segment_seq for s in remainder] == [5], "trailing tail stays buffered"

        ready, remainder = state_service.select_batches(segs, max_segments=99, max_chars=8)
        assert [[s.segment_seq for s in b] for b in ready] == [[1, 2], [3, 4]]
        assert [s.segment_seq for s in remainder] == [5]


class TestIncremental:
    def test_second_batch_updates_incrementally(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        provider = agent
        mid = _start_meeting(client)

        for seq in (1, 2):
            _final(client, mid, seq, f"line {seq}")
        assert _state(client, mid)["version"] == 1

        for seq in (3, 4):
            _final(client, mid, seq, f"line {seq}")
        body = _state(client, mid)
        assert body["version"] == 2, "state version increments per successful update"
        assert body["last_processed_segment_seq"] == 4
        assert provider.seqs_seen == [[1, 2], [3, 4]]

    def test_prompt_carries_state_and_only_the_new_batch(self, client, agent, monkeypatch):
        """The cost model of this step: previous state + NEW batch, never the
        whole transcript again."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        provider = agent
        mid = _start_meeting(client)

        for seq in (1, 2):
            _final(client, mid, seq, "FIRST_BATCH_SENTINEL")
        for seq in (3, 4):
            _final(client, mid, seq, "SECOND_BATCH_SENTINEL")

        second_prompt = provider.calls[1]
        assert "SECOND_BATCH_SENTINEL" in second_prompt
        assert "FIRST_BATCH_SENTINEL" not in second_prompt
        # ...but what we already derived from batch 1 IS re-sent, as state.
        assert "<current_state>" in second_prompt
        assert "决策2" in second_prompt

    def test_merge_distinguishes_omitted_fields_from_explicit_empty_lists(
        self, client, monkeypatch
    ):
        """Omitted preserves; explicit [] clears resolved/retracted content."""
        mid = _start_meeting(client)
        from app.meeting_state import service as svc

        previous = MeetingStateContent(
            live_summary="old live summary",
            current_topic="old topic",
            decisions=[{"text": "old", "source_segment_seqs": [1]}],
            open_questions=[{"text": "resolved", "source_segment_seqs": [1]}],
        )
        omitted = MeetingStateUpdate()
        preserved = svc.merge_content(previous, omitted)
        assert [d.text for d in preserved.decisions] == ["old"]
        assert [q.text for q in preserved.open_questions] == ["resolved"]
        assert preserved.live_summary == "old live summary"
        assert preserved.current_topic == "old topic"

        explicit_empty = MeetingStateUpdate(
            live_summary=None,
            current_topic=None,
            decisions=[],
            open_questions=[],
        )
        cleared = svc.merge_content(previous, explicit_empty)
        assert cleared.decisions == []
        assert cleared.open_questions == []
        assert cleared.live_summary is None
        assert cleared.current_topic is None

        replacing = MeetingStateContent(
            decisions=[{"text": "new", "source_segment_seqs": [2]}]
        )
        merged2 = svc.merge_content(previous, replacing)
        assert [d.text for d in merged2.decisions] == ["new"]


class TestIdempotency:
    def test_duplicate_final_is_not_processed_twice(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)

        _final(client, mid, 1, "hello")
        assert len(provider.calls) == 1
        version_after_first = _state(client, mid)["version"]

        # Exact retry: same event_id, same seq, same content -> 200 idempotent.
        assert _final(client, mid, 1, "hello").status_code == 200
        assert len(provider.calls) == 1, "a duplicate FINAL must not trigger the agent"
        assert _state(client, mid)["version"] == version_after_first

    def test_replay_of_an_old_segment_does_not_advance_state(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)

        for seq in (1, 2, 3):
            _final(client, mid, seq, f"line {seq}")
        assert _state(client, mid)["last_processed_segment_seq"] == 3
        calls_before = len(provider.calls)

        # A late duplicate of an already-processed segment (Case C path: same
        # seq, different event_id, identical content).
        resp = client.post(
            f"/api/internal/meetings/{mid}/transcript-events",
            json={
                "event_id": "evt_late_replay",
                "meeting_id": mid,
                "utterance_id": "utt_2",
                "segment_seq": 2,
                "speaker_id": "speaker_0",
                "text": "line 2",
                "status": "FINAL",
                "start_ms": 2000,
                "end_ms": 2900,
            },
        )
        assert resp.status_code == 200
        assert len(provider.calls) == calls_before
        assert _state(client, mid)["last_processed_segment_seq"] == 3

    def test_cursor_blocks_a_stale_run_even_if_called_directly(self, client, agent, monkeypatch):
        """The cursor is enforced in the service, not only at the HTTP layer."""
        from app.db import SessionLocal

        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)
        _final(client, mid, 1, "line 1")

        db = SessionLocal()
        try:
            report = state_service.process_pending(db, mid, provider=provider)
        finally:
            db.close()
        assert report["outcome"] == state_service.OUTCOME_NO_WORK
        assert len(provider.calls) == 1


class TestForcedFlush:
    def test_finish_does_not_flush_a_sub_threshold_buffer(self, client, agent, monkeypatch):
        """Finalization must not run or wait for realtime understanding."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 50)
        provider = agent
        mid = _start_meeting(client)

        for seq in (1, 2):
            _final(client, mid, seq, f"line {seq}")
        assert provider.calls == [], "two segments are below the batch threshold"

        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200

        assert provider.calls == []
        body = _state(client, mid)
        assert body["last_processed_segment_seq"] == 0
        assert body["version"] == 0

    def test_forced_flush_drains_a_tail_larger_than_one_batch(self, client, agent, monkeypatch):
        """A ready high-watermark is coalesced into one bounded provider call."""
        from app.db import SessionLocal

        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        provider = agent

        # Buffer the whole meeting with the agent disabled: otherwise each FINAL
        # drains its own ready batch on arrival and the pending tail never grows
        # past one batch, so the per-run cap could never bite.
        monkeypatch.setattr("app.config.settings.meeting_agent_enabled", False)
        mid = _start_meeting(client)
        for seq in range(1, 9):
            _final(client, mid, seq, f"line {seq}")
        assert provider.calls == []
        monkeypatch.setattr("app.config.settings.meeting_agent_enabled", True)

        # One run consumes the captured range in one call.
        db = SessionLocal()
        try:
            report = state_service.process_pending(db, mid, provider=provider)
        finally:
            db.close()
        assert report["outcome"] == state_service.OUTCOME_OK
        assert report["remaining_segment_seqs"] == []
        assert provider.seqs_seen == [[1, 2, 3, 4, 5, 6, 7, 8]]

        # Finish does not create any additional realtime calls.
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200
        assert provider.seqs_seen == [[1, 2, 3, 4, 5, 6, 7, 8]]
        assert _state(client, mid)["last_processed_segment_seq"] == 8

    def test_finish_survives_the_agent_being_disabled(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_enabled", False)
        mid = _start_meeting(client)
        _final(client, mid, 1, "line 1")
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200


class TestFailurePolicy:
    def test_success_snapshot_and_failure_health_are_durable(
        self, client, agent, monkeypatch
    ):
        from app.db import SessionLocal
        from app.meeting_state import snapshot_repository
        from app.meeting_state.snapshot_model import MeetingStateHealth

        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        mid = _start_meeting(client)
        for seq in (1, 2):
            _final(client, mid, seq, f"line {seq}")

        with SessionLocal() as db:
            snapshots = snapshot_repository.list_for_meeting(db, mid)
            assert [(s.from_segment_seq, s.to_segment_seq) for s in snapshots] == [(1, 2)]
            health = db.get(MeetingStateHealth, mid)
            assert health is not None
            assert health.last_success_at is not None
            assert health.consecutive_failures == 0

        agent.error = "temporary outage"
        for seq in (3, 4):
            _final(client, mid, seq, f"line {seq}")

        with SessionLocal() as db:
            snapshots = snapshot_repository.list_for_meeting(db, mid)
            assert len(snapshots) == 1, "a failed window must not create a checkpoint"
            health = db.get(MeetingStateHealth, mid)
            assert health.consecutive_failures == 1
            assert health.last_error

    def test_invalid_llm_output_does_not_pollute_the_previous_state(
        self, client, agent, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)

        _final(client, mid, 1, "first")
        good = _state(client, mid)
        assert good["version"] == 1
        assert good["status"] == "ACTIVE"

        provider.raw = "{ this is not json"
        _final(client, mid, 2, "second")

        after = _state(client, mid)
        assert after["status"] == "STALE"
        assert after["version"] == 1, "a failed update is not a version"
        assert after["last_processed_segment_seq"] == 1, "the cursor must not advance"
        assert after["decisions"] == good["decisions"], "content must be untouched"

        row = _db_state(mid)
        assert row.last_error and "invalid JSON" in row.last_error

    def test_state_recovering_after_failure_replays_the_failed_segments(
        self, client, agent, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)
        _final(client, mid, 1, "first")

        provider.raw = "not json"
        _final(client, mid, 2, "second")
        assert _state(client, mid)["status"] == "STALE"

        provider.raw = None  # the model behaves again
        _final(client, mid, 3, "third")
        body = _state(client, mid)
        assert body["status"] == "ACTIVE"
        # Both the failed seq 2 and the new seq 3 are drained in this one run:
        # the cursor only advanced to 1 on the failure, so 2 is pending again.
        assert body["last_processed_segment_seq"] == 3
        assert provider.seqs_seen[-2:] == [[2], [2, 3]], (
            "the previously failed segment is replayed before the new one"
        )

    def test_provider_level_failure_is_reported_without_repair(self, client, agent):
        from app.meeting_state import updater

        provider = agent
        provider.error = "boom"
        result = updater.run_update(
            provider,
            "prompt",
            build_repair_prompt=lambda *a: "repair",
            max_attempts=3,
            repair_enabled=True,
        )
        assert not result.ok
        assert "boom" in result.reason
        assert len(provider.calls) == 1, "a transport failure is not retried"

    def test_agent_failure_does_not_block_transcript_or_sse(self, client, agent, monkeypatch):
        """The core failure-policy invariant: FINAL is already durable when the
        agent is scheduled, so a broken LLM cannot fail the transcript path."""
        # A single FINAL must actually reach the agent, hence threshold 1.
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        agent.error = "provider down"
        mid = _start_meeting(client)
        assert _final(client, mid, 1, "still saved").status_code == 200

        snapshot = client.get(f"/api/meetings/{mid}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1]
        assert _state(client, mid)["status"] == "FAILED"

    def test_oversized_state_fails_loudly_instead_of_truncating(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)

        _final(client, mid, 1, "first")
        assert _state(client, mid)["version"] == 1
        calls_after_first = len(provider.calls)

        # Now that the persisted state is non-trivial, drop the bound BELOW its
        # size: the next update must refuse rather than send a truncated prompt.
        from app.meeting_state.schemas import MeetingStateContent

        state_size = len(
            MeetingStateContent.model_validate(_state(client, mid)).model_dump_json()
        )
        monkeypatch.setattr("app.config.settings.meeting_agent_max_state_chars", state_size - 1)

        _final(client, mid, 2, "second")
        body = _state(client, mid)
        assert body["status"] == "STALE"
        assert body["last_processed_segment_seq"] == 1
        assert len(provider.calls) == calls_after_first, (
            "the oversized batch is never sent to the LLM"
        )
        row = _db_state(mid)
        assert row.last_error and "too large" in row.last_error

    def test_disabled_agent_is_inert(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_enabled", False)
        provider = agent
        mid = _start_meeting(client)

        assert _final(client, mid, 1, "line").status_code == 200
        assert provider.calls == []
        assert _db_state(mid) is None, "no row, no LLM, no state when disabled"
        assert _state(client, mid)["status"] == "EMPTY"
        # ...and the rest of the chain is unaffected.
        assert client.get(f"/api/meetings/{mid}/transcript").status_code == 200


class TestPersistence:
    def test_state_survives_a_fresh_session(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        _ = agent
        mid = _start_meeting(client)
        _final(client, mid, 1, "durable")
        before = _state(client, mid)

        from app.db import SessionLocal

        db = SessionLocal()
        try:
            restored = state_service.get_state(db, mid)
        finally:
            db.close()

        assert restored.version == before["version"]
        assert restored.last_processed_segment_seq == 1
        assert restored.model_name == "scripted-provider"

    def test_unreadable_state_is_refused_not_overwritten(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)
        _final(client, mid, 1, "first")

        # Corrupt the persisted payload behind the repository's back.
        from app.db import SessionLocal
        from app.meeting_state import repository as repo

        db = SessionLocal()
        try:
            row = repo.get(db, mid)
            row.state_json = "{not json"
            db.commit()
        finally:
            db.close()

        _final(client, mid, 2, "second")
        assert len(provider.calls) == 1, "an unreadable state stops the updater"
        assert _db_state(mid).state_json == "{not json", "must not be overwritten"

    def test_corrupt_row_read_is_loud(self, client):
        from app.db import SessionLocal
        from app.meeting_state import repository as repo

        mid = _start_meeting(client)
        db = SessionLocal()
        try:
            repo.upsert(
                db, mid,
                content=MeetingStateContent(),
                version=1, last_processed_segment_seq=0, status="ACTIVE",
            )
            row = repo.get(db, mid)
            row.state_json = "[]"
            db.commit()
            with pytest.raises(repo.MeetingStateCorruptError):
                repo.get_content(db, mid)
        finally:
            db.close()


# ==========================================================================
# integration
# ==========================================================================

class TestIntegrationChain:
    def test_full_incremental_chain(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_chars", 10_000)
        provider = agent
        mid = _start_meeting(client)

        # 1. meeting started, transcript arriving
        for seq in range(1, 5):
            assert _final(client, mid, seq, f"line {seq}").status_code == 200
        assert _state(client, mid)["version"] == 2

        # 2. more FINAL -> incremental update
        for seq in range(5, 7):
            _final(client, mid, seq, f"line {seq}")
        assert _state(client, mid)["version"] == 3
        assert _state(client, mid)["last_processed_segment_seq"] == 6

        # 3. one unprocessed segment; finish must not run realtime AI
        _final(client, mid, 7, "line 7")
        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200

        body = _state(client, mid)
        assert body["last_processed_segment_seq"] == 6
        assert body["version"] == 3

        # 4. the meeting still completes through the normal Summary chain
        stream_end = client.post(
            f"/api/internal/meetings/{mid}/stream-end",
            json={"event_id": "e_end", "meeting_id": mid,
                  "event_type": "TRANSCRIPT_STREAM_END"},
        )
        assert stream_end.status_code == 200
        assert client.get(f"/api/meetings/{mid}").json()["status"] == "COMPLETED"
        assert client.get(f"/api/meetings/{mid}/summary").status_code == 200

    def test_frozen_state_closes_out_after_the_stream_ends(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        _ = agent
        mid = _start_meeting(client)
        for seq in (1, 2):
            _final(client, mid, seq, f"line {seq}")
        assert _state(client, mid)["status"] == "ACTIVE"

        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200
        client.post(
            f"/api/internal/meetings/{mid}/stream-end",
            json={"event_id": "e_end", "meeting_id": mid,
                  "event_type": "TRANSCRIPT_STREAM_END"},
        )

        # A drain trigger after the freeze closes the state out.
        assert client.post(
            f"/api/internal/meetings/{mid}/state/process-pending"
        ).status_code == 200
        assert _state(client, mid)["status"] == "FROZEN"

    def test_debug_trigger_is_idempotent(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        provider = agent
        mid = _start_meeting(client)
        _final(client, mid, 1, "line")

        resp = client.post(f"/api/internal/meetings/{mid}/state/process-pending")
        assert resp.status_code == 200
        assert resp.json()["version_before"] == 1
        assert len(provider.calls) == 1, "nothing pending -> no extra LLM call"


class TestEvidenceTrace:
    def test_items_carry_source_segment_seqs(self, client, agent, monkeypatch):
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 1)
        _ = agent
        mid = _start_meeting(client)
        _final(client, mid, 1, "line 1")

        body = _state(client, mid)
        assert body["decisions"][0]["source_segment_seqs"] == [1]
        assert body["topics"][0]["source_segment_seqs"] == [1]

    def test_output_shape_matches_the_parse_gate(self):
        """The prompt's declared output contract and the validation model must
        describe the same object — otherwise the model is asked for a shape that
        cannot validate."""
        from app.meeting_state.prompts import OUTPUT_SHAPE
        from app.meeting_state.schemas import MeetingStateUpdate

        declared = json.loads(OUTPUT_SHAPE)
        produced = MeetingStateUpdate.model_validate(declared)
        assert set(produced.model_dump()) == set(declared)


class TestScriptedMeetingReplay:
    """§13: the deterministic six-segment meeting from the step spec.

    The spec is explicit that verbatim model wording must NOT be asserted — the
    scripted provider below plays the role of a competent model, emitting the
    semantic expectations (topic / decision / action item with owner+deadline /
    open question) keyed off the batch it is handed. What this class actually
    verifies is the PIPELINE contract around that content: schema validity, the
    cursor, batch boundaries, replay non-duplication, forced flush, source trace,
    and that a broken model turn cannot pollute the persisted state.
    """

    SCRIPT = [
        (1, "今天讨论上线时间"),
        (2, "我觉得下周一可能有点赶"),
        (3, "那就定下周三上线吧"),
        (4, "小王负责后端接口联调"),
        (5, "周二之前完成"),
        (6, "还有支付接口稳定性需要确认"),
    ]

    class ScriptedMeetingProvider:
        """Emits the §13 expectations for whichever segments it is shown.

        Being keyed off the seqs in the prompt (not off a call counter) is what
        makes it correct under batching: a batch of [3,4] must produce the
        decision AND the action item, a batch of [1,2,3] only the first two, and
        so on.
        """

        name = "scripted-meeting"

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.seqs_seen: list[list[int]] = []
            self.seqs_accumulated: set[int] = set()
            self.error: str | None = None
            self.raw: str | None = None

        def complete(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
            from app.agent.llm.base import SummaryGenerationError

            self.calls.append(prompt)
            if self.error:
                raise SummaryGenerationError(self.error)

            seqs = _seqs_in_batch(prompt)
            self.seqs_seen.append(seqs)
            self.seqs_accumulated.update(seqs)
            known = self.seqs_accumulated
            if self.raw is not None:
                return self.raw

            payload: dict = {
                "live_summary": None,
                "current_topic": None,
                "topics": [],
                "decisions": [],
                "action_items": [],
                "open_questions": [],
                "risks": [],
            }
            if known:
                payload["live_summary"] = "会议正在讨论上线安排、风险、决策与后续联调事项。"
            if 1 in known:
                payload["current_topic"] = "上线安排"
                payload["topics"] = [{
                    "title": "上线安排", "summary": "讨论上线时间与稳定性",
                    "source_segment_seqs": sorted(s for s in known if s in (1, 2)),
                }]
            if 2 in known:
                payload["risks"] = [{
                    "text": "下周一可能有点赶",
                    "source_segment_seqs": [2],
                }]
            if 3 in known:
                payload["decisions"] = [{
                    "text": "定下周三上线",
                    "source_segment_seqs": [3],
                }]
            if 4 in known:
                payload["action_items"] = [{
                    "task": "后端接口联调", "owner": "小王", "deadline": None,
                    "source_segment_seqs": [4],
                }]
            if 5 in known:
                payload["action_items"] = [{
                    "task": "后端接口联调", "owner": "小王", "deadline": "周二之前",
                    "source_segment_seqs": [4, 5],
                }]
            if 6 in known:
                payload["open_questions"] = [{
                    "text": "支付接口稳定性需要确认",
                    "source_segment_seqs": [6],
                }]
            return json.dumps(payload, ensure_ascii=False)

    @pytest.fixture
    def scripted(self, monkeypatch):
        provider = self.ScriptedMeetingProvider()
        monkeypatch.setattr("app.agent.llm.factory.get_provider", lambda **_policy: provider)
        # Two segments per batch: the meeting then spans exactly three batches,
        # so batching, incrementality and the trailing remainder are all real.
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 2)
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_chars", 10_000)
        return provider

    def _run_script(self, client, mid, seqs=None):
        for seq, text in self.SCRIPT:
            if seqs is not None and seq not in seqs:
                continue
            assert _final(client, mid, seq, text).status_code == 200

    def test_scripted_meeting_produces_the_expected_state(self, client, scripted):
        mid = _start_meeting(client)
        self._run_script(client, mid, seqs=(1, 2, 3, 4, 5, 6))

        body = _state(client, mid)
        # schema validity: the endpoint returned a shape the schema accepts
        MeetingStateContent.model_validate(
            {k: v for k, v in body.items() if k in PAYLOAD_FIELDS + ("live_summary", "current_topic",)}
        )
        assert body["live_summary"] == "会议正在讨论上线安排、风险、决策与后续联调事项。"
        assert body["current_topic"] == "上线安排"
        assert any("上线" in t["title"] for t in body["topics"])
        assert any("下周三" in d["text"] for d in body["decisions"])
        item = next(a for a in body["action_items"] if "联调" in a["task"])
        assert item["owner"] == "小王" and item["deadline"] == "周二之前"
        assert any("支付" in q["text"] for q in body["open_questions"])

    def test_cursor_tracks_the_last_processed_segment(self, client, scripted):
        mid = _start_meeting(client)
        self._run_script(client, mid)
        body = _state(client, mid)
        assert body["last_processed_segment_seq"] == 6
        # 6 segments / 2 per batch = 3 version increments (v1 -> v3)
        assert body["version"] == 3

    def test_every_item_cites_its_source_segments(self, client, scripted):
        mid = _start_meeting(client)
        self._run_script(client, mid)
        body = _state(client, mid)

        known = {seq for seq, _ in self.SCRIPT}
        cited = []
        for field in PAYLOAD_FIELDS:
            for item in body[field]:
                assert item["source_segment_seqs"], f"{field} item without evidence"
                cited.extend(item["source_segment_seqs"])
        assert set(cited) <= known
        # seq 4 and 5 are one action item split across two batches; the final
        # state must cite BOTH, i.e. the trace survived the incremental merge.
        item = next(a for a in body["action_items"] if "联调" in a["task"])
        assert item["source_segment_seqs"] == [4, 5]

    def test_replaying_the_whole_script_changes_nothing(self, client, scripted):
        """§4/§13: a replayed FINAL is skipped, never double-counted."""
        mid = _start_meeting(client)
        self._run_script(client, mid)
        before = _state(client, mid)
        calls_before = len(scripted.calls)

        # Identical seq+event_id+content: the ingress dedupes, the agent never runs.
        self._run_script(client, mid)

        after = _state(client, mid)
        assert len(scripted.calls) == calls_before, "replay must not call the LLM"
        assert after["version"] == before["version"]
        assert after["last_processed_segment_seq"] == before["last_processed_segment_seq"]
        assert after["decisions"] == before["decisions"]
        assert after["action_items"] == before["action_items"]

    def test_sub_threshold_tail_remains_transcript_only_at_finish(self, client, scripted, monkeypatch):
        """Buffered realtime input remains durable in Transcript at finish."""
        monkeypatch.setattr("app.config.settings.meeting_agent_batch_max_segments", 4)
        provider = scripted
        mid = _start_meeting(client)
        self._run_script(client, mid, seqs=(1, 2, 3, 4, 5, 6))

        # 6 segments with a threshold of 4 -> batch [1..4] flushed, [5,6] buffered.
        assert provider.seqs_seen == [[1, 2, 3, 4]]
        assert _state(client, mid)["last_processed_segment_seq"] == 4

        assert client.post(f"/api/meetings/{mid}/finish").status_code == 200
        body = _state(client, mid)
        assert body["last_processed_segment_seq"] == 4
        snapshot = client.get(f"/api/meetings/{mid}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1, 2, 3, 4, 5, 6]

    def test_invalid_model_output_does_not_pollute_the_state(self, client, scripted):
        mid = _start_meeting(client)
        self._run_script(client, mid, seqs=(1, 2, 3, 4))
        good = _state(client, mid)
        assert good["decisions"], "precondition: a real decision is persisted"

        scripted.raw = '{"decisions": "not a list", "topics": 5}'
        # Two segments, so the batch reaches the threshold and actually calls the
        # model — a lone trailing segment would just stay buffered.
        self._run_script(client, mid, seqs=(5, 6))

        after = _state(client, mid)
        assert after["status"] == "STALE"
        assert after["version"] == good["version"], "a failed update is not a version"
        assert after["last_processed_segment_seq"] == good["last_processed_segment_seq"]
        assert after["decisions"] == good["decisions"], "content must be untouched"
        assert after["topics"] == good["topics"]

    def test_agent_failure_leaves_the_transcript_intact(self, client, scripted):
        mid = _start_meeting(client)
        self._run_script(client, mid, seqs=(1, 2))
        scripted.error = "provider down"
        self._run_script(client, mid, seqs=(3, 4))

        snapshot = client.get(f"/api/meetings/{mid}/transcript").json()
        assert [s["segment_seq"] for s in snapshot["segments"]] == [1, 2, 3, 4]
        assert _state(client, mid)["status"] == "STALE"


# ==========================================================================
# schema
# ==========================================================================

class TestSchemas:
    def test_llm_output_cannot_carry_pipeline_owned_fields(self):
        from pydantic import ValidationError

        from app.meeting_state.schemas import MeetingStateUpdate

        with pytest.raises(ValidationError):
            MeetingStateUpdate.model_validate(
                {"version": 99, "last_processed_segment_seq": 42,
                 "topics": [], "decisions": [], "action_items": [],
                 "open_questions": [], "risks": []}
            )

    def test_owner_and_deadline_default_to_null(self):
        from app.meeting_state.schemas import MeetingStateUpdate

        parsed = MeetingStateUpdate.model_validate(
            {"action_items": [{"task": "联调", "source_segment_seqs": [4, 5]}]}
        )
        item = parsed.action_items[0]
        assert item.owner is None and item.deadline is None
        assert item.source_segment_seqs == [4, 5]

    def test_empty_lists_are_valid(self):
        from app.meeting_state.schemas import MeetingStateUpdate

        parsed = MeetingStateUpdate.model_validate({})
        assert parsed.current_topic is None
        for field in PAYLOAD_FIELDS:
            assert getattr(parsed, field) == []
