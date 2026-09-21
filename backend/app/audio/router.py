import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import re
import threading
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.audio.pcm import apply_pcm16_gain
from app.audio.session_manager import AudioSession, audio_session_manager
from app.config import settings
from app.db import SessionLocal
from app.device.meeting_binding import get_active_meeting
from app.speech.result_parser import TencentSentenceResult
from app.speech.tencent_client import TencentRealtimeAsrClient
from app.speech.transcript_dispatcher import transcript_dispatch_worker

router = APIRouter(tags=["audio"])
logger = logging.getLogger(__name__)
AUDIO_DIR = Path(__file__).resolve().parents[2] / "data" / "audio_test"
DEVICE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
FRAME_BYTES = 1280
ASR_CHUNK_BYTES = 6400
ASR_QUEUE_MAXSIZE = 25
AUDIO_LOG_INTERVAL = 25
PCM_FLUSH_INTERVAL = 25
ASR_FEED_LOG_INTERVAL = 25
ASR_QUEUE_DROP_LOG_INTERVAL = 25
ASR_SEND_INTERVAL_SECONDS = 0.2


@dataclass
class _AsrSessionState:
    started: bool = False
    healthy: bool = False
    failed: bool = False
    queue_drop_count: int = 0


def _build_sentence_callback(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[TencentSentenceResult | None],
    accepting_results: threading.Event,
    device_id: str,
) -> Callable[[TencentSentenceResult], None]:
    """Create the receiver-thread to asyncio-loop handoff callback."""

    def enqueue(sentence: TencentSentenceResult) -> None:
        if accepting_results.is_set():
            queue.put_nowait(sentence)

    def on_sentence(sentence: TencentSentenceResult) -> None:
        if not accepting_results.is_set():
            logger.debug(
                "[TRANSCRIPT] callback ignored after shutdown device=%s",
                device_id,
            )
            return
        try:
            loop.call_soon_threadsafe(enqueue, sentence)
        except RuntimeError:
            logger.warning(
                "[TRANSCRIPT] event loop closed; sentence ignored device=%s",
                device_id,
            )

    return on_sentence


def _safe_device_id(device_id: str) -> str:
    safe_id = DEVICE_ID_PATTERN.sub("_", device_id).strip(".")
    return safe_id or "unknown-device"


def _resolve_active_meeting(device_id: str) -> str | None:
    db = SessionLocal()
    try:
        return get_active_meeting(db, device_id)
    finally:
        db.close()


async def _receive_until_stopped(
    websocket: WebSocket,
    stop_requested: asyncio.Event,
) -> dict | None:
    """Receive one frame, while allowing the Finish request to wake the loop."""
    receive_task = asyncio.create_task(websocket.receive())
    stop_task = asyncio.create_task(stop_requested.wait())
    done, pending = await asyncio.wait(
        {receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )

    # Preserve a frame that arrived at the same instant as the stop request.
    if receive_task in done:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        return await receive_task

    receive_task.cancel()
    await asyncio.gather(receive_task, return_exceptions=True)
    for task in pending:
        task.cancel()
    return None


def _enqueue_asr_frame(
    queue: asyncio.Queue[bytes | None],
    frame: bytes,
    device_id: str,
    state: _AsrSessionState,
) -> None:
    try:
        queue.put_nowait(frame)
        return
    except asyncio.QueueFull:
        queue.get_nowait()
        queue.task_done()

    state.queue_drop_count += 1
    if state.queue_drop_count % ASR_QUEUE_DROP_LOG_INTERVAL == 0:
        print(
            f"[ASR] queue overflow device={device_id} "
            f"dropped={state.queue_drop_count}"
        )
    queue.put_nowait(frame)


async def _asr_sender_worker(
    queue: asyncio.Queue[bytes | None],
    asr_client: TencentRealtimeAsrClient,
    device_id: str,
    state: _AsrSessionState,
) -> None:
    aggregation_buffer = bytearray()
    feed_chunk_count = 0
    loop = asyncio.get_running_loop()
    next_send_time = loop.time()

    async def send_chunk(chunk: bytes) -> bool:
        nonlocal feed_chunk_count, next_send_time
        now = loop.time()
        if now < next_send_time:
            await asyncio.sleep(next_send_time - now)
        send_started_at = loop.time()
        chunk = apply_pcm16_gain(chunk, settings.asr_audio_gain)
        try:
            await asyncio.to_thread(asr_client.feed_audio, chunk)
        except Exception as exc:
            state.healthy = False
            state.failed = True
            print(f"[ASR] feed failed device={device_id}: {exc}")
            print(f"[ASR] unavailable, recording continues device={device_id}")
            return False

        next_send_time = send_started_at + ASR_SEND_INTERVAL_SECONDS
        feed_chunk_count += 1
        if feed_chunk_count % ASR_FEED_LOG_INTERVAL == 0:
            print(f"[ASR] feed chunks={feed_chunk_count} device={device_id}")
        return True

    try:
        try:
            await asyncio.to_thread(asr_client.start_stream)
        except Exception as exc:
            state.failed = True
            print(
                f"[ASR] unavailable, recording continues "
                f"device={device_id}: {exc}"
            )
            return

        state.started = True
        state.healthy = True
        next_send_time = loop.time()
        print(f"[ASR] session started device={device_id}")

        while True:
            frame = await queue.get()
            try:
                if frame is None:
                    # Do not throw away the final <200 ms of speech. Tencent is
                    # fed fixed 200 ms chunks, so pad only the ASR copy with
                    # silence; the raw PCM recording remains byte-exact.
                    if aggregation_buffer:
                        tail = bytes(aggregation_buffer).ljust(ASR_CHUNK_BYTES, b"\x00")
                        await send_chunk(tail)
                    return

                aggregation_buffer.extend(frame)
                while len(aggregation_buffer) >= ASR_CHUNK_BYTES:
                    asr_chunk = bytes(aggregation_buffer[:ASR_CHUNK_BYTES])
                    del aggregation_buffer[:ASR_CHUNK_BYTES]
                    if not await send_chunk(asr_chunk):
                        return
            finally:
                queue.task_done()
    finally:
        state.healthy = False
        if state.started:
            try:
                await asyncio.to_thread(asr_client.end_stream)
            except Exception as exc:
                print(f"[ASR] session close failed device={device_id}: {exc}")
            print(f"[ASR] session ended device={device_id}")


async def _shutdown_asr_worker(
    queue: asyncio.Queue[bytes | None],
    worker_task: asyncio.Task[None],
    device_id: str,
) -> None:
    if worker_task.done():
        await asyncio.gather(worker_task, return_exceptions=True)
        return

    # Tencent's own end_stream() has a bounded receive timeout. Do not place a
    # shorter timeout around it here: cancelling its to_thread await would not
    # stop the receiver thread and could mark the pipeline drained before the
    # final sentence callback arrives. The product Finish request owns the
    # overall timeout and leaves the meeting IN_PROGRESS if this takes too long.
    await queue.put(None)
    await worker_task


async def _shutdown_transcript_worker(
    queue: asyncio.Queue[TencentSentenceResult | None],
    worker_task: asyncio.Task[None],
    accepting_results: threading.Event,
    device_id: str,
) -> None:
    if worker_task.done():
        accepting_results.clear()
        await asyncio.gather(worker_task, return_exceptions=True)
        return

    try:
        # end_stream() joins the Tencent receiver before this function runs.
        # Yield once so its final call_soon_threadsafe callbacks reach the queue
        # before the sentinel is appended.
        await asyncio.sleep(0)
        await queue.put(None)
        await worker_task
    finally:
        accepting_results.clear()


@router.websocket("/ws/audio/{device_id}")
async def receive_audio(websocket: WebSocket, device_id: str) -> None:
    await websocket.accept()
    meeting_id = _resolve_active_meeting(device_id)
    if meeting_id is None:
        await websocket.close(code=1008, reason="no active meeting")
        return

    session = AudioSession(
        device_id=device_id,
        meeting_id=meeting_id,
        websocket=websocket,
    )
    if not audio_session_manager.register(session):
        await websocket.close(code=1013, reason="audio session unavailable")
        return

    safe_device_id = _safe_device_id(device_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_path = AUDIO_DIR / f"{safe_device_id}_{timestamp}.pcm"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    frame_number = 0
    asr_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
        maxsize=ASR_QUEUE_MAXSIZE
    )
    transcript_queue: asyncio.Queue[TencentSentenceResult | None] = (
        asyncio.Queue()
    )
    accepting_transcript_results = threading.Event()
    accepting_transcript_results.set()
    loop = asyncio.get_running_loop()
    on_sentence = _build_sentence_callback(
        loop,
        transcript_queue,
        accepting_transcript_results,
        device_id,
    )
    transcript_worker_task = asyncio.create_task(
        transcript_dispatch_worker(transcript_queue, device_id)
    )
    asr_client: TencentRealtimeAsrClient | None = None
    asr_worker_task: asyncio.Task[None] | None = None
    asr_state = _AsrSessionState()

    print(f"[AUDIO] device={device_id} connected")
    try:
        asr_client = TencentRealtimeAsrClient(
            device_id=device_id,
            on_sentence=on_sentence,
        )
        asr_worker_task = asyncio.create_task(
            _asr_sender_worker(asr_queue, asr_client, device_id, asr_state)
        )
    except Exception as exc:
        asr_state.failed = True
        print(
            f"[ASR] unavailable, recording continues "
            f"device={device_id}: {exc}"
        )

    try:
        with output_path.open("ab") as output_file:
            while True:
                message = await _receive_until_stopped(
                    websocket, session.stop_requested
                )
                if message is None:
                    break
                if message.get("type") == "websocket.disconnect":
                    break
                frame = message.get("bytes")
                if frame is None:
                    print(f"[AUDIO] device={device_id} rejected non-binary message")
                    await websocket.close(
                        code=1003, reason="binary audio frames required"
                    )
                    return
                if len(frame) != FRAME_BYTES:
                    print(
                        f"[AUDIO] invalid frame device={device_id} "
                        f"bytes={len(frame)} expected={FRAME_BYTES}"
                    )
                    continue

                output_file.write(frame)
                frame_number += 1
                total_bytes += len(frame)
                if frame_number % PCM_FLUSH_INTERVAL == 0:
                    output_file.flush()
                if frame_number % AUDIO_LOG_INTERVAL == 0:
                    print(
                        f"[AUDIO] received frames={frame_number} "
                        f"bytes={total_bytes} device={device_id}"
                    )

                if (
                    asr_worker_task is not None
                    and not asr_worker_task.done()
                    and not asr_state.failed
                ):
                    _enqueue_asr_frame(
                        asr_queue, frame, device_id, asr_state
                    )
    except WebSocketDisconnect:
        pass
    finally:
        if asr_worker_task is not None:
            await _shutdown_asr_worker(
                asr_queue, asr_worker_task, device_id
            )
        await _shutdown_transcript_worker(
            transcript_queue,
            transcript_worker_task,
            accepting_transcript_results,
            device_id,
        )
        try:
            await websocket.close(code=1000, reason="audio session ended")
        except Exception:
            # The peer may already have completed the close handshake.
            pass
        audio_session_manager.unregister(session)
        print(
            f"[AUDIO] device={device_id} disconnected "
            f"total_bytes={total_bytes} file={output_path}"
        )
