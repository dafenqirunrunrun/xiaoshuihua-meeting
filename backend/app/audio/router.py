import asyncio
from datetime import datetime
from pathlib import Path
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.audio.pcm import apply_pcm16_gain
from app.config import settings
from app.speech.tencent_client import TencentRealtimeAsrClient

router = APIRouter(tags=["audio"])
AUDIO_DIR = Path(__file__).resolve().parents[2] / "data" / "audio_test"
DEVICE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
FRAME_BYTES = 1280
ASR_CHUNK_BYTES = 6400


def _safe_device_id(device_id: str) -> str:
    safe_id = DEVICE_ID_PATTERN.sub("_", device_id).strip(".")
    return safe_id or "unknown-device"


@router.websocket("/ws/audio/{device_id}")
async def receive_audio(websocket: WebSocket, device_id: str) -> None:
    await websocket.accept()
    safe_device_id = _safe_device_id(device_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_path = AUDIO_DIR / f"{safe_device_id}_{timestamp}.pcm"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    frame_number = 0
    asr_buffer = bytearray()
    asr_client: TencentRealtimeAsrClient | None = None
    asr_available = False

    try:
        asr_client = TencentRealtimeAsrClient(device_id=device_id)
        await asyncio.to_thread(asr_client.start_stream)
        asr_available = True
        print(f"[ASR] session started device={device_id}")
    except Exception as exc:
        print(f"[ASR] unavailable, recording continues: {exc}")
        asr_client = None

    print(f"[AUDIO] device={device_id} connected")
    try:
        with output_path.open("ab") as output_file:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                frame = message.get("bytes")
                if frame is None:
                    print(f"[AUDIO] device={device_id} rejected non-binary message")
                    await websocket.close(code=1003, reason="binary audio frames required")
                    return
                if len(frame) != FRAME_BYTES:
                    print(
                        f"[AUDIO] invalid frame device={device_id} "
                        f"bytes={len(frame)} expected={FRAME_BYTES}"
                    )

                output_file.write(frame)
                output_file.flush()
                frame_number += 1
                total_bytes += len(frame)
                if len(frame) != FRAME_BYTES:
                    continue
                asr_buffer.extend(frame)
                while len(asr_buffer) >= ASR_CHUNK_BYTES:
                    asr_chunk = bytes(asr_buffer[:ASR_CHUNK_BYTES])
                    del asr_buffer[:ASR_CHUNK_BYTES]
                    if asr_client is not None and asr_available:
                        try:
                            asr_chunk = apply_pcm16_gain(asr_chunk, settings.asr_audio_gain)
                            await asyncio.to_thread(asr_client.feed_audio, asr_chunk)
                        except Exception as exc:
                            print(f"[ASR] feed failed device={device_id}: {exc}")
                            asr_available = False
    except WebSocketDisconnect:
        pass
    finally:
        if asr_client is not None and asr_available:
            try:
                await asyncio.to_thread(asr_client.end_stream)
            except Exception as exc:
                print(f"[ASR] session close failed device={device_id}: {exc}")
        print(f"[ASR] session ended device={device_id}")
        print(f"[AUDIO] device={device_id} disconnected total_bytes={total_bytes} file={output_path}")