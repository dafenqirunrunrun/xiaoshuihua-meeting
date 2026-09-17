"""SQLAlchemy engine / session factory.

MVP uses SQLite via DATABASE_URL. The rest of the app talks to repositories,
not to this module directly, so swapping to a company data platform later
only changes this file + connection settings.
"""

# NOTE: SQLite needs `check_same_thread` for FastAPI's threadpool.
# Even though we use async I/O, SQLAlchemy sync sessions are used for MVP simplicity.
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# The `data` dir must exist for the SQLite file.
import os
import pathlib

_db_path = settings.database_url
if _db_path.startswith("sqlite:///"):
    _file = _db_path.removeprefix("sqlite:///")
    pathlib.Path(os.path.dirname(_file) or ".").mkdir(parents=True, exist_ok=True)

_connect_args: dict = {}
if _db_path.startswith("sqlite"):
    # check_same_thread: FastAPI story threadpool + sync sessions.
    # timeout: SQLite busy timeout (seconds) — lets concurrent writers wait for
    # the write lock instead of failing with "database is locked". MVP single
    # writer; see report for PostgreSQL hardening notes.
    _connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, connect_args=_connect_args)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def create_all() -> None:
    """Create tables (imports models first)."""
    from app import models  # noqa: F401  (register models on Base)

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
