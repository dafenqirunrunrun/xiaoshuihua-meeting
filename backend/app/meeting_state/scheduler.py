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

    Signals are collected until the segment, silence or interval policy is met.
    The resulting run consumes one captured high-watermark in one bounded call.
    Signals received while the model is running form one follow-up window.
    """

    def __init__(
        self,
        *,
        runner: Callable[[str], None] | None = None,
        max_workers: int = 2,
        debounce_seconds: float | None = None,
        max_wait_seconds: float = 0.0,
        min_signals: int | None = None,
        target_interval_seconds: float = 0.0,
        silence_seconds: float | None = None,
        burst_signals: int | None = None,
        burst_window_seconds: float = 0.0,
    ) -> None:
        self._runner = runner
        # `debounce_seconds` remains as a compatibility alias for focused unit
        # tests and internal callers. Product configuration uses silence_seconds.
        quiet = silence_seconds if silence_seconds is not None else debounce_seconds
        self._silence_seconds = max(0.0, quiet or 0.0)
        self._min_signals = max(
            1,
            min_signals
            if min_signals is not None
            else (2**31 - 1 if debounce_seconds is not None else 1),
        )
        self._target_interval_seconds = max(0.0, target_interval_seconds)
        self._max_wait_seconds = max(0.0, max_wait_seconds)
        self._burst_signals = max(1, burst_signals or 2**31 - 1)
        self._burst_window_seconds = max(0.0, burst_window_seconds)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="meeting-state",
        )
        self._running: set[str] = set()
        self._pending: set[str] = set()
        self._window_started_at: dict[str, float] = {}
        self._last_signal_at: dict[str, float] = {}
        self._signal_count: dict[str, int] = {}
        self._recent_signal_times: dict[str, list[float]] = {}
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
            self._signal_count[meeting_id] = self._signal_count.get(meeting_id, 0) + 1
            recent = self._recent_signal_times.setdefault(meeting_id, [])
            recent.append(now)
            if self._burst_window_seconds > 0:
                cutoff = now - self._burst_window_seconds
                first_live_index = next(
                    (i for i, value in enumerate(recent) if value >= cutoff),
                    len(recent),
                )
                del recent[:first_live_index]
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

        # The scheduler has already decided this high-watermark is ready. The
        # service folds the captured pending range in one bounded LLM call.
        run_agent_background(meeting_id, force_flush=True)

    def _wait_for_window(self, meeting_id: str) -> bool:
        """Wait until quiet/max-wait; false means there is no pending signal."""
        with self._idle:
            while meeting_id in self._pending:
                now = time.monotonic()
                if self._signal_count.get(meeting_id, 0) >= self._min_signals:
                    remaining = 0.0
                elif (
                    len(self._recent_signal_times.get(meeting_id, []))
                    >= self._burst_signals
                ):
                    remaining = 0.0
                else:
                    quiet_deadline = (
                        self._last_signal_at[meeting_id] + self._silence_seconds
                    )
                    deadlines = [quiet_deadline]
                    if self._target_interval_seconds > 0:
                        deadlines.append(
                            self._window_started_at[meeting_id]
                            + self._target_interval_seconds
                        )
                    if self._max_wait_seconds > 0:
                        deadlines.append(
                            self._window_started_at[meeting_id] + self._max_wait_seconds
                        )
                    remaining = min(deadlines) - now
                if remaining <= 0:
                    self._pending.discard(meeting_id)
                    self._window_started_at.pop(meeting_id, None)
                    self._last_signal_at.pop(meeting_id, None)
                    self._signal_count.pop(meeting_id, None)
                    self._recent_signal_times.pop(meeting_id, None)
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

    def cancel_pending(self, meeting_id: str) -> None:
        """Drop a not-yet-started realtime pass when a meeting is ending.

        An already-running provider call is not forcibly interrupted; it may
        persist its last-good snapshot, but no follow-up realtime run is kept.
        """
        with self._idle:
            self._pending.discard(meeting_id)
            self._window_started_at.pop(meeting_id, None)
            self._last_signal_at.pop(meeting_id, None)
            self._signal_count.pop(meeting_id, None)
            self._recent_signal_times.pop(meeting_id, None)
            self._idle.notify_all()
        from app.meeting_state.service import reset_summary_refresh_gate

        reset_summary_refresh_gate(meeting_id)

    def cancel_all_pending(self) -> None:
        """Cancel every waiting window during process shutdown/test cleanup."""
        with self._idle:
            self._pending.clear()
            self._window_started_at.clear()
            self._last_signal_at.clear()
            self._signal_count.clear()
            self._recent_signal_times.clear()
            self._idle.notify_all()
        from app.meeting_state.service import reset_summary_refresh_gate

        reset_summary_refresh_gate()

    def _is_busy(self, meeting_id: str | None) -> bool:
        if meeting_id is None:
            return bool(self._running)
        return meeting_id in self._running


meeting_state_scheduler = MeetingStateScheduler(
    min_signals=settings.meeting_agent_realtime_min_segments,
    target_interval_seconds=settings.meeting_agent_realtime_target_interval_seconds,
    silence_seconds=settings.meeting_agent_realtime_silence_seconds,
    max_wait_seconds=settings.meeting_agent_realtime_max_wait_seconds,
    burst_signals=settings.meeting_agent_realtime_burst_segments,
    burst_window_seconds=settings.meeting_agent_realtime_burst_window_seconds,
)


__all__ = ["MeetingStateScheduler", "meeting_state_scheduler"]
