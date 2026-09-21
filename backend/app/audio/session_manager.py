"""Process-local control for the current ESP32 audio WebSocket session.

The MVP runs one uvicorn process and one physical room device.  Meeting HTTP
requests and the audio WebSocket therefore share this small registry so the
Finish button can stop the socket and wait until its PCM/ASR/transcript queues
have drained before the transcript is frozen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import RLock
from typing import Any


class AudioSessionStopTimeout(RuntimeError):
    """The audio pipeline did not finish draining within the allowed time."""


@dataclass(eq=False)
class AudioSession:
    device_id: str
    meeting_id: str
    websocket: Any
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)


class AudioSessionManager:
    """Coordinate audio WebSockets with Start/Finish meeting actions."""

    def __init__(self) -> None:
        self._sessions: dict[str, AudioSession] = {}
        self._stopping: set[str] = set()
        self._lock = RLock()

    def register(self, session: AudioSession) -> bool:
        """Register one device session unless Finish is closing that device."""
        with self._lock:
            if session.device_id in self._stopping:
                return False
            current = self._sessions.get(session.device_id)
            if current is not None and not current.drained.is_set():
                return False
            self._sessions[session.device_id] = session
            return True

    def unregister(self, session: AudioSession) -> None:
        with self._lock:
            if self._sessions.get(session.device_id) is session:
                del self._sessions[session.device_id]
        session.drained.set()

    def get(self, device_id: str) -> AudioSession | None:
        with self._lock:
            return self._sessions.get(device_id)

    def begin_stop(self, device_id: str, meeting_id: str) -> AudioSession | None:
        """Block reconnects and ask the matching socket to drain and close."""
        with self._lock:
            self._stopping.add(device_id)
            session = self._sessions.get(device_id)
            if session is None or session.meeting_id != meeting_id:
                return None
            session.stop_requested.set()
            return session

    async def wait_until_drained(
        self,
        session: AudioSession | None,
        *,
        timeout_seconds: float,
    ) -> None:
        if session is None:
            return
        try:
            await asyncio.wait_for(session.drained.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise AudioSessionStopTimeout(
                f"audio session for device {session.device_id} did not drain "
                f"within {timeout_seconds:g}s"
            ) from exc

    def end_stop(self, device_id: str) -> None:
        with self._lock:
            self._stopping.discard(device_id)

    def clear(self) -> None:
        """Test/app-shutdown cleanup; wakes any waiter before dropping state."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._stopping.clear()
        for session in sessions:
            session.stop_requested.set()
            session.drained.set()


audio_session_manager = AudioSessionManager()


__all__ = [
    "AudioSession",
    "AudioSessionManager",
    "AudioSessionStopTimeout",
    "audio_session_manager",
]
