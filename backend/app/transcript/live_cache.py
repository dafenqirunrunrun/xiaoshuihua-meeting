"""live_utterance_cache: process-local, Backend-Process-scoped PARTIAL store.

PARTIAL events never touch the DB. keyed by meeting_id + utterance_id.
PRD §22, ADR D4, architecture §5.1.

Lifecycle semantics: **Backend Process Scoped Temporary State**.
  - Fully decoupled from Frontend / Browser Session / SSE Connection lifecycle:
    the Backend keeps receiving PARTIAL and updating this cache even when there
    is NO SSE client connected. Downstream connections never gate ingesting.
  - Entries are lost on process restart; acceptable because PARTIAL is never
    Source-of-Truth (ADR D3/D4). Future: Redis for multi-instance.
  - FINAL ingestion deletes the matching PARTIAL entry (PRD §23).
"""

from dataclasses import dataclass, field
from threading import RLock
from typing import TypedDict

from typing_extensions import Unpack


class PartialValue(TypedDict):
    text: str
    speaker_id: str
    start_ms: int | None
    end_ms: int | None
    emitted_at: int | None


class _PartialCache:
    def __init__(self) -> None:
        self._store: dict[str, PartialValue] = {}
        self._lock = RLock()

    @staticmethod
    def _key(meeting_id: str, utterance_id: str) -> str:
        return f"{meeting_id}:{utterance_id}"

    def upsert(self, meeting_id: str, utterance_id: str, **value: Unpack[PartialValue]) -> None:
        with self._lock:
            self._store[self._key(meeting_id, utterance_id)] = PartialValue(**value)

    def get(self, meeting_id: str, utterance_id: str) -> PartialValue | None:
        with self._lock:
            return self._store.get(self._key(meeting_id, utterance_id))

    def delete(self, meeting_id: str, utterance_id: str) -> None:
        with self._lock:
            self._store.pop(self._key(meeting_id, utterance_id), None)

    def clear_meeting(self, meeting_id: str) -> None:
        """Drop every PARTIAL entry for one meeting (Step 6 freeze cleanup).

        Called ONLY after the durable freeze boundary (accepted transcript_stream
        row) has committed successfully. If freeze fails (integrity 409) this is
        NOT called, so residual PARTIAL survives for the retry path.
        """
        prefix = f"{meeting_id}:"
        with self._lock:
            for key in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(key, None)

    def clear_all(self) -> None:
        """Drop every PARTIAL entry, for every meeting.

        Process-wide reset: used by the test harness to isolate cases that share
        this singleton, and available as a whole-cache flush. NOT part of any
        business transition — lifecycle cleanup uses `clear_meeting`.
        """
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Singleton shared across the app (single-process MVP).
live_utterance_cache = _PartialCache()
