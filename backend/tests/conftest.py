"""Shared pytest fixtures (ASR-0B test consolidation).

Isolation strategy — why env vars are set at import time:

``app.config.settings`` is a module-level singleton built at import. It reads
``DATABASE_URL`` / ``LLM_PROVIDER`` from the environment exactly once, and
``app.db`` builds the engine from ``settings`` at ITS import. So the overrides
below MUST run before any ``app.*`` import — hence they sit at the very top of
this conftest, and the app modules are imported lazily inside the fixtures.

Each test gets a FRESH schema. The engine itself is session-scoped (cheap), but
an autouse fixture drops and re-creates every table per test, so tests never
observe each other's meetings / segments / summaries.

LLM is pinned to ``fake`` so the whole suite is offline and deterministic.
The factory is fail-closed (unknown provider -> LLMConfigurationError), so
pinning explicitly here also proves we never silently fall back.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

# --- MUST precede any `app.*` import ---------------------------------------
_TMP_DIR = Path(tempfile.mkdtemp(prefix="xiaoshuihua-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["DEEPSEEK_API_KEY"] = ""
# The two-stage referee is opt-in and the offline suite was written against the
# single-call default (see `captain_two_stage_enabled` in app/config.py). A
# developer who turned it on in their own `.env` must not thereby change what the
# suite exercises — same reason LLM_PROVIDER is pinned above. Tests that want
# two-stage turn it on themselves via `monkeypatch.setattr(settings, ...)`.
os.environ["CAPTAIN_TWO_STAGE_ENABLED"] = "false"
# Small queue so the eviction path is reachable without huge payloads.
os.environ["SSE_QUEUE_MAXSIZE"] = "100"
# Keep the heartbeat out of the way of short-lived assertions.
os.environ["SSE_HEARTBEAT_INTERVAL"] = "60"
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def app_module():
    """The FastAPI app, imported only after the env overrides above are set."""
    from app.main import app

    return app


@pytest.fixture(autouse=True)
def fresh_schema(app_module) -> Iterator[None]:
    """Drop + recreate all tables around every test (per-test isolation)."""
    from app.db import Base, engine

    # Import the models so they are registered on Base.metadata before create.
    from app.agent.summary_model import MeetingSummary  # noqa: F401
    from app.captain.model import CaptainReport  # noqa: F401
    from app.meeting.meeting_model import Meeting  # noqa: F401
    from app.meeting_state.model import MeetingState  # noqa: F401
    from app.meeting_state.snapshot_model import (  # noqa: F401
        MeetingMemorySnapshot,
        MeetingStateHealth,
    )
    from app.transcript.segment_model import TranscriptSegment  # noqa: F401
    from app.transcript.stream_model import TranscriptStream  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(app_module) -> Iterator[TestClient]:
    """TestClient with lifespan run (tables exist, brokers closed on exit).

    BackgroundTasks run synchronously inside the TestClient request, so the
    Finalizer / regenerate pipeline completes before the call returns — that is
    what lets the E2E tests assert on a COMPLETED summary without polling.
    """
    with TestClient(app_module) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_inprocess_state() -> Iterator[None]:
    """Clear process-local caches / broker entries between tests.

    The live PARTIAL cache and the SSE broker are process-wide singletons, so
    without this a previous test's utterances or subscribers would leak in.
    """
    from app.realtime.event_broker import event_broker
    from app.audio.session_manager import audio_session_manager
    from app.device.meeting_binding import device_meeting_bindings
    from app.transcript.live_cache import live_utterance_cache
    from app.captain.service import reset_throttle as reset_captain_throttle
    from app.captain.worker import captain_worker
    from app.captain.search.cache import reset_cache as reset_search_cache
    from app.meeting_state.scheduler import meeting_state_scheduler

    # Drain the referee queue BEFORE dropping the schema, so a background
    # analysis cannot write into a table the next test just recreated.
    assert captain_worker.drain(timeout=10.0)
    reset_captain_throttle()
    # The search cache is a process-wide singleton with TTLs measured in HOURS
    # (TECH_FACT) and DAYS (STATIC_FACT), so it survives the whole test session.
    # Without this reset, one test's scripted search response is served to the
    # next test that happens to plan the same query — which makes the later
    # test's assertions depend on an earlier test's fixtures rather than on the
    # code under test.
    reset_search_cache()

    meeting_state_scheduler.cancel_all_pending()
    assert meeting_state_scheduler.wait_for_idle(timeout_seconds=10.0)
    # Production uses 8 segments / 25s / 10s silence. Unit tests exercise the
    # service thresholds directly, so make scheduler delivery immediate while
    # retaining the exact same one-worker/coalescing implementation.
    meeting_state_scheduler._min_signals = 2**31 - 1
    meeting_state_scheduler._silence_seconds = 0.0
    meeting_state_scheduler._target_interval_seconds = 0.0
    meeting_state_scheduler._max_wait_seconds = 0.0
    from app.meeting_state.service import run_agent_background

    meeting_state_scheduler._runner = lambda meeting_id: run_agent_background(
        meeting_id, force_flush=False
    )
    device_meeting_bindings.clear()
    audio_session_manager.clear()
    yield

    assert captain_worker.drain(timeout=10.0)
    reset_captain_throttle()
    meeting_state_scheduler.cancel_all_pending()
    assert meeting_state_scheduler.wait_for_idle(timeout_seconds=10.0)
    device_meeting_bindings.clear()
    audio_session_manager.clear()
    live_utterance_cache.clear_all()
    event_broker.disconnect_all()
