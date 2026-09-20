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
from app.common import health_router
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
# Meeting State agent debug surface (Step AGENT-1): a downstream consumer of the
# FINAL stream. Additive and independent — it shares no routes with the existing
# routers, and it stays functional (empty state) even when the agent is disabled.
app.include_router(meeting_state_router.router, prefix="/api")
# Public read-only Meeting State (Step UI-INTEL-1): the product surface the
# Intelligence Panel reads. Additive route, distinct path from every existing
# one; the internal debug router above is untouched.
app.include_router(meeting_state_router.public_router, prefix="/api")
# Focused docs for the collaborator-facing internal ASR contract
# (GET /api/internal/docs + /api/internal/openapi.json) — derived from the real
# routes above, so it cannot drift from the implementation.
app.include_router(transcript_internal_docs.router, prefix="/api")

