"""Per-meeting serialization for long-running Meeting State agent work.

This lock is deliberately separate from ``meeting_lock``. Transcript writes
must remain free to commit while an LLM request is in flight; only two agent
runs for the same meeting need to exclude each other.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock, RLock
from typing import Iterator

_registry_guard = Lock()
_locks: dict[str, RLock] = {}


def _lock_for(meeting_id: str) -> RLock:
    with _registry_guard:
        lock = _locks.get(meeting_id)
        if lock is None:
            lock = RLock()
            _locks[meeting_id] = lock
        return lock


@contextmanager
def meeting_agent_run_lock(meeting_id: str) -> Iterator[None]:
    """Allow only one Meeting State run per meeting at a time."""
    lock = _lock_for(meeting_id)
    with lock:
        yield


__all__ = ["meeting_agent_run_lock"]
