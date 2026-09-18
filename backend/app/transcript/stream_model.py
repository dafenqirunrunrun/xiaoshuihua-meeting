"""Transcript stream-end persistence model.

Persists the accepted TRANSCRIPT_STREAM_END state so it survives process restarts
(Part 5 / §13 Case 12). One terminal stream end per meeting.

PRD §25 contract: event_type=TRANSCRIPT_STREAM_END, last_segment_seq, emitted_at.
Fields kept minimal and aligned with PRD; no invented redundancy.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TranscriptStream(Base):
    __tablename__ = "transcript_stream"

    # One stream end per meeting (single terminal point, Part 7).
    meeting_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meeting.id", ondelete="CASCADE"), primary_key=True
    )
    last_segment_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACCEPTED")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
