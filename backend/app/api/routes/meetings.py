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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import meeting as meeting_schemas
from app.schemas import transcript as transcript_schemas
from app.services import meeting_service, transcript_service

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("", response_model=meeting_schemas.MeetingOut, status_code=201)
def create_meeting(payload: meeting_schemas.MeetingCreate, db: Session = Depends(get_db)):
    return meeting_service.create_meeting(db, payload)


@router.get("/{meeting_id}", response_model=meeting_schemas.MeetingOut)
def get_meeting(meeting_id: str, db: Session = Depends(get_db)):
    return meeting_service.get_meeting(db, meeting_id)


@router.post("/{meeting_id}/start", response_model=meeting_schemas.MeetingOut)
def start_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """CREATED → IN_PROGRESS. Idempotent; 409 if already FINALIZING/COMPLETED."""
    return meeting_service.start_meeting(db, meeting_id)


@router.post("/{meeting_id}/finish", response_model=meeting_schemas.MeetingOut)
def finish_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """IN_PROGRESS → FINALIZING. Idempotent; 409 if still CREATED."""
    return meeting_service.finish_meeting(db, meeting_id)


@router.get("/{meeting_id}/transcript", response_model=transcript_schemas.TranscriptSnapshot)
def get_transcript(meeting_id: str, db: Session = Depends(get_db)):
    """FINAL Transcript snapshot, ordered by segment_seq ASC (arch §6, PRD §32)."""
    meeting, segments = transcript_service.get_snapshot(db, meeting_id)
    return transcript_schemas.TranscriptSnapshot(
        meeting_id=meeting.id,
        segments=list(segments),
    )
