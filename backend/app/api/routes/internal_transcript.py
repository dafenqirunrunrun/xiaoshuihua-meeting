"""Internal API: transcript event ingress.

This is the formal contract for the upstream ASR / Mock Transcript Producer.
Real ASR does NOT talk to business endpoints; it talks to this internal API.

Routes:
  POST /api/internal/meetings/{meeting_id}/transcript-events   (ingest PARTIAL/FINAL)
  GET  /api/meetings/{meeting_id}/transcript                    (Snapshot, PRD §48)
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.exceptions import InvalidMeetingStateTransition, MeetingNotFoundError, TranscriptConflictError, TranscriptValidationError
from app.schemas import transcript as transcript_schemas
from app.services import transcript_service

router = APIRouter(tags=["internal"])


@router.post("/internal/meetings/{meeting_id}/transcript-events", status_code=status.HTTP_200_OK)
def ingest_transcript_event(
    meeting_id: str,
    event: transcript_schemas.TranscriptEvent,
    db: Session = Depends(get_db),
) -> Response:
    """Ingest a single PARTIAL / FINAL transcript event (idempotent for FINAL)."""
    transcript_service.handle_event(db, meeting_id, event)
    return Response(status_code=status.HTTP_200_OK)
