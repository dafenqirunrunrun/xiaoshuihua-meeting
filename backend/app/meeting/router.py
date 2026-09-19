"""会议生命周期路由。"""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.meeting import schemas as meeting_schemas
from app.meeting import service as meeting_service

router = APIRouter(prefix="/meetings", tags=["meetings"])

# GET /api/meetings 必须注册在 GET /api/meetings/{meeting_id} 之前，
# 否则 FastAPI 会把字面量 "/meetings" 交给 "/{meeting_id}" 匹配。
@router.get("", response_model=meeting_schemas.MeetingListOut)
def list_meetings(
    db: Session = Depends(get_db),
    status: Annotated[
        Literal["CREATED", "IN_PROGRESS", "FINALIZING", "COMPLETED"] | None,
        Query(description="按 Meeting 状态过滤（四态之一）"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=meeting_service.LIST_MAX_LIMIT)] = (
        meeting_service.LIST_DEFAULT_LIMIT
    ),
    offset: Annotated[int, Query(ge=0)] = 0,
) -> meeting_schemas.MeetingListOut:
    items, total = meeting_service.list_meetings(
        db, status=status, limit=limit, offset=offset
    )
    return meeting_schemas.MeetingListOut(
        items=items, total=total, limit=limit, offset=offset
    )


@router.post("", response_model=meeting_schemas.MeetingOut, status_code=201)
def create_meeting(payload: meeting_schemas.MeetingCreate, db: Session = Depends(get_db)):
    return meeting_service.create_meeting(db, payload)


@router.get("/{meeting_id}", response_model=meeting_schemas.MeetingOut)
def get_meeting(meeting_id: str, db: Session = Depends(get_db)):
    return meeting_service.get_meeting(db, meeting_id)


@router.post("/{meeting_id}/start", response_model=meeting_schemas.MeetingOut)
def start_meeting(meeting_id: str, db: Session = Depends(get_db)):
    return meeting_service.start_meeting(db, meeting_id)


@router.post("/{meeting_id}/finish", response_model=meeting_schemas.MeetingOut)
def finish_meeting(
    meeting_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """IN_PROGRESS -> FINALIZING.

    On the way out, schedules a FORCED flush of the meeting-state agent (Step
    AGENT-1): the meeting is ending, so whatever is still buffered below the
    batch thresholds must be folded into the state now rather than waiting for
    FINALs that will never arrive. Fired after the response and on its own DB
    session; disabled-agent and no-work cases are both no-ops. The agent never
    gates this transition — the meeting finishes regardless of what the agent
    does (or fails to do).
    """
    meeting = meeting_service.finish_meeting(db, meeting_id)
    if meeting.status == "FINALIZING":
        from app.meeting_state import service as meeting_state_service

        background_tasks.add_task(
            meeting_state_service.run_agent_background, meeting_id, force_flush=True
        )
    return meeting
