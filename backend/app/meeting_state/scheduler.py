"""Coalescing process-local scheduler for real-time Meeting State updates."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Condition, RLock
import time

from app.config import settings

logger = logging.getLogger(__name__)


class MeetingStateScheduler:
    """Run at most one worker per meeting and merge repeated FINAL signals.

    Signals are collected until a short quiet window, capped by a maximum wait.
    The resulting run force-flushes the pending tail, so a short meeting becomes
    visible while recording instead of waiting for the normal batch threshold or
    Finish. Signals received while the model is running form one follow-up window.
    """

    def __init__(
        self,
        *,
        runner: Callable[[str], None] | None = None,
        max_workers: int = 2,
        debounce_seconds: float = 0.0,
        max_wait_seconds: float = 0.0,
    ) -> None:
        self._runner = runner
        self._debounce_seconds = max(0.0, debounce_seconds)
        self._max_wait_seconds = max(0.0, max_wait_seconds)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="meeting-state",
        )
        self._running: set[str] = set()
        self._pending: set[str] = set()
        self._window_started_at: dict[str, float] = {}
        self._last_signal_at: dict[str, float] = {}
        self._lock = RLock()
        self._idle = Condition(self._lock)

    def schedule(self, meeting_id: str) -> bool:
        """Schedule or coalesce one meeting. Returns true for a new worker."""
        with self._lock:
            now = time.monotonic()
            if meeting_id not in self._pending:
                self._window_started_at[meeting_id] = now
            self._pending.add(meeting_id)
            self._last_signal_at[meeting_id] = now
            self._idle.notify_all()
            if meeting_id in self._running:
                return False
            self._running.add(meeting_id)
            self._executor.submit(self._drain, meeting_id)
            return True

    def _run_once(self, meeting_id: str) -> None:
        if self._runner is not None:
            self._runner(meeting_id)
            return
        from app.meeting_state.service import run_agent_background

        # The debounce window is the batching boundary for live updates. Force
        # the below-threshold tail so state is refreshed during the recording.
        run_agent_background(meeting_id, force_flush=True)

    def _wait_for_window(self, meeting_id: str) -> bool:
        """Wait until quiet/max-wait; false means there is no pending signal."""
        with self._idle:
            while meeting_id in self._pending:
                now = time.monotonic()
                quiet_deadline = (
                    self._last_signal_at[meeting_id] + self._debounce_seconds
                )
                max_deadline = (
                    self._window_started_at[meeting_id] + self._max_wait_seconds
                    if self._max_wait_seconds > 0
                    else quiet_deadline
                )
                remaining = min(quiet_deadline, max_deadline) - now
                if remaining <= 0:
                    self._pending.discard(meeting_id)
                    self._window_started_at.pop(meeting_id, None)
                    self._last_signal_at.pop(meeting_id, None)
                    return True
                self._idle.wait(timeout=remaining)
            return False

    def _drain(self, meeting_id: str) -> None:
        while True:
            if not self._wait_for_window(meeting_id):
                with self._lock:
                    self._running.discard(meeting_id)
                    self._idle.notify_all()
                return
            try:
                self._run_once(meeting_id)
            except Exception:  # pragma: no cover - runner is already defensive
                logger.exception(
                    "meeting state scheduled run failed for meeting %s",
                    meeting_id,
                )

            with self._lock:
                if meeting_id in self._pending:
                    continue
                self._running.discard(meeting_id)
                self._idle.notify_all()
                return

    def wait_for_idle(
        self,
        meeting_id: str | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> bool:
        """Wait for tests/shutdown; production request paths never call this."""
        deadline = time.monotonic() + timeout_seconds
        with self._idle:
            while self._is_busy(meeting_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(timeout=remaining)
        return True

    def _is_busy(self, meeting_id: str | None) -> bool:
        if meeting_id is None:
            return bool(self._running)
        return meeting_id in self._running


meeting_state_scheduler = MeetingStateScheduler(
    debounce_seconds=settings.meeting_agent_realtime_debounce_seconds,
    max_wait_seconds=settings.meeting_agent_realtime_max_wait_seconds,
)


__all__ = ["MeetingStateScheduler", "meeting_state_scheduler"]
