"""水华队长 HTTP surface (ADR-002 §6).

::

    POST /api/meetings/{meeting_id}/captain            -> 202 Accepted
    GET  /api/meetings/{meeting_id}/captain/{report_id} -> report (durable)
    GET  /api/meetings/{meeting_id}/captain/latest      -> most recent report

Why 202 and not a synchronous report
------------------------------------
A report costs one LLM call. Returning it inline would hold an HTTP worker
thread for the duration on a single-process MVP, and — worse — the result would
be unrecoverable: a user who refreshes mid-analysis loses it. So the POST only
creates the durable request and returns immediately; the client reads the result
from the GET, which is the source of truth. This is the same shape the summary
regeneration endpoint already uses (`POST /summary/regenerate` -> 202).

The POST body is optional: the wake-word path supplies an explicit anchor, the
button path omits it and the server takes the current high-water mark.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.captain import service as captain_service
from app.captain.schemas import CaptainReportPublic
from app.captain.worker import captain_worker
from app.common.exceptions import MeetingNotFoundError
from app.config import settings
from app.db import get_db

router = APIRouter(tags=["captain"])


class TriggerRequest(BaseModel):
    """Optional trigger parameters.

    `anchor_segment_seq` is the FINAL that triggered this report. The wake-word
    path already knows it; a manual button press omits it and the server uses
    the current max. Supplying it makes the trigger idempotent, which is what
    stops a re-delivered wake word from paying for a second analysis.
    """

    anchor_segment_seq: int | None = Field(default=None, ge=1)


class TriggerAccepted(BaseModel):
    """202 body: the durable handle for a report that is now in flight."""

    report_id: str
    meeting_id: str
    status: str
    anchor_segment_seq: int
    created: bool
    # False when this anchor was already analysed (or is analysing) — the client
    # polls the same report rather than a new one.
    reused: bool = False


@router.post(
    "/meetings/{meeting_id}/captain",
    response_model=TriggerAccepted,
    status_code=202,
)
def trigger_captain(
    meeting_id: str,
    payload: TriggerRequest | None = None,
    db: Session = Depends(get_db),
) -> TriggerAccepted:
    """Request a referee report. Returns immediately with a durable handle.

    Never waits for the LLM: the analysis runs on the captain dispatch worker.
    """
    if not settings.captain_enabled:
        raise HTTPException(status_code=503, detail="captain is disabled")

    # Throttle BEFORE creating anything: a rejected trigger must leave no row
    # behind, otherwise the dedupe key would block the user's legitimate retry.
    remaining = captain_service.throttle_remaining(meeting_id)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"captain was invoked recently, retry in {remaining:.1f}s",
        )

    try:
        row, created = captain_service.trigger(
            db,
            meeting_id,
            anchor_segment_seq=(payload.anchor_segment_seq if payload else None),
        )
    except MeetingNotFoundError:
        raise
    except Exception as exc:  # CaptainError / transition errors
        from app.captain.service import CaptainError
        from app.common.exceptions import InvalidMeetingStateTransition

        if isinstance(exc, (CaptainError, InvalidMeetingStateTransition)):
            raise
        raise

    if created:
        queued = captain_worker.submit(meeting_id, row.report_id)
        if not queued:
            # The queue is full. Fail the report now rather than leaving a
            # PENDING row that can never resolve and a spinner that never stops.
            captain_service.run_analysis_failure(db, row.report_id, "captain queue full")
            raise HTTPException(status_code=503, detail="captain queue is full")

    return TriggerAccepted(
        report_id=row.report_id,
        meeting_id=meeting_id,
        status=row.status,
        anchor_segment_seq=row.anchor_segment_seq,
        created=created,
        reused=not created,
    )


@router.get(
    "/meetings/{meeting_id}/captain/latest",
    response_model=CaptainReportPublic,
)
def get_latest_captain_report(
    meeting_id: str, db: Session = Depends(get_db)
) -> CaptainReportPublic:
    """Most recent report for the meeting — the refresh/reconnect read.

    Returns 404 when the meeting has never been refereed. A meeting that exists
    but has no report is a normal state, not an error in itself, but the client
    needs to distinguish "no report yet" from "report exists and is running", so
    absence is surfaced as 404 rather than an empty object.
    """
    report = captain_service.get_latest_report(db, meeting_id)
    if report is None:
        raise HTTPException(status_code=404, detail="no captain report for this meeting")
    return report


@router.get(
    "/meetings/{meeting_id}/captain/{report_id}",
    response_model=CaptainReportPublic,
)
def get_captain_report(
    meeting_id: str, report_id: str, db: Session = Depends(get_db)
) -> CaptainReportPublic:
    """One report by id. The durable source of truth for the referee UI."""
    report = captain_service.get_report(db, report_id)
    if report is None or report.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="captain report not found")
    return report


__all__ = ["router", "TriggerRequest", "TriggerAccepted"]
