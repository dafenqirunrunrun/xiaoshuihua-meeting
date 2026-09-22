from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select

from app.audio import router as audio_router
from app.config import settings
from app.db import SessionLocal
from app.device.meeting_binding import (
    bind_device_to_meeting,
    device_meeting_bindings,
    get_active_meeting,
)
from app.meeting.meeting_model import Meeting
from app.speech.result_parser import TencentSentenceResult
from app.speech.transcript_dispatcher import dispatch_sentence_result
from app.speech import transcript_dispatcher
from app.transcript.live_cache import live_utterance_cache
from app.transcript.segment_model import TranscriptSegment


def _sentence(
    *,
    voice_id: str = "voice-A",
    sentence_id: int = 1,
    sentence_type: int = 0,
    speaker_id: int = 0,
    text: str = "今天",
) -> TencentSentenceResult:
    return TencentSentenceResult(
        voice_id=voice_id,
        sentence_id=sentence_id,
        sentence_type=sentence_type,
        speaker_id=speaker_id,
        start_ms=100,
        end_ms=300,
        text=text,
    )


def _create_meeting(meeting_id: str, status: str = "IN_PROGRESS") -> None:
    db = SessionLocal()
    try:
        db.add(Meeting(id=meeting_id, title=meeting_id, status=status))
        db.commit()
    finally:
        db.close()


def _segment_count() -> int:
    db = SessionLocal()
    try:
        return db.scalar(select(func.count()).select_from(TranscriptSegment)) or 0
    finally:
        db.close()


def test_meeting_start_binding_conflict_and_stale_replacement(client) -> None:
    first = client.post("/api/meetings", json={"title": "first"}).json()["id"]
    second = client.post("/api/meetings", json={"title": "second"}).json()["id"]

    assert client.post(f"/api/meetings/{first}/start").status_code == 200
    assert device_meeting_bindings.get(settings.shuihua_audio_device_id) == first
    assert client.post(f"/api/meetings/{first}/start").status_code == 200

    conflict = client.post(f"/api/meetings/{second}/start")
    assert conflict.status_code == 409
    assert client.get(f"/api/meetings/{second}").json()["status"] == "CREATED"

    assert client.post(f"/api/meetings/{first}/finish").status_code == 200
    db = SessionLocal()
    try:
        assert get_active_meeting(db, settings.shuihua_audio_device_id) is None
    finally:
        db.close()
    # The raw stale binding remains until a new active meeting replaces it.
    assert device_meeting_bindings.get(settings.shuihua_audio_device_id) == first

    assert client.post(f"/api/meetings/{second}/start").status_code == 200
    assert device_meeting_bindings.get(settings.shuihua_audio_device_id) == second


def test_dispatch_resolves_device_for_each_sentence_and_preserves_domain_rules() -> None:
    _create_meeting("meeting-A")
    db = SessionLocal()
    try:
        bind_device_to_meeting(db, "ESP32-001", "meeting-A")
    finally:
        db.close()

    assert dispatch_sentence_result("ESP32-002", _sentence()) is None
    assert _segment_count() == 0

    assert dispatch_sentence_result(
        "ESP32-001", _sentence(text="今天")
    ) == "meeting-A"
    assert dispatch_sentence_result(
        "ESP32-001", _sentence(text="今天我们")
    ) == "meeting-A"
    assert _segment_count() == 0
    assert live_utterance_cache.get("meeting-A", "voice-A:1")["text"] == "今天我们"

    final = _sentence(sentence_type=1, speaker_id=1, text="今天我们开始会议")
    assert dispatch_sentence_result("ESP32-001", final) == "meeting-A"
    assert dispatch_sentence_result("ESP32-001", final) == "meeting-A"
    assert _segment_count() == 1
    assert live_utterance_cache.get("meeting-A", "voice-A:1") is None

    second_session = _sentence(
        voice_id="voice-B",
        sentence_type=1,
        speaker_id=0,
        text="另一会话",
    )
    dispatch_sentence_result("ESP32-001", second_session)

    db = SessionLocal()
    try:
        segments = list(
            db.scalars(select(TranscriptSegment).order_by(TranscriptSegment.segment_seq))
        )
        assert [segment.utterance_id for segment in segments] == [
            "voice-A:1",
            "voice-B:1",
        ]
        assert segments[0].speaker_id == "unknown"
    finally:
        db.close()


def test_no_binding_skips_without_creating_a_segment() -> None:
    _create_meeting("meeting-unbound")

    assert dispatch_sentence_result(
        "ESP32-001", _sentence(sentence_type=1, text="不能落库")
    ) is None
    assert _segment_count() == 0


def test_product_finish_freezes_transcript_and_starts_summary(client) -> None:
    meeting_id = client.post(
        "/api/meetings", json={"title": "product finish"}
    ).json()["id"]
    assert client.post(f"/api/meetings/{meeting_id}/start").status_code == 200

    response = client.post(
        f"/api/meetings/{meeting_id}/finish?finalize_audio=true"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FINALIZING"

    meeting = client.get(f"/api/meetings/{meeting_id}").json()
    summary = client.get(f"/api/meetings/{meeting_id}/summary").json()
    assert meeting["status"] == "COMPLETED"
    assert summary["status"] == "COMPLETED"


def test_real_dispatch_worker_schedules_state_agent_only_after_final(
    monkeypatch,
) -> None:
    scheduled: list[str] = []
    monkeypatch.setattr(
        transcript_dispatcher,
        "dispatch_sentence_result",
        lambda _device_id, _sentence: "meeting-live",
    )
    monkeypatch.setattr(
        "app.meeting_state.scheduler.meeting_state_scheduler.schedule",
        scheduled.append,
    )

    async def run() -> None:
        queue: asyncio.Queue[TencentSentenceResult | None] = asyncio.Queue()
        await queue.put(_sentence(sentence_type=0, text="partial"))
        await queue.put(_sentence(sentence_type=1, text="final"))
        await queue.put(None)
        await transcript_dispatcher.transcript_dispatch_worker(queue, "ESP32-001")

    asyncio.run(run())
    assert scheduled == ["meeting-live"]


class _DisconnectingWebSocket:
    async def accept(self) -> None:
        return None

    async def receive(self) -> dict[str, str]:
        await asyncio.sleep(0)
        return {"type": "websocket.disconnect"}


class _TrailingFinalTencentClient:
    def __init__(self, *, device_id: str, on_sentence) -> None:
        self.device_id = device_id
        self.on_sentence = on_sentence

    def start_stream(self) -> None:
        return None

    def feed_audio(self, _data: bytes) -> None:
        return None

    def end_stream(self) -> None:
        callback_thread = threading.Thread(
            target=self.on_sentence,
            args=(
                _sentence(
                    sentence_type=1,
                    speaker_id=2,
                    text="断开前最后一句",
                ),
            ),
        )
        callback_thread.start()
        callback_thread.join(timeout=2)
        assert not callback_thread.is_alive()


def test_receiver_thread_handoff_keeps_trailing_final_during_shutdown() -> None:
    _create_meeting("meeting-trailing")
    db = SessionLocal()
    try:
        bind_device_to_meeting(db, "ESP32-001", "meeting-trailing")
    finally:
        db.close()

    with TemporaryDirectory() as temp_dir, patch.object(
        audio_router, "AUDIO_DIR", Path(temp_dir)
    ), patch.object(
        audio_router,
        "TencentRealtimeAsrClient",
        _TrailingFinalTencentClient,
    ):
        asyncio.run(
            audio_router.receive_audio(_DisconnectingWebSocket(), "ESP32-001")
        )

    db = SessionLocal()
    try:
        segment = db.scalar(select(TranscriptSegment))
        assert segment is not None
        assert segment.text == "断开前最后一句"
        assert segment.speaker_id == "unknown"
    finally:
        db.close()
