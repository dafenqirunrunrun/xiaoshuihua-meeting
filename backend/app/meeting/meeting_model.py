"""Meeting ORM model. PRD §46.1."""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.meeting.id_generator import generate_meeting_code


class Meeting(Base):
    __tablename__ = "meeting"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    meeting_code: Mapped[str] = mapped_column(
        String(9), nullable=False, unique=True, index=True,
        default=generate_meeting_code,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # PRD §11: CREATED / IN_PROGRESS / FINALIZING / COMPLETED
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED", index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
