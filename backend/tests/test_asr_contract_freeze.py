"""ASR-0B contract-freeze regression suite.

Covers the five decisions frozen in ASR-0B:

  1. `last_segment_seq` optional + `ge=0`; omitted -> server derives max(seq) or 0
  2. STREAM_END ownership: segment_seq stays Transcript-owned; the explicit
     field is a compatibility / extra-consistency capability only
  3. multi-ASR-session timeline: start_ms/end_ms are session-local offsets;
     ordering is ONLY ever segment_seq
  4. PARTIAL after FINAL: an utterance that reached FINAL is closed -> 409
  5. asrSessionId maxLength=48 exposed as a contract bound

Tests run against the in-process ASGI app (no live server, no network), with a
fresh SQLite schema per test — see conftest.py.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _asr(meeting_id: str, sentence_id: int, kind: str, **over) -> dict:
    body = {
        "type": f"transcript.{kind}",
        "meetingId": meeting_id,
        "asrSessionId": "sess-1",
        "sentenceId": sentence_id,
        "speakerId": 0,
        "startMs": 1000,
        "endMs": 2000,
        "text": f"sentence-{sentence_id}",
        "provider": "tencent",
    }
    body.update(over)
    return body


def _create(client, title: str = "t", *, start: bool = True) -> str:
    mid = client.post("/api/meetings", json={"title": title}).json()["id"]
    if start:
        assert client.post(f"/api/meetings/{mid}/start").status_code == 200
    return mid


def _asr_post(client, meeting_id: str, sentence_id: int, kind: str, **over):
    return client.post(
        f"/api/internal/meetings/{meeting_id}/asr-events",
        json=_asr(meeting_id, sentence_id, kind, **over),
    )


def _finish(client, meeting_id: str):
    return client.post(f"/api/meetings/{meeting_id}/finish")


def _stream_end(client, meeting_id: str, **over):
    body = {
        "event_id": "evt-end",
        "meeting_id": meeting_id,
        "event_type": "TRANSCRIPT_STREAM_END",
    }
    body.update(over)
    return client.post(
        f"/api/internal/meetings/{meeting_id}/stream-end", json=body
    )


def _seqs(client, meeting_id: str) -> list[int]:
    segs = client.get(f"/api/meetings/{meeting_id}/transcript").json()["segments"]
    return [s["segment_seq"] for s in segs]


# ==========================================================================
# 1. last_segment_seq optional / ge=0 / server-derived
# ==========================================================================

class TestStreamEndOptional:
    def test_empty_meeting_can_freeze_by_omitting_last_seq(self, client):
        """An EMPTY meeting must be able to leave FINALIZING.

        Regression: `ge=1` + "required" made a zero-segment meeting permanently
        stuck in FINALIZING — no client-supplied value could ever satisfy the
        integrity check (0 -> 422, 1 -> 409 missing [1]).
        """
        mid = _create(client, "empty")
        _finish(client, mid)

        r = _stream_end(client, mid)  # last_segment_seq omitted
        assert r.status_code == 200, r.text
        assert r.json()["transcript_complete"] is True
        assert r.json()["last_segment_seq"] == 0

    def test_empty_meeting_can_freeze_with_explicit_zero(self, client):
        mid = _create(client, "empty-explicit")
        _finish(client, mid)

        r = _stream_end(client, mid, last_segment_seq=0)
        assert r.status_code == 200, r.text
        assert r.json()["last_segment_seq"] == 0

    def test_empty_meeting_reaches_completed_via_finalizer(self, client):
        """The whole point: an empty meeting must reach a terminal state."""
        mid = _create(client, "empty-complete")
        _finish(client, mid)
        assert _stream_end(client, mid).status_code == 200

        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "COMPLETED"

        summary = client.get(f"/api/meetings/{mid}/summary")
        assert summary.status_code == 200
        assert summary.json()["status"] == "COMPLETED"

    def test_omitted_last_seq_derives_from_persisted_max(self, client):
        mid = _create(client, "derive")
        for sid in (1, 2, 3):
            assert _asr_post(client, mid, sid, "final").status_code == 200
        _finish(client, mid)

        r = _stream_end(client, mid)
        assert r.status_code == 200, r.text
        assert r.json()["last_segment_seq"] == 3

    def test_supplied_last_seq_still_strictly_validated(self, client):
        """Decision 2: the explicit field keeps the full gap/overrun check."""
        mid = _create(client, "strict")
        _asr_post(client, mid, 1, "final")
        _finish(client, mid)

        r = _stream_end(client, mid, last_segment_seq=5)
        assert r.status_code == 409
        assert "missing segments" in r.json()["detail"]

    def test_supplied_last_seq_matching_max_is_accepted(self, client):
        mid = _create(client, "match")
        _asr_post(client, mid, 1, "final")
        _asr_post(client, mid, 2, "final")
        _finish(client, mid)

        r = _stream_end(client, mid, last_segment_seq=2)
        assert r.status_code == 200
        assert r.json()["last_segment_seq"] == 2

    def test_negative_last_seq_rejected(self, client):
        mid = _create(client, "neg")
        _finish(client, mid)
        assert _stream_end(client, mid, last_segment_seq=-1).status_code == 422

    def test_idempotent_retry_with_omitted_sequence_reuses_terminal(self, client):
        """Second call omitting the field derives the SAME 0 -> idempotent 200."""
        mid = _create(client, "empty-retry")
        _finish(client, mid)

        first = _stream_end(client, mid, event_id="e1")
        assert first.status_code == 200

        second = _stream_end(client, mid, event_id="e2")  # omitted again
        assert second.status_code == 200
        assert second.json()["last_segment_seq"] == 0

    def test_still_rejects_zero_segment_stream_end_state_gate(self, client):
        """A CREATED meeting still cannot stream-end, empty or not."""
        mid = _create(client, "created", start=False)
        assert _stream_end(client, mid).status_code == 409

    def test_new_final_after_empty_freeze_is_rejected(self, client):
        """Freezing at 0 still closes the transcript: no silent append."""
        mid = _create(client, "empty-then-append")
        _finish(client, mid)
        assert _stream_end(client, mid).status_code == 200

        r = _asr_post(client, mid, 1, "final")
        assert r.status_code == 409
        assert _seqs(client, mid) == []


# ==========================================================================
# 2. ordering ownership — segment_seq is Transcript-owned, arrival-ordered
# ==========================================================================

class TestOrderingOwnership:
    def test_segment_seq_is_arrival_ordered_not_sentence_ordered(self, client):
        """`segment_seq = sentenceId` is FORBIDDEN; out-of-order arrival is fine.

        Sentence ids deliberately arrive 3, 1, 2 -> the transcript ordering is
        the arrival ordering 1, 2, 3, never the sentence numbering.
        """
        mid = _create(client, "arrival")
        for sid in (3, 1, 2):
            assert _asr_post(client, mid, sid, "final").status_code == 200

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["segment_seq"] for s in segments] == [1, 2, 3]
        assert [s["utterance_id"] for s in segments] == ["sess-1:3", "sess-1:1", "sess-1:2"]

    def test_replayed_old_utterance_reuses_its_sequence(self, client):
        """A late retry must not consume a new number (or it would collide)."""
        mid = _create(client, "retry")
        _asr_post(client, mid, 1, "final", text="one")
        _asr_post(client, mid, 2, "final", text="two")
        assert _seqs(client, mid) == [1, 2]

        r = _asr_post(client, mid, 1, "final", text="one")  # identical replay
        assert r.status_code == 200
        assert _seqs(client, mid) == [1, 2]

    def test_conflicting_replay_of_same_sentence_is_a_loud_409(self, client):
        """Same ASR sentence identity, different content -> never silent."""
        mid = _create(client, "conflict")
        _asr_post(client, mid, 1, "final", text="original")

        r = _asr_post(client, mid, 1, "final", text="TAMPERED")
        assert r.status_code == 409
        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert segments[0]["text"] == "original"

    def test_unknown_field_is_rejected_loudly(self, client):
        """Contract drift (e.g. a smuggled segmentSeq) must fail, not be ignored."""
        mid = _create(client, "drift")
        r = _asr_post(client, mid, 1, "final", segmentSeq=1)
        assert r.status_code == 422

    def test_timing_is_not_used_for_ordering(self, client):
        """start_ms/end_ms never influence ordering — only segment_seq does."""
        mid = _create(client, "timing")
        assert _asr_post(client, mid, 1, "final", startMs=9000, endMs=9500).status_code == 200
        assert _asr_post(client, mid, 2, "final", startMs=100, endMs=200).status_code == 200

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["segment_seq"] for s in segments] == [1, 2]
        assert [s["start_ms"] for s in segments] == [9000, 100]


# ==========================================================================
# 3. multi-ASR-session timeline
# ==========================================================================

class TestMultiSessionTimeline:
    def test_reconnect_session_keeps_identities_distinct(self, client):
        """A reconnect restarts sentenceId; utterances must NOT collide."""
        mid = _create(client, "reconnect")
        assert _asr_post(client, mid, 1, "final", asrSessionId="A", text="A-first").status_code == 200
        assert _asr_post(client, mid, 1, "final", asrSessionId="B", text="B-first").status_code == 200

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["utterance_id"] for s in segments] == ["A:1", "B:1"]
        assert [s["segment_seq"] for s in segments] == [1, 2]

    def test_session_local_offsets_are_preserved_verbatim(self, client):
        """MVP does no rebase: offsets are stored as the session reported them.

        Both sessions restart their clock at 0. We keep both raw values and do
        NOT attempt a meeting-global timeline (documented V0.2 tech debt) — the
        ordering remains unambiguous because it is segment_seq.
        """
        mid = _create(client, "no-rebase")
        _asr_post(client, mid, 1, "final", asrSessionId="A", startMs=0, endMs=5000)
        _asr_post(client, mid, 1, "final", asrSessionId="B", startMs=0, endMs=900)

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [(s["start_ms"], s["end_ms"]) for s in segments] == [(0, 5000), (0, 900)]
        assert [s["segment_seq"] for s in segments] == [1, 2]

    def test_same_session_id_continues_one_utterance_identity(self, client):
        mid = _create(client, "same-session")
        _asr_post(client, mid, 7, "partial", text="typing")
        _asr_post(client, mid, 7, "final", text="done")

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["utterance_id"] for s in segments] == ["sess-1:7"]


# ==========================================================================
# 4. utterance closure — PARTIAL after FINAL is rejected
# ==========================================================================

class TestUtteranceClosure:
    def test_partial_after_final_is_rejected(self, client):
        """A finalized utterance is closed; a late PARTIAL must not be cached.

        Before this freeze the late PARTIAL was accepted into the live cache,
        which renders a permanent "识别中" line for an utterance whose FINAL had
        already arrived and would never be re-sent as a live frame.
        """
        mid = _create(client, "closed")
        assert _asr_post(client, mid, 1, "partial", text="typing").status_code == 200
        assert _asr_post(client, mid, 1, "final", text="done").status_code == 200

        r = _asr_post(client, mid, 1, "partial", text="late frame")
        assert r.status_code == 409
        assert "closed" in r.json()["detail"]

    def test_rejection_does_not_disturb_the_persisted_final(self, client):
        mid = _create(client, "closed-intact")
        _asr_post(client, mid, 1, "final", text="done")
        _asr_post(client, mid, 1, "partial", text="late")

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert [s["text"] for s in segments] == ["done"]

    def test_other_utterances_still_accept_partials(self, client):
        """Closure is per-utterance, not per-meeting."""
        mid = _create(client, "other-open")
        _asr_post(client, mid, 1, "final", text="done")

        assert _asr_post(client, mid, 2, "partial", text="still speaking").status_code == 200
        assert _asr_post(client, mid, 3, "partial", text="another").status_code == 200

    def test_repeated_partials_before_final_still_allowed(self, client):
        """In-place revision is the normal path — closure must not break it."""
        mid = _create(client, "revisions")
        for text in ("a", "ab", "abc"):
            assert _asr_post(client, mid, 1, "partial", text=text).status_code == 200
        assert _asr_post(client, mid, 1, "final", text="abc.").status_code == 200

    def test_legacy_internal_ingress_also_enforces_closure(self, client):
        """Closure is a domain invariant, so BOTH ingresses honour it."""
        mid = _create(client, "legacy-closure")
        base = {
            "meeting_id": mid,
            "utterance_id": "utt_1",
            "speaker_id": "speaker_0",
            "text": "done",
            "status": "FINAL",
            "segment_seq": 1,
            "start_ms": 0,
            "end_ms": 10,
            "emitted_at": 1,
        }
        assert client.post(
            f"/api/internal/meetings/{mid}/transcript-events",
            json={**base, "event_id": "evt_1"},
        ).status_code == 200

        r = client.post(
            f"/api/internal/meetings/{mid}/transcript-events",
            json={**base, "event_id": "evt_2", "status": "PARTIAL", "segment_seq": None},
        )
        assert r.status_code == 409


# ==========================================================================
# 5. asrSessionId maxLength=48 is a published contract bound
# ==========================================================================

class TestSessionIdBound:
    def test_48_char_session_id_accepted(self, client):
        mid = _create(client, "sid-48")
        assert _asr_post(client, mid, 1, "final", asrSessionId="a" * 48).status_code == 200

    def test_49_char_session_id_rejected(self, client):
        mid = _create(client, "sid-49")
        r = _asr_post(client, mid, 1, "final", asrSessionId="a" * 49)
        assert r.status_code == 422

    def test_derived_utterance_id_fits_the_internal_bound(self, client):
        """The 48 bound exists so `{session}:{sentence}` fits the 64-char id."""
        mid = _create(client, "sid-bound")
        assert _asr_post(
            client, mid, 2147483647, "final", asrSessionId="a" * 48
        ).status_code == 200

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert len(segments[0]["utterance_id"]) <= 64

    def test_bound_is_published_in_the_openapi_schema(self, client):
        schema = client.get("/openapi.json").json()
        prop = schema["components"]["schemas"]["AsrTranscriptEvent"]["properties"]
        assert prop["asrSessionId"]["maxLength"] == 48

    def test_stream_end_optionality_is_published_in_openapi(self, client):
        schema = client.get("/openapi.json").json()
        props = schema["components"]["schemas"]["StreamEndEvent"]["properties"]
        assert "last_segment_seq" in props
        # required must NOT include it any more
        assert "last_segment_seq" not in schema["components"]["schemas"]["StreamEndEvent"].get(
            "required", []
        )


# ==========================================================================
# error matrix — the contract surface around the freeze
# ==========================================================================

class TestErrorMatrix:
    def test_unknown_event_type_rejected(self, client):
        mid = _create(client, "bad-type")
        r = client.post(
            f"/api/internal/meetings/{mid}/asr-events",
            json=_asr(mid, 1, "bogus"),
        )
        assert r.status_code == 422

    def test_path_payload_meeting_mismatch(self, client):
        mid = _create(client, "mismatch")
        r = client.post(
            f"/api/internal/meetings/{mid}/asr-events",
            json=_asr(mid, 1, "final", meetingId="meeting_other"),
        )
        assert r.status_code == 400

    def test_missing_meeting_404(self, client):
        r = client.post(
            "/api/internal/meetings/meeting_nope/asr-events",
            json=_asr("meeting_nope", 1, "final"),
        )
        assert r.status_code == 404

    def test_end_before_start_rejected(self, client):
        mid = _create(client, "bad-timing")
        r = _asr_post(client, mid, 1, "final", startMs=5000, endMs=100)
        assert r.status_code == 422

    def test_events_rejected_in_created_state(self, client):
        mid = _create(client, "created-gate", start=False)
        assert _asr_post(client, mid, 1, "final").status_code == 409

    def test_speaker_minus_one_maps_to_unknown(self, client):
        mid = _create(client, "unknown-speaker")
        assert _asr_post(client, mid, 1, "final", speakerId=-1).status_code == 200

        segments = client.get(f"/api/meetings/{mid}/transcript").json()["segments"]
        assert segments[0]["speaker_id"] == "unknown"

    def test_negative_speaker_below_minus_one_rejected(self, client):
        mid = _create(client, "bad-speaker")
        assert _asr_post(client, mid, 1, "final", speakerId=-2).status_code == 422
