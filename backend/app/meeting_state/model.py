"""Meeting State ORM model (Step AGENT-1).

One row per meeting, holding the latest structured meeting state. This is a NEW
table created by `Base.metadata.create_all()` — NOT an ALTER and NOT a
migration, so existing databases pick it up on the next start with zero risk to
the three MVP tables.

Why a new table instead of reusing an existing one
--------------------------------------------------
`meeting` (four-state lifecycle, PRD §11), `transcript_segment` (append-only
FINAL facts) and `meeting_summary` (append-only versioned FINAL results) all
have meanings that do NOT match "a working memory that is repeatedly REPLACED".
Folding state into `meeting_summary` in particular would corrupt the version
semantics the frontend's Result page relies on (append-only, v1 never
overwritten). So: new table, deliberately.

Shape
-----
`meeting_id` is the PRIMARY KEY: exactly one current state per meeting, which is
what makes the update a simple replace rather than another version chain. The
structured payload lives in `state_json` (same convention as
`meeting_summary.key_topics_json`), while the fields the pipeline reads without
parsing (`version`, `last_processed_segment_seq`, `status`) are real columns —
the cursor must be queryable/updatable without JSON round-tripping.

Concurrency
-----------
All writes go through `common.meeting_lock` (the same per-meeting lock FINAL
ingest and stream-end use), so a single process is fully serialized. `version`
is a monotonic counter on top of that. This is NOT a cross-process guarantee —
see `docs/architecture.md` AGENT-1 limitations (PostgreSQL / multi-worker would
need a row lock or a conditional `UPDATE ... WHERE version = :expected`).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MeetingState(Base):
    __tablename__ = "meeting_state"

    meeting_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("meeting.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Monotonic state version: 0 = no rows processed yet, N = N successful
    # updates. Incremented by the pipeline only, never by the model.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The idempotency cursor: the highest segment_seq already folded into this
    # state. A FINAL at or below this value has already been processed and will
    # never be processed twice (duplicate / replayed delivery is a no-op).
    last_processed_segment_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # Serialized MeetingStateContent. Text (not JSON column) to stay portable
    # across the SQLite -> company data platform move, matching the existing
    # meeting_summary *_json convention.
    state_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # EMPTY / ACTIVE / STALE (a transient update failed but last-good content is
    # preserved) / FAILED (no usable content yet) / FROZEN (archived last-good
    # view; its cursor may trail the independently frozen transcript).
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="EMPTY")

    # Provider identity of the last SUCCESSFUL update (traceability: which model
    # produced the current state). Comes from `provider.name` — we deliberately
    # do not carry a separate MEETING_AGENT_MODEL knob, which could drift from
    # the model actually called.
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Human-readable reason for the last failure. Never contains transcript
    # text, prompt bodies, API keys or provider stacks.
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
