"""FastAPI app entry. PRD §7.

Run:  uvicorn app.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import health, internal_transcript, meetings
from app.config import settings
from app.db import create_all
from app.exceptions import (
    InvalidMeetingStateTransition,
    MeetingNotFoundError,
    TranscriptConflictError,
    TranscriptValidationError,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_all()  # ensure tables exist (PRD §46)
    yield


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


app.include_router(health.router, prefix="/api")
app.include_router(meetings.router, prefix="/api")
app.include_router(internal_transcript.router, prefix="/api")
