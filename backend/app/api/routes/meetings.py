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
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.repositories import summary_repository as summary_repo
from app.schemas import meeting as meeting_schemas
from app.schemas import summary as summary_schemas
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


@router.get("/{meeting_id}/summary", response_model=summary_schemas.SummaryOut)
def get_summary(meeting_id: str, db: Session = Depends(get_db)):
    """Latest-version Summary snapshot — durable recovery fact-source (Step 7B §15/§16).

    Frontend recovery model: GET Meeting + GET Summary + Transcript Snapshot +
    SSE. Summary SSE events are NON-durable (not replayed on reconnect); this
    endpoint is how a reconnecting client sees the current state:

      - GENERATING / COMPLETED / FAILED -> 200 with the latest version's row
      - meeting does not exist            -> 404
      - meeting exists but no summary yet -> 404 ("summary not found")
    """
    # Meeting existence is checked by the repository lookup below (summary rows
    # are FK'd to meeting), but we want a precise 404 "meeting not found" when
    # the meeting itself is missing vs "summary not found" when it has none.
    meeting_service.get_meeting(db, meeting_id)  # raises MeetingNotFoundError -> 404
    item = summary_repo.get_latest(db, meeting_id)
    if item is None:
        raise HTTPException(status_code=404, detail="summary not found")
    return item


@router.post(
    "/{meeting_id}/summary/regenerate",
    response_model=summary_schemas.RegenerationAccepted,
    status_code=202,
)
def regenerate_summary(
    meeting_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> summary_schemas.RegenerationAccepted:
    """Explicit human regeneration (Step 7B §17-§22).

    202 Accepted (does NOT block on DeepSeek):
      1. synchronously allocate vN+1 + create a GENERATING row
         (create_regeneration; 409 if the latest is already GENERATING)
      2. schedule the background Phase B/C completion on OUR OWN DB session
      3. return {meeting_id, version, status: "GENERATING"}

    The meeting may be COMPLETED — regeneration is a Summary-lifecycle concern
    and never moves the Meeting out of COMPLETED (§19). Version always appends.
    """
    from app.services.finalizer_service import run_regenerate_background
    from app.services.summary_service import create_regeneration

    item = create_regeneration(db, meeting_id)
    background_tasks.add_task(run_regenerate_background, meeting_id)
    return summary_schemas.RegenerationAccepted(
        meeting_id=item.meeting_id, version=item.version, status=item.status
    )
