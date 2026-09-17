"""FastAPI app entry. PRD §7.

Run:  uvicorn app.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health, meetings
from app.config import settings
from app.db import create_all


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_all()  # ensure tables exist (PRD §46)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router, prefix="/api")
app.include_router(meetings.router, prefix="/api")
