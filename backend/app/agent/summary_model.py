"""Meeting summary ORM model. PRD §46.3.

Summary is an AI-derived result, NOT the source of truth.
status: NOT_STARTED / GENERATING / COMPLETED / FAILED (PRD §12).
version kept for future regeneration (PRD §46.3 / V0.2).
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MeetingSummary(Base):
    __tablename__ = "meeting_summary"
    __table_args__ = (UniqueConstraint("meeting_id", "version", name="uq_summary_meeting_version"),)

    # NOTE: plain Integer PK for SQLite autoincrement (BigInteger does not auto-gen on SQLite).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meeting.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_topics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    raw_model_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_STARTED", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # --- JSON helpers -------------------------------------------------
    @property
    def key_topics(self) -> list[str]:
        return json.loads(self.key_topics_json) if self.key_topics_json else []

    @property
    def conclusions(self) -> list[str]:
        return json.loads(self.conclusions_json) if self.conclusions_json else []

    @property
    def open_questions(self) -> list[str]:
        return json.loads(self.open_questions_json) if self.open_questions_json else []

    @staticmethod
    def dumps(items: list[Any]) -> str:
        return json.dumps(items, ensure_ascii=False)
