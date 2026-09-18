"""Transcript repository: SQLAlchemy queries, no business rules.

Insert / query only. Idempotency decisions live in transcript_service.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.transcript.segment_model import TranscriptSegment
from app.transcript.stream_model import TranscriptStream


def insert_segment(db: Session, segment: TranscriptSegment) -> TranscriptSegment:
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def get_by_event_id(db: Session, meeting_id: str, event_id: str) -> TranscriptSegment | None:
    return db.scalar(
        select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == meeting_id,
            TranscriptSegment.event_id == event_id,
        )
    )


def get_by_seq(db: Session, meeting_id: str, segment_seq: int) -> TranscriptSegment | None:
    return db.scalar(
        select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == meeting_id,
            TranscriptSegment.segment_seq == segment_seq,
        )
    )


def list_sorted(db: Session, meeting_id: str) -> list[TranscriptSegment]:
    """All FINAL segments ordered by segment_seq ASC (PRD §9/ §39, arch §6)."""
    return list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id)
            .order_by(TranscriptSegment.segment_seq.asc())
        )
    )


def max_seq(db: Session, meeting_id: str) -> int | None:
    """Max segment_seq for a meeting (None if empty)."""
    return db.scalar(select(func.max(TranscriptSegment.segment_seq)).where(
        TranscriptSegment.meeting_id == meeting_id
    ))


# --- Transcript stream end ---------------------------------------------

def get_stream_end(db: Session, meeting_id: str) -> TranscriptStream | None:
    return db.get(TranscriptStream, meeting_id)


def insert_stream_end(
    db: Session, meeting_id: str, last_segment_seq: int
) -> TranscriptStream:
    stream = TranscriptStream(
        meeting_id=meeting_id, last_segment_seq=last_segment_seq, status="ACCEPTED"
    )
    db.add(stream)
    db.commit()
    db.refresh(stream)
    return stream



