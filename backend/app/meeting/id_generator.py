"""Numeric identifiers for meetings.

The durable meeting ID is a 64-bit Snowflake value represented as a string.
The user-facing meeting code is an independent nine-digit decimal alias.
"""

from __future__ import annotations

import secrets
import threading
import time


# 2026-01-01T00:00:00Z. A recent custom epoch keeps IDs shorter while retaining
# the standard Snowflake layout: 41-bit milliseconds, 10-bit worker, 12-bit seq.
_EPOCH_MS = 1_767_225_600_000
_WORKER_BITS = 10
_SEQUENCE_BITS = 12
_MAX_WORKER_ID = (1 << _WORKER_BITS) - 1
_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1
_WORKER_SHIFT = _SEQUENCE_BITS
_TIMESTAMP_SHIFT = _WORKER_BITS + _SEQUENCE_BITS


class SnowflakeGenerator:
    """Thread-safe 64-bit numeric ID generator.

    A logical clock absorbs wall-clock rollback. The database primary key and
    bounded create retry remain the final cross-process collision guard.
    """

    def __init__(self, worker_id: int) -> None:
        if not 0 <= worker_id <= _MAX_WORKER_ID:
            raise ValueError(f"worker_id must be between 0 and {_MAX_WORKER_ID}")
        self._worker_id = worker_id
        self._last_timestamp_ms = -1
        self._sequence = 0
        self._lock = threading.Lock()

    def next_id(self) -> str:
        with self._lock:
            timestamp_ms = max(self._now_ms(), self._last_timestamp_ms)
            if timestamp_ms == self._last_timestamp_ms:
                self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
                if self._sequence == 0:
                    # Advance the logical clock instead of blocking when the
                    # wall clock moved backwards or one millisecond is full.
                    timestamp_ms = self._last_timestamp_ms + 1
            else:
                self._sequence = 0

            self._last_timestamp_ms = timestamp_ms
            value = (
                ((timestamp_ms - _EPOCH_MS) << _TIMESTAMP_SHIFT)
                | (self._worker_id << _WORKER_SHIFT)
                | self._sequence
            )
            return str(value)

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000


def generate_meeting_code() -> str:
    """Return a cryptographically random nine-digit user-facing code."""

    return str(secrets.randbelow(900_000_000) + 100_000_000)
