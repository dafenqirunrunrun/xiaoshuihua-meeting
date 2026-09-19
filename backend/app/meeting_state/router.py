"""Internal debug API for Meeting State (Step AGENT-1).

::

    GET  /api/internal/meetings/{meeting_id}/state                  -> current state
    POST /api/internal/meetings/{meeting_id}/state/process-pending  -> force a drain

Both endpoints are for development / debugging ONLY. They are NOT a product UI
surface and the frontend does not use them (the step spec is explicit: build the
backend chain first, decide on any UI later). They live under `/api/internal`
alongside the other internal contracts so the product surface stays clean.

`process-pending` exists because the batch thresholds deliberately mean "the
state may lag the transcript"; without a way to force a drain, an operator
debugging a partially-processed meeting would have to inject transcript
segments. It is idempotent: calling it twice with nothing new to process reports
NO_WORK and changes nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.meeting_state import service as state_service
from app.meeting_state.schemas import MeetingState

router = APIRouter(tags=["internal"])


@router.get("/internal/meetings/{meeting_id}/state", response_model=MeetingState)
def get_meeting_state(meeting_id: str, db: Session = Depends(get_db)) -> MeetingState:
    """Current structured meeting state (working memory) for a meeting.

    Returns an empty state (version 0, status EMPTY) when nothing has been
    processed yet — that is a legitimate answer, not a 404. A missing meeting is
    a 404, consistent with the rest of the API.
    """
    return state_service.get_state(db, meeting_id)


@router.post("/internal/meetings/{meeting_id}/state/process-pending")
def process_pending_state(
    meeting_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Schedule a drain of any pending FINAL into the meeting state.

    Runs in the background (own DB session) so this debug call never blocks on
    the LLM, mirroring how the real triggers behave.
    """
    meeting = state_service.get_state(db, meeting_id)  # 404 if the meeting is missing
    background_tasks.add_task(state_service.run_agent_background, meeting_id)
    return {
        "meeting_id": meeting_id,
        "scheduled": True,
        "version_before": meeting.version,
        "last_processed_segment_seq": meeting.last_processed_segment_seq,
    }
