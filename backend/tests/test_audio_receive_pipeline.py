from __future__ import annotations

import asyncio
import threading
import time
import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.audio import router as audio_router


class FakeWebSocket:
    def __init__(self, frames: Iterable[bytes]) -> None:
        self._messages = [
            {"type": "websocket.receive", "bytes": frame} for frame in frames
        ]
        self._messages.append({"type": "websocket.disconnect"})
        self.accepted = False
        self.closed = False
        self.disconnect_received = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, object]:
        await asyncio.sleep(0)
        message = self._messages.pop(0)
        if message["type"] == "websocket.disconnect":
            self.disconnect_received.set()
        return message

    async def close(self, code: int, reason: str) -> None:
        del code, reason
        self.closed = True


class FakeTencentClient:
    def __init__(
        self,
        *,
        start_delay: float = 0.0,
        start_error: bool = False,
        feed_error_at: int | None = None,
        feed_gate: threading.Event | None = None,
    ) -> None:
        self.start_delay = start_delay
        self.start_error = start_error
        self.feed_error_at = feed_error_at
        self.feed_gate = feed_gate
        self.started = False
        self.ended = False
        self.start_calls = 0
        self.end_calls = 0
        self.feed_calls: list[bytes] = []
        self.feed_times: list[float] = []
        self.on_sentence = None

    def start_stream(self) -> None:
        self.start_calls += 1
        if self.start_delay:
            time.sleep(self.start_delay)
        if self.start_error:
            raise RuntimeError("fake start failure")
        self.started = True

    def feed_audio(self, data: bytes) -> None:
        self.feed_calls.append(data)
        self.feed_times.append(time.monotonic())
        if self.feed_gate is not None:
            self.feed_gate.wait(timeout=2.0)
        if self.feed_error_at == len(self.feed_calls):
            raise RuntimeError("fake feed failure")

    def end_stream(self) -> None:
        self.end_calls += 1
        self.ended = True


def _client_factory(client: FakeTencentClient):
    def create(*, device_id: str, on_sentence=None) -> FakeTencentClient:
        del device_id
        client.on_sentence = on_sentence
        return client

    return create


def _run_audio_handler(
    audio_dir: Path,
    frames: list[bytes],
    client: FakeTencentClient,
) -> FakeWebSocket:
    websocket = FakeWebSocket(frames)
    with (
        patch.object(audio_router, "AUDIO_DIR", audio_dir),
        patch.object(
            audio_router,
            "TencentRealtimeAsrClient",
            _client_factory(client),
        ),
    ):
        asyncio.run(audio_router.receive_audio(websocket, "ESP32-001"))
    return websocket


class TestAudioReceivePipeline(unittest.TestCase):
    def setUp(self) -> None:
        audio_router.audio_session_manager.clear()
        self._gain_patch = patch.object(
            audio_router.settings,
            "asr_audio_gain",
            1.0,
        )
        self._meeting_patch = patch.object(
            audio_router,
            "_resolve_active_meeting",
            return_value="meeting-audio-test",
        )
        self._gain_patch.start()
        self._meeting_patch.start()

    def tearDown(self) -> None:
        self._meeting_patch.stop()
        self._gain_patch.stop()
        audio_router.audio_session_manager.clear()

    def test_valid_frames_are_recorded_and_aggregated(self) -> None:
        for frame_count, expected_feed_count in ((5, 1), (10, 2)):
            with self.subTest(frame_count=frame_count), TemporaryDirectory() as temp_dir:
                audio_dir = Path(temp_dir)
                frames = [
                    bytes([index % 256]) * audio_router.FRAME_BYTES
                    for index in range(frame_count)
                ]
                client = FakeTencentClient()

                websocket = _run_audio_handler(audio_dir, frames, client)

                recording = next(audio_dir.glob("*.pcm"))
                self.assertTrue(websocket.accepted)
                self.assertEqual(recording.read_bytes(), b"".join(frames))
                self.assertEqual(
                    recording.stat().st_size,
                    frame_count * audio_router.FRAME_BYTES,
                )
                self.assertEqual(len(client.feed_calls), expected_feed_count)
                self.assertTrue(
                    all(
                        len(chunk) == audio_router.ASR_CHUNK_BYTES
                        for chunk in client.feed_calls
                    )
                )
                self.assertTrue(client.started)
                self.assertTrue(client.ended)
                self.assertEqual(client.start_calls, 1)
                self.assertEqual(client.end_calls, 1)

    def test_invalid_frame_is_not_recorded_or_sent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            client = FakeTencentClient()

            _run_audio_handler(audio_dir, [b"x" * 1000], client)

            recording = next(audio_dir.glob("*.pcm"))
            self.assertEqual(recording.stat().st_size, 0)
            self.assertEqual(client.feed_calls, [])
            self.assertTrue(client.ended)

    def test_incomplete_tail_is_zero_padded_and_sent_before_shutdown(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            frames = [b"x" * audio_router.FRAME_BYTES] * 4
            client = FakeTencentClient()

            _run_audio_handler(audio_dir, frames, client)

            self.assertEqual(
                next(audio_dir.glob("*.pcm")).stat().st_size, 5120
            )
            self.assertEqual(len(client.feed_calls), 1)
            self.assertEqual(
                client.feed_calls[0],
                (b"x" * 5120) + (b"\x00" * 1280),
            )
            self.assertTrue(client.ended)

    def test_finish_signal_closes_socket_after_pipeline_drains(self) -> None:
        class BlockingWebSocket:
            def __init__(self) -> None:
                self.accepted = False
                self.closed = False

            async def accept(self) -> None:
                self.accepted = True

            async def receive(self) -> dict[str, object]:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self, code: int, reason: str) -> None:
                del code, reason
                self.closed = True

        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            websocket = BlockingWebSocket()
            client = FakeTencentClient()

            async def run_scenario() -> None:
                handler = asyncio.create_task(
                    audio_router.receive_audio(websocket, "ESP32-001")
                )
                for _ in range(20):
                    session = audio_router.audio_session_manager.get("ESP32-001")
                    if session is not None:
                        break
                    await asyncio.sleep(0)
                else:
                    self.fail("audio session was not registered")

                stopping = audio_router.audio_session_manager.begin_stop(
                    "ESP32-001", "meeting-audio-test"
                )
                await audio_router.audio_session_manager.wait_until_drained(
                    stopping, timeout_seconds=1.0
                )
                await handler
                audio_router.audio_session_manager.end_stop("ESP32-001")

            with (
                patch.object(audio_router, "AUDIO_DIR", audio_dir),
                patch.object(
                    audio_router,
                    "TencentRealtimeAsrClient",
                    _client_factory(client),
                ),
            ):
                asyncio.run(run_scenario())

            self.assertTrue(websocket.accepted)
            self.assertTrue(websocket.closed)
            self.assertTrue(client.ended)
            self.assertIsNone(
                audio_router.audio_session_manager.get("ESP32-001")
            )

    def test_blocked_asr_feed_does_not_block_websocket_receiver(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            feed_gate = threading.Event()
            client = FakeTencentClient(feed_gate=feed_gate)
            websocket = FakeWebSocket(
                [b"x" * audio_router.FRAME_BYTES] * 10
            )

            async def run_scenario() -> None:
                started_at = time.monotonic()
                handler_task = asyncio.create_task(
                    audio_router.receive_audio(websocket, "ESP32-001")
                )
                await asyncio.wait_for(
                    websocket.disconnect_received.wait(), timeout=0.5
                )
                self.assertLess(time.monotonic() - started_at, 0.5)
                self.assertFalse(handler_task.done())
                feed_gate.set()
                await handler_task

            with (
                patch.object(audio_router, "AUDIO_DIR", audio_dir),
                patch.object(
                    audio_router,
                    "TencentRealtimeAsrClient",
                    _client_factory(client),
                ),
            ):
                asyncio.run(run_scenario())

            self.assertEqual(
                next(audio_dir.glob("*.pcm")).stat().st_size, 12800
            )
            self.assertEqual(len(client.feed_calls), 2)
            self.assertTrue(client.ended)

    def test_feed_failure_keeps_recording_and_still_ends_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            frames = [b"x" * audio_router.FRAME_BYTES] * 10
            client = FakeTencentClient(feed_error_at=1)

            _run_audio_handler(audio_dir, frames, client)

            self.assertEqual(
                next(audio_dir.glob("*.pcm")).stat().st_size, 12800
            )
            self.assertEqual(len(client.feed_calls), 1)
            self.assertTrue(client.ended)
            self.assertEqual(client.end_calls, 1)

    def test_start_failure_keeps_recording_without_worker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            frames = [b"x" * audio_router.FRAME_BYTES] * 5
            client = FakeTencentClient(start_error=True)

            _run_audio_handler(audio_dir, frames, client)

            self.assertEqual(
                next(audio_dir.glob("*.pcm")).stat().st_size, 6400
            )
            self.assertEqual(client.feed_calls, [])
            self.assertFalse(client.ended)
            self.assertEqual(client.start_calls, 1)
            self.assertEqual(client.end_calls, 0)

    def test_slow_start_does_not_block_receiver_and_queue_stays_bounded(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            client = FakeTencentClient(start_delay=2.0)
            websocket = FakeWebSocket(
                [b"x" * audio_router.FRAME_BYTES] * 50
            )

            async def run_scenario() -> None:
                started_at = time.monotonic()
                handler_task = asyncio.create_task(
                    audio_router.receive_audio(websocket, "ESP32-001")
                )
                await asyncio.wait_for(
                    websocket.disconnect_received.wait(), timeout=0.5
                )
                self.assertLess(time.monotonic() - started_at, 0.5)
                self.assertFalse(handler_task.done())
                await handler_task

            with (
                patch.object(audio_router, "AUDIO_DIR", audio_dir),
                patch.object(
                    audio_router,
                    "TencentRealtimeAsrClient",
                    _client_factory(client),
                ),
            ):
                asyncio.run(run_scenario())

            self.assertEqual(
                next(audio_dir.glob("*.pcm")).stat().st_size, 64000
            )
            self.assertEqual(len(client.feed_calls), 5)
            self.assertEqual(client.start_calls, 1)
            self.assertEqual(client.end_calls, 1)

    def test_backlog_chunks_are_paced_at_200ms(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_dir = Path(temp_dir)
            client = FakeTencentClient(start_delay=0.1)
            frames = [b"x" * audio_router.FRAME_BYTES] * 15

            _run_audio_handler(audio_dir, frames, client)

            self.assertEqual(len(client.feed_times), 3)
            intervals = [
                later - earlier
                for earlier, later in zip(
                    client.feed_times, client.feed_times[1:]
                )
            ]
            self.assertTrue(all(interval >= 0.18 for interval in intervals))
            self.assertEqual(client.end_calls, 1)

    def test_queue_full_drops_oldest_and_keeps_latest(self) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=audio_router.ASR_QUEUE_MAXSIZE
        )
        state = audio_router._AsrSessionState(started=True, healthy=True)
        frames = [
            bytes([index]) * audio_router.FRAME_BYTES for index in range(26)
        ]
        for frame in frames[:25]:
            queue.put_nowait(frame)

        audio_router._enqueue_asr_frame(
            queue, frames[25], "ESP32-001", state
        )

        queued_frames = [
            queue.get_nowait() for _ in range(queue.qsize())
        ]
        self.assertEqual(queued_frames, frames[1:])
        self.assertEqual(state.queue_drop_count, 1)
