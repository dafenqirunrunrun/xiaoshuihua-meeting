"""Summary repository: SQLAlchemy queries, no business rules.

Insert / query only. Generation decisions live in summary_service.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models


def insert_summary_item(db: Session, item: models.MeetingSummary) -> models.MeetingSummary:
    """Persist a fresh summary row (idempotency is the caller's business)."""
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def get_for_meeting_version(
    db: Session, meeting_id: str, version: int
) -> models.MeetingSummary | None:
    """The single summary row for (meeting_id, version) — UNIQUE constraint."""
    from app.models.meeting_summary import MeetingSummary

    return db.scalar(
        select(MeetingSummary).where(
            MeetingSummary.meeting_id == meeting_id,
            MeetingSummary.version == version,
        )
    )


def list_sorted(db: Session, meeting_id: str) -> list[models.MeetingSummary]:
    """All summary versions for a meeting, version ASC."""
    from app.models.meeting_summary import MeetingSummary

    return list(
        db.scalars(
            select(MeetingSummary)
            .where(MeetingSummary.meeting_id == meeting_id)
            .order_by(MeetingSummary.version.asc())
        )
    )


def max_version(db: Session, meeting_id: str) -> int | None:
    """Max summary version for a meeting (None if no row exists)."""
    from app.models.meeting_summary import MeetingSummary

    return db.scalar(
        select(func.max(MeetingSummary.version)).where(MeetingSummary.meeting_id == meeting_id)
    )


__all__ = [
    "insert_summary_item",
    "get_for_meeting_version",
    "list_sorted",
    "max_version",
]
