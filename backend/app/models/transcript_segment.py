"""Transcript segment ORM model. PRD §46.2.

Append-only store for FINAL segments. Every FINAL event must be persisted here.
Constraints (PRD §23 / §46.2):
  UNIQUE(event_id)
  UNIQUE(meeting_id, segment_seq)
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TranscriptSegment(Base):
    __tablename__ = "transcript_segment"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_transcript_event_id"),
        UniqueConstraint("meeting_id", "segment_seq", name="uq_transcript_meeting_seq"),
    )

    # NOTE: plain int() primary key on SQLite so that autoincrement works —
    # BigInteger PK does NOT auto-generate on SQLite ("integer primary key" only).
    # PostgreSQL would be fine with BigInteger.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meeting.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    utterance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    speaker_id: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(String(4000), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
