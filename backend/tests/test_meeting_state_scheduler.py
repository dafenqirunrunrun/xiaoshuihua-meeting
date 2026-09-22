from __future__ import annotations

from threading import Event, Lock
import time

from app.meeting_state.scheduler import MeetingStateScheduler


def test_repeated_final_signals_coalesce_to_one_follow_up_run() -> None:
    entered = Event()
    release = Event()
    calls: list[str] = []
    calls_lock = Lock()

    def runner(meeting_id: str) -> None:
        with calls_lock:
            calls.append(meeting_id)
            first = len(calls) == 1
        if first:
            entered.set()
            assert release.wait(timeout=2.0)

    scheduler = MeetingStateScheduler(runner=runner, max_workers=1)
    assert scheduler.schedule("meeting-1") is True
    assert entered.wait(timeout=2.0)

    for _ in range(20):
        assert scheduler.schedule("meeting-1") is False

    release.set()
    assert scheduler.wait_for_idle("meeting-1", timeout_seconds=2.0)
    assert calls == ["meeting-1", "meeting-1"]


def test_different_meetings_can_be_scheduled_independently() -> None:
    calls: list[str] = []
    calls_lock = Lock()

    def runner(meeting_id: str) -> None:
        with calls_lock:
            calls.append(meeting_id)

    scheduler = MeetingStateScheduler(runner=runner, max_workers=2)
    assert scheduler.schedule("meeting-a") is True
    assert scheduler.schedule("meeting-b") is True
    assert scheduler.wait_for_idle(timeout_seconds=2.0)
    assert sorted(calls) == ["meeting-a", "meeting-b"]


def test_quiet_window_coalesces_signals_before_first_run() -> None:
    calls: list[float] = []
    scheduler = MeetingStateScheduler(
        runner=lambda _meeting_id: calls.append(time.monotonic()),
        max_workers=1,
        debounce_seconds=0.08,
        max_wait_seconds=0.5,
    )

    assert scheduler.schedule("meeting-1") is True
    time.sleep(0.04)
    assert scheduler.schedule("meeting-1") is False
    time.sleep(0.04)
    assert calls == []

    assert scheduler.wait_for_idle("meeting-1", timeout_seconds=1.0)
    assert len(calls) == 1


def test_continuous_signals_flush_at_max_wait() -> None:
    called = Event()
    started_at = time.monotonic()
    scheduler = MeetingStateScheduler(
        runner=lambda _meeting_id: called.set(),
        max_workers=1,
        debounce_seconds=0.3,
        max_wait_seconds=0.15,
    )

    assert scheduler.schedule("meeting-1") is True
    time.sleep(0.05)
    assert scheduler.schedule("meeting-1") is False

    assert called.wait(timeout=0.25)
    assert time.monotonic() - started_at < 0.3
    assert scheduler.wait_for_idle("meeting-1", timeout_seconds=1.0)


def test_burst_signals_flush_immediately_before_quiet_window() -> None:
    called = Event()
    started_at = time.monotonic()
    scheduler = MeetingStateScheduler(
        runner=lambda _meeting_id: called.set(),
        max_workers=1,
        min_signals=20,
        silence_seconds=0.5,
        max_wait_seconds=5.0,
        burst_signals=4,
        burst_window_seconds=1.0,
    )

    assert scheduler.schedule("meeting-1") is True
    for _ in range(3):
        time.sleep(0.02)
        assert scheduler.schedule("meeting-1") is False

    assert called.wait(timeout=0.2)
    assert time.monotonic() - started_at < 0.5
    assert scheduler.wait_for_idle("meeting-1", timeout_seconds=1.0)


def test_default_runner_force_flushes_the_live_tail(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    def run(meeting_id: str, *, force_flush: bool = False) -> None:
        calls.append((meeting_id, force_flush))

    monkeypatch.setattr("app.meeting_state.service.run_agent_background", run)
    scheduler = MeetingStateScheduler(max_workers=1)
    assert scheduler.schedule("meeting-1") is True
    assert scheduler.wait_for_idle("meeting-1", timeout_seconds=1.0)
    assert calls == [("meeting-1", True)]
