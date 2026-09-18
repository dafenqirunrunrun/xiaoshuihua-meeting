"""会议生命周期路由。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.meeting import schemas as meeting_schemas
from app.meeting import service as meeting_service

router = APIRouter(prefix="/meetings", tags=["meetings"])


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
