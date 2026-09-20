"""Meeting State HTTP surface (Step AGENT-1 internal + Step UI-INTEL-1 public).

::

    Public   GET /api/meetings/{meeting_id}/state            -> product projection
    Internal GET  /api/internal/meetings/{meeting_id}/state  -> full debug state
    Internal POST /api/internal/meetings/{meeting_id}/state/process-pending

The two surfaces answer the same question at different levels of detail and are
BOTH kept:

  - The internal pair is the engineering/debug surface (it still exposes
    `model_name`, and still raises on a corrupt row so the problem stays loud).
    The frontend does not use it.
  - The public single endpoint is the one the product UI reads. It returns
    `MeetingStatePublic` — no `model_name`, no internal error detail — and
    degrades a corrupt row to `status=FAILED` rather than turning a data-integrity
    problem into a product 500 (see `service.get_public_state`).

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
from app.meeting_state.schemas import MeetingState, MeetingStatePublic

# Product surface (Step UI-INTEL-1). Read-only; the pipeline remains the only
# writer of Meeting State.
public_router = APIRouter(tags=["meeting-state"])

# Internal debug surface (Step AGENT-1). Unchanged and still functional.
router = APIRouter(tags=["internal"])


@public_router.get("/meetings/{meeting_id}/state", response_model=MeetingStatePublic)
def get_public_meeting_state(
    meeting_id: str, db: Session = Depends(get_db)
) -> MeetingStatePublic:
    """Current Meeting State for the meeting (working memory), product view.

    This is the SOURCE OF TRUTH the Intelligence Panel renders on load, refresh
    and reconnect. `meeting.state.updated` only says "re-read this".

    Returns an empty state (version 0, status EMPTY) when nothing has been
    processed yet — a legitimate answer, not a 404, and the common case early in
    a meeting while the agent's batch thresholds have not been reached. A missing
    MEETING is a 404, consistent with the rest of the API.
    """
    return state_service.get_public_state(db, meeting_id)


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
