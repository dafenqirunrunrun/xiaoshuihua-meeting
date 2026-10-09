"""Dispatch worker for the referee (ADR-002 §7).

THIS IS NOT A SCHEDULER. The distinction is the whole point of this module.

`MeetingStateScheduler` is a COALESCING scheduler: it exists because the
meeting-state agent consumes a high-frequency, SYSTEM-driven signal (every FINAL)
and must merge many signals into few LLM calls. It therefore owns window
thresholds, silence detection, burst detection and a pending high-water mark.

The referee's signal is the opposite: RARE and USER-driven (a wake word or a
button press, a handful of times per meeting). There is nothing to coalesce —
merging two user triggers 30 seconds apart would answer the wrong question. So
none of that machinery is copied here. What remains is the minimum needed to get
work off the caller's thread:

    queue      bounded; a full queue refuses the trigger loudly (503)
    dedupe     already enforced at INSERT by UNIQUE(meeting_id, anchor)
    throttle   enforced in service.trigger (per-minute rate)
    worker     ONE daemon thread, daemon=True, so shutdown never hangs

Why a thread and not FastAPI BackgroundTasks
--------------------------------------------
BackgroundTasks only exists on the HTTP request path. The wake-word trigger
fires from the REAL ASR path too (`transcript_dispatcher`), which has no
FastAPI request and therefore no BackgroundTasks to hand. Wiring the two paths
separately is what let the meeting-state agent grow two divergent hooks. One
queue at the single choke point both paths already share (the committed FINAL)
keeps them structurally identical instead.

Why the LLM must not run on the ingest thread
---------------------------------------------
The trigger decision is made next to FINAL commit. Running a 60-second provider
call there would hold `meeting_lock` and stall transcript persistence — the one
hard prohibition in ADR-002 §10. The queue is what makes that structurally
impossible rather than merely discouraged.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from app.config import settings

logger = logging.getLogger(__name__)

# Sentinel that stops the worker loop on shutdown.
_STOP = object()


class CaptainWorker:
    """Single-threaded FIFO dispatcher for referee analyses."""

    def __init__(self, *, maxsize: int | None = None) -> None:
        self._queue: queue.Queue = queue.Queue(
            maxsize=maxsize if maxsize is not None else settings.captain_queue_maxsize
        )
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start the daemon thread (idempotent, restartable after stop)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="captain-worker",
                daemon=True,
            )
            self._thread.start()
        # Wait outside the lock: the worker takes it nowhere, but a stop() racing
        # us must not be blocked behind this wait.
        self._started.wait(timeout=2.0)

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the worker to finish and wait briefly for it.

        Guarded on the thread actually being ALIVE. Enqueuing the sentinel for a
        dead worker would leave an item that nothing will ever consume, so
        `unfinished_tasks` would stay non-zero forever and every later `drain()`
        would block for its full timeout — a stop/start cycle (which the app
        lifespan performs) must leave the queue clean.

        A provider call already in flight is NOT interrupted — it may still
        commit its report. Nothing here can block shutdown for long, because the
        thread is a daemon: even a stuck provider call cannot keep the process
        alive.
        """
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread = None
                return
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                # Worker is alive but wedged; joining below is still bounded and
                # the daemon flag guarantees it cannot hold the process open.
                pass
        thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    # --- dispatch ----------------------------------------------------------
    def submit(self, meeting_id: str, report_id: str) -> bool:
        """Queue one analysis. False when the queue is full (caller reports 503).

        Refusing loudly is deliberate: silently dropping the request would leave
        a PENDING row that never resolves, and the user would watch a spinner
        forever with no explanation.
        """
        self.start()
        try:
            self._queue.put_nowait((meeting_id, report_id))
            return True
        except queue.Full:
            logger.warning(
                "captain queue full, refusing trigger report_id=%s", report_id
            )
            return False

    # --- worker loop -------------------------------------------------------
    def _run(self) -> None:
        self._started.set()
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                meeting_id, report_id = item
                # Imported here so the module stays import-light and the
                # service's own imports cannot create a cycle at import time.
                from app.captain.service import run_analysis

                try:
                    run_analysis(meeting_id, report_id)
                except Exception:  # pragma: no cover - run_analysis is defensive
                    # One bad report must never kill the worker: the next
                    # trigger still has to work.
                    logger.exception(
                        "captain worker run failed report_id=%s", report_id
                    )
            finally:
                self._queue.task_done()

    # --- test/introspection helpers ----------------------------------------
    def pending(self) -> int:
        return self._queue.qsize()

    def drain(self, timeout: float = 10.0) -> bool:
        """Block until the queue is empty. Tests and shutdown only.

        Production request paths never call this — a caller waiting on the
        referee would defeat the entire point of dispatching it.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0


# Process-wide singleton (single-process MVP, same stance as event_broker).
captain_worker = CaptainWorker()

__all__ = ["CaptainWorker", "captain_worker"]
