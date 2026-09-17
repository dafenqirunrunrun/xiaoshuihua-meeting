"""Shared per-meeting synchronization boundary.

FINAL ingest and STREAM_END integrity check must share THE SAME lock for a
meeting (Step 4 §9), otherwise a concurrent FINAL write could race with the
integrity check (or a FINAL could be appended after stream-end succeeded).

Current: single-process MVP -> an in-process per-meeting RLock registry is correct
and deterministic. This is NOT a cross-process/cross-instance lock.

Future (ADR D8): multi-worker / multi-instance / PostgreSQL must move this to
database-level concurrency control (SELECT ... FOR UPDATE / conditional UPDATE /
optimistic versioning). Do NOT treat this registry as production-grade for
multi-process deployments.
"""

from threading import RLock
from typing import Any

_locks: dict[str, RLock] = {}
_registry_lock = RLock()


def meeting_lock(meeting_id: str) -> RLock:
    """Return the shared RLock for a meeting (created on demand)."""
    with _registry_lock:
        lock = _locks.get(meeting_id)
        if lock is None:
            lock = _locks[meeting_id] = RLock()
        return lock
