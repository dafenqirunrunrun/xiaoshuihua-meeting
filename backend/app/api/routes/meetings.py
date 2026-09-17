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

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import meeting as meeting_schemas
from app.schemas import transcript as transcript_schemas
from app.services import meeting_service, sse_service, transcript_service

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


@router.get("/{meeting_id}/events")
async def get_events(
    meeting_id: str,
    after_segment_seq: int = Query(
        default=0,
        description=(
            "Recovery cursor: replay persisted FINAL with segment_seq > this value. "
            "Default 0 = replay the full transcript on connect."
        ),
    ),
):
    """SSE incremental event stream (PRD §30/§32).

    Contract:
      Content-Type: text/event-stream
      no WebSocket; single GET stream per connection.

    Recovery (gap-free, Step 5 Part 10/11):
      caller reads Snapshot -> max_segment_seq = N, then connects with
      after_segment_seq=N; persisted FINAL with seq > N are replayed first
      (catch-up) inside the same per-meeting lock used by FINAL ingest, so no
      FINAL is lost or duplicated. Then live events flow.

    after_segment_seq < 0 -> 400 (Part 17). If > current max seq ->
    catch-up empty and live continues (documented in api-contract).
    """
    if after_segment_seq < 0:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail=f"after_segment_seq must be >= 0, got {after_segment_seq}",
        )

    # connect() lives on this event loop; 404 if meeting missing (Part 15).
    sub, catchup = sse_service.connect(meeting_id, after_segment_seq)
    return StreamingResponse(
        sse_service.event_stream(sub, meeting_id, catchup),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
