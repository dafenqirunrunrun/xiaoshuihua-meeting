"""会议生命周期路由。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
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
def finish_meeting(meeting_id: str, db: Session = Depends(get_db)):
    return meeting_service.finish_meeting(db, meeting_id)
