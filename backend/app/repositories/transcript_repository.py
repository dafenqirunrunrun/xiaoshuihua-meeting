"""Transcript repository: SQLAlchemy queries, no business rules.

Insert / query only. Idempotency decisions live in transcript_service.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models


def insert_segment(db: Session, segment: models.TranscriptSegment) -> models.TranscriptSegment:
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def get_by_event_id(db: Session, meeting_id: str, event_id: str) -> models.TranscriptSegment | None:
    return db.scalar(
        select(models.TranscriptSegment).where(
            models.TranscriptSegment.meeting_id == meeting_id,
            models.TranscriptSegment.event_id == event_id,
        )
    )


def get_by_seq(db: Session, meeting_id: str, segment_seq: int) -> models.TranscriptSegment | None:
    return db.scalar(
        select(models.TranscriptSegment).where(
            models.TranscriptSegment.meeting_id == meeting_id,
            models.TranscriptSegment.segment_seq == segment_seq,
        )
    )


def list_sorted(db: Session, meeting_id: str) -> list[models.TranscriptSegment]:
    """All FINAL segments ordered by segment_seq ASC (PRD §9/ §39, arch §6)."""
    return list(
        db.scalars(
            select(models.TranscriptSegment)
            .where(models.TranscriptSegment.meeting_id == meeting_id)
            .order_by(models.TranscriptSegment.segment_seq.asc())
        )
    )


def max_seq(db: Session, meeting_id: str) -> int | None:
    """Max segment_seq for a meeting (None if empty)."""
    return db.scalar(select(func.max(models.TranscriptSegment.segment_seq)).where(
        models.TranscriptSegment.meeting_id == meeting_id
    ))


# --- Transcript stream end ---------------------------------------------

def get_stream_end(db: Session, meeting_id: str) -> models.TranscriptStream | None:
    return db.get(models.TranscriptStream, meeting_id)


def insert_stream_end(
    db: Session, meeting_id: str, last_segment_seq: int
) -> models.TranscriptStream:
    stream = models.TranscriptStream(
        meeting_id=meeting_id, last_segment_seq=last_segment_seq, status="ACCEPTED"
    )
    db.add(stream)
    db.commit()
    db.refresh(stream)
    return stream
