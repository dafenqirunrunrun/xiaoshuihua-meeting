"""live_utterance_cache: process-local PARTIAL store.

PARTIAL events never touch the DB. They are UI-only transient state,
keyed by meeting_id + utterance_id. PRD §22, ADR D4, architecture §5.1.

Lifecycle / limits (documented in reporting, see also architecture §5.1):
  - In-memory only: entries are lost on process restart; that's acceptable
    because PARTIAL is never source-of-truth. On Frontend recovery the page
    re-syncs via Snapshot (FINAL) + SSE; PARTIAL only exists while the SSE
    connection is alive and emitting.
  - Process-local: NOT shared across uvicorn workers / instances.
    Multi-instance / Redis upgrade noted in ADR D3/D8.
"""

from dataclasses import dataclass, field
from threading import RLock
from typing import NotRequired, TypedDict, Unpack


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

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Singleton shared across the app (single-process MVP).
live_utterance_cache = _PartialCache()
