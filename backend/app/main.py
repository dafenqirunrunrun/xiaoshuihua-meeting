"""FastAPI app entry. PRD §7.

Run:  uvicorn app.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import create_all
from app.common.exceptions import (
    InvalidMeetingStateTransition,
    MeetingNotFoundError,
    TranscriptConflictError,
    TranscriptValidationError,
)
from app.agent import router as agent_router
from app.audio import router as audio_router
from app.captain import router as captain_router
from app.common import health_router
from app.demo import router as demo_router
from app.meeting import router as meeting_router
from app.meeting_state import router as meeting_state_router
from app.realtime import router as realtime_router
from app.transcript import internal_docs as transcript_internal_docs
from app.transcript import internal_router as transcript_internal_router
from app.transcript import router as transcript_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_all()  # ensure tables exist (PRD §46)
    yield
    # Wake audio receive loops before the process event loop is torn down.
    from app.audio.session_manager import audio_session_manager

    audio_session_manager.clear()
    from app.meeting_state.scheduler import meeting_state_scheduler

    meeting_state_scheduler.cancel_all_pending()
    meeting_state_scheduler.wait_for_idle(timeout_seconds=2.0)
    # 水华队长 dispatch worker (ADR-002). Stopped AFTER the meeting-state agent so
    # an in-flight transcript drain is not competing for the DB during shutdown.
    # Its thread is a daemon, so a stuck provider call cannot delay exit.
    from app.captain.worker import captain_worker

    captain_worker.stop(timeout=2.0)
    from app.agent.llm.factory import close_cached_providers

    close_cached_providers()
    # Close every SSE subscriber so generators end cleanly on shutdown.
    from app.realtime.event_broker import event_broker

    event_broker.disconnect_all()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)


# --- Domain exception -> HTTP mapping ------------------------------------
@app.exception_handler(MeetingNotFoundError)
async def _meeting_not_found_handler(_request: Request, exc: MeetingNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidMeetingStateTransition)
async def _invalid_transition_handler(_request: Request, exc: InvalidMeetingStateTransition) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(TranscriptValidationError)
async def _transcript_validation_handler(_request: Request, exc: TranscriptValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(TranscriptConflictError)
async def _transcript_conflict_handler(_request: Request, exc: TranscriptConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


app.include_router(health_router.router, prefix="/api")
app.include_router(meeting_router.router, prefix="/api")
app.include_router(transcript_router.router, prefix="/api")
app.include_router(realtime_router.router, prefix="/api")
app.include_router(agent_router.router, prefix="/api")
app.include_router(transcript_internal_router.router, prefix="/api")
app.include_router(audio_router.router)
# Meeting State agent debug surface (Step AGENT-1): a downstream consumer of the
# FINAL stream. Additive and independent — it shares no routes with the existing
# routers, and it stays functional (empty state) even when the agent is disabled.
app.include_router(meeting_state_router.router, prefix="/api")
# Public read-only Meeting State (Step UI-INTEL-1): the product surface the
# Intelligence Panel reads. Additive route, distinct path from every existing
# one; the internal debug router above is untouched.
app.include_router(meeting_state_router.public_router, prefix="/api")
# 水华队长 AI Referee (ADR-002): an ON-DEMAND sidecar over the same read-only
# FINAL transcript. Additive routes under a distinct path prefix; it shares no
# implementation with the Meeting State agent above and stays fully functional
# when that agent is disabled.
app.include_router(captain_router.router, prefix="/api")
# 演示输入（会议室页「模拟参会人发言」）: a product-level ingress that turns a
# typed line into a REAL FINAL by reusing the existing ASR ingress application
# layer — so ordering / idempotency / SSE / Meeting State / 水华队长 wake-up are
# the same code path the hardware uses, with no second implementation to drift.
# It never hands `segment_seq` ownership to the browser (CLAUDE.md hard rule).
# Set `DEMO_INGRESS_ENABLED=false` to remove the route entirely.
app.include_router(demo_router.router, prefix="/api")
# Focused docs for the collaborator-facing internal ASR contract
# (GET /api/internal/docs + /api/internal/openapi.json) — derived from the real
# routes above, so it cannot drift from the implementation.
app.include_router(transcript_internal_docs.router, prefix="/api")

