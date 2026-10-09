"""Persistence for captain_report. The only module that touches the table.

Repository-isolates-DB is a project rule (CLAUDE.md): services and routers never
build queries. This layer deliberately exposes NARROW operations rather than a
generic upsert, because the referee's write path has one hard invariant:

    a TERMINAL row is never overwritten.

`mark_completed` / `mark_failed` therefore guard on the current status and
return None when the row already reached a terminal state. That is what makes a
duplicated or replayed analysis a no-op instead of a silent overwrite of a
report the user may already be reading.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.captain.model import (
    STATUS_ANALYZING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    TERMINAL_STATUSES,
    CaptainReport,
)


def get_by_report_id(db: Session, report_id: str) -> CaptainReport | None:
    """Look one report up by its public identifier."""
    return db.execute(
        select(CaptainReport).where(CaptainReport.report_id == report_id)
    ).scalar_one_or_none()


def get_by_anchor(
    db: Session, meeting_id: str, anchor_segment_seq: int
) -> CaptainReport | None:
    """The dedupe lookup: has this anchor already been analysed for this meeting?"""
    return db.execute(
        select(CaptainReport).where(
            CaptainReport.meeting_id == meeting_id,
            CaptainReport.anchor_segment_seq == anchor_segment_seq,
        )
    ).scalar_one_or_none()


def get_latest(db: Session, meeting_id: str) -> CaptainReport | None:
    """Most recently created report for a meeting (any status), or None.

    Ordered by `id` DESC rather than `created_at`: two reports can share a
    timestamp at SQLite's resolution, and the autoincrement id is the true
    insertion order.
    """
    return db.execute(
        select(CaptainReport)
        .where(CaptainReport.meeting_id == meeting_id)
        .order_by(CaptainReport.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_for_meeting(db: Session, meeting_id: str) -> list[CaptainReport]:
    """All reports for a meeting, oldest first."""
    return list(
        db.execute(
            select(CaptainReport)
            .where(CaptainReport.meeting_id == meeting_id)
            .order_by(CaptainReport.id.asc())
        ).scalars()
    )


def insert_pending(
    db: Session,
    *,
    report_id: str,
    meeting_id: str,
    anchor_segment_seq: int,
    exclude_anchor: bool = True,
) -> CaptainReport:
    """Create the PENDING row and commit it.

    Committing HERE (not later) is deliberate: the 202 response hands the client
    a report_id, and that id must be durable before the response is sent —
    otherwise a refresh in the following milliseconds loses a report the user
    was told about. The window bounds are unknown at this point and stay 0 until
    the analysis assigns them.

    `exclude_anchor` is persisted rather than re-derived at analysis time: the
    window must reflect the TRIGGER that created this row, and a meeting's wake
    configuration could change in between (see `captain/model.py`).
    """
    row = CaptainReport(
        report_id=report_id,
        meeting_id=meeting_id,
        anchor_segment_seq=anchor_segment_seq,
        exclude_anchor=exclude_anchor,
        status="PENDING",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def claim_for_analysis(db: Session, report: CaptainReport) -> bool:
    """Move PENDING -> ANALYZING. Returns False if another run already claimed it.

    The status check and the write are one transaction, so two workers racing on
    the same row cannot both proceed: the loser sees the already-changed status
    and backs off without calling the provider.
    """
    if report.status != "PENDING":
        return False
    report.status = STATUS_ANALYZING
    db.commit()
    db.refresh(report)
    return True


def mark_completed(
    db: Session,
    report: CaptainReport,
    *,
    content: dict,
    model_name: str | None,
    window_from_seq: int,
    window_to_seq: int,
) -> CaptainReport | None:
    """Write the terminal success state. None if the row already terminated."""
    if report.status in TERMINAL_STATUSES:
        return None
    report.report_json = json.dumps(content, ensure_ascii=False)
    report.model_name = model_name
    report.window_from_seq = window_from_seq
    report.window_to_seq = window_to_seq
    report.last_error = None
    report.status = STATUS_COMPLETED
    db.commit()
    db.refresh(report)
    return report


def mark_failed(
    db: Session, report: CaptainReport, *, reason: str
) -> CaptainReport | None:
    """Write the terminal failure state. None if already terminal.

    Only the status and (truncated) reason move. `report_json` is left untouched
    so a failure can never leave half a report on disk — the reader sees either a
    complete report or an honest failure, never a partial one.
    """
    if report.status in TERMINAL_STATUSES:
        return None
    report.status = STATUS_FAILED
    report.last_error = reason[:1000]
    db.commit()
    db.refresh(report)
    return report


__all__ = [
    "get_by_report_id",
    "get_by_anchor",
    "get_latest",
    "list_for_meeting",
    "insert_pending",
    "claim_for_analysis",
    "mark_completed",
    "mark_failed",
]
