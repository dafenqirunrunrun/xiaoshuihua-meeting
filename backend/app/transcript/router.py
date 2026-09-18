"""字幕查询路由。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.transcript import schemas as transcript_schemas
from app.transcript import service as transcript_service

router = APIRouter(prefix="/meetings", tags=["transcript"])


@router.get("/{meeting_id}/transcript", response_model=transcript_schemas.TranscriptSnapshot)
def get_transcript(meeting_id: str, db: Session = Depends(get_db)):
    meeting, segments = transcript_service.get_snapshot(db, meeting_id)
    return transcript_schemas.TranscriptSnapshot(
        meeting_id=meeting.id,
        segments=list(segments),
    )
