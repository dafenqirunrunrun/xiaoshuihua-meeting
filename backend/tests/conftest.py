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
    from app.meeting.meeting_model import Meeting  # noqa: F401
    from app.meeting_state.model import MeetingState  # noqa: F401
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
    from app.meeting_state.scheduler import meeting_state_scheduler

    assert meeting_state_scheduler.wait_for_idle(timeout_seconds=10.0)
    device_meeting_bindings.clear()
    audio_session_manager.clear()
    yield

    assert meeting_state_scheduler.wait_for_idle(timeout_seconds=10.0)
    device_meeting_bindings.clear()
    audio_session_manager.clear()
    live_utterance_cache.clear_all()
    event_broker.disconnect_all()
