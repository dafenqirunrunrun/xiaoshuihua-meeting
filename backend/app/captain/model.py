"""captain_report ORM model (ADR-002).

One row per requested referee report. A meeting accumulates MANY rows over its
lifetime (one per trigger), which is the opposite shape from `meeting_state`
(exactly one row per meeting, repeatedly REPLACED). That difference is why this
is a new table rather than a column on an existing one.

This is a NEW table created by `Base.metadata.create_all()` — NOT an ALTER and
NOT a migration, so existing databases pick it up on the next start with zero
risk to the existing tables (same convention as `meeting_state`).

Append-only semantics
---------------------
A report is written once as PENDING, then moves to ANALYZING and finally to a
TERMINAL state (COMPLETED / FAILED). A terminal row is NEVER overwritten — the
same discipline `meeting_summary` uses for its versions. Re-triggering produces
a NEW row with a new anchor, never a mutation of an old report.

Window provenance
-----------------
`anchor_segment_seq` / `window_from_seq` / `window_to_seq` are recorded so a
report is auditable after the fact: the reader can tell exactly which stretch of
transcript the referee saw, and can detect a report that was produced from a
degraded (truncated or too-small) window.

`status` values are the REFEREE's own run status — they are NOT meeting states.
The meeting state machine stays four-state (ADR D1); this is a separate entity's
lifecycle field, exactly like `meeting_summary.status` and `meeting_state.status`.
"""

import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# The referee's own report lifecycle. NOT meeting states (ADR D1 untouched).
STATUS_PENDING = "PENDING"
STATUS_ANALYZING = "ANALYZING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})


class CaptainReport(Base):
    __tablename__ = "captain_report"
    __table_args__ = (
        # Dedupe key. The wake word can be FINAL-ised as its own utterance and a
        # user can double-click the button; both collapse onto the same anchor
        # and must reuse one report rather than paying for the LLM twice.
        UniqueConstraint(
            "meeting_id", "anchor_segment_seq", name="uq_captain_meeting_anchor"
        ),
    )

    # NOTE: plain Integer PK so SQLite autoincrement works (BigInteger does not
    # auto-generate on SQLite). Same note as transcript_segment / meeting_summary.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Public-facing identifier. Stable across the PENDING -> COMPLETED
    # transition so the client can poll/GET the row it was handed at 202 time.
    report_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    meeting_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meeting.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # PENDING / ANALYZING / COMPLETED / FAILED (this table's own lifecycle).
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STATUS_PENDING, index=True
    )

    # The FINAL that triggered the report. Also the dedupe key.
    anchor_segment_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    # Is the anchor the wake PHRASE, or just the latest utterance?
    #
    # `anchor_segment_seq` conflates two things that need opposite treatment in
    # `context.select_window`, so the distinction is persisted rather than
    # re-derived (a meeting's wake configuration can change between the trigger
    # and the analysis, and re-deriving would then silently change the window of
    # an already-created report).
    #
    #   True   wake-word trigger — the anchor IS the wake phrase, not discussion.
    #   False  button / explicit anchor — ordinary content; keep it in the window.
    #
    # Defaults to True so any row created by an older code path keeps the stricter
    # (wake-word) behaviour rather than silently pulling in a possible wake phrase.
    exclude_anchor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )

    # Inclusive bounds of the transcript window actually analysed. 0/0 before a
    # run assigns them (PENDING rows are created before the window is known).
    window_from_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_to_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Serialized CaptainReportContent, written ONLY on success. Text (not a JSON
    # column) to stay portable across the SQLite -> company data platform move,
    # matching the existing *_json convention.
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provider identity of the successful run (traceability). Comes from
    # `provider.name`; no separate knob that could drift from what was called.
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Human-readable failure reason. Never contains transcript text, prompt
    # bodies, API keys or provider stacks.
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

    # --- JSON helpers -------------------------------------------------------
    def report(self) -> dict | None:
        """Decoded report content, or None when not COMPLETED."""
        return json.loads(self.report_json) if self.report_json else None


__all__ = [
    "CaptainReport",
    "STATUS_PENDING",
    "STATUS_ANALYZING",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "TERMINAL_STATUSES",
]
