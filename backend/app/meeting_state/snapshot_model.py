"""Durable checkpoints and health for realtime meeting understanding."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MeetingMemorySnapshot(Base):
    """One successfully processed contiguous transcript window."""

    __tablename__ = "meeting_memory_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id", "from_segment_seq", "to_segment_seq",
            name="uq_meeting_memory_snapshot_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meeting.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_segment_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    to_segment_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A validated full working-memory checkpoint after this window was folded.
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MeetingStateHealth(Base):
    """Operational diagnostics kept separate from the product state payload."""

    __tablename__ = "meeting_state_health"

    meeting_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meeting.id", ondelete="CASCADE"), primary_key=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["MeetingMemorySnapshot", "MeetingStateHealth"]
