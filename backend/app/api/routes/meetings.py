"""Meeting API routes.

PRD §48:
  POST   /api/meetings
  POST   /api/meetings/{meeting_id}/start
  POST   /api/meetings/{meeting_id}/finish
  GET    /api/meetings/{meeting_id}
  GET    /api/meetings/{meeting_id}/transcript
  GET    /api/meetings/{meeting_id}/events
  GET    /api/meetings/{meeting_id}/summary
  POST   /api/meetings/{meeting_id}/summary/regenerate

Step 1: only POST create + GET one. Others implemented in later steps.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import meeting as meeting_schemas
from app.services import meeting_service

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("", response_model=meeting_schemas.MeetingOut, status_code=201)
def create_meeting(payload: meeting_schemas.MeetingCreate, db: Session = Depends(get_db)):
    return meeting_service.create_meeting(db, payload)


@router.get("/{meeting_id}", response_model=meeting_schemas.MeetingOut)
def get_meeting(meeting_id: str, db: Session = Depends(get_db)):
    meeting = meeting_service.get_meeting(db, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"meeting not found: {meeting_id}")
    return meeting
