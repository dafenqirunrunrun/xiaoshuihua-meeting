"""Internal API: transcript event ingress.

This is the formal contract for the upstream ASR / Mock Transcript Producer.
Real ASR does NOT talk to business endpoints; it talks to this internal API.

Routes:
  POST /api/internal/meetings/{meeting_id}/transcript-events   (ingest PARTIAL/FINAL)
  GET  /api/meetings/{meeting_id}/transcript                    (Snapshot, PRD §48)
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.transcript import schemas as transcript_schemas
from app.transcript import service as transcript_service
from app.transcript import stream_service

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


@router.post("/internal/meetings/{meeting_id}/stream-end", response_model=transcript_schemas.StreamEndOut)
def ingest_stream_end(
    meeting_id: str,
    background_tasks: BackgroundTasks,
    payload: transcript_schemas.StreamEndEvent,
    db: Session = Depends(get_db),
) -> transcript_schemas.StreamEndOut:
    """Accept the terminal TRANSCRIPT_STREAM_END after integrity check (durable).

    On the FIRST accepted stream-end (
ewly_accepted=True`) schedules the
    Finalizer as a FastAPI BackgroundTask (process-local trigger; NOT a durable
    queue — see arch §9.4 / ADR D15). Idempotent same-value retries return 200
    with newly_accepted=False and never re-schedule the Summary pipeline.
    """
    meeting, stream, complete, newly_accepted = stream_service.handle_stream_end(db, meeting_id, payload)
    if newly_accepted:
        from app.agent.finalizer import run_finalizer_background

        background_tasks.add_task(run_finalizer_background, meeting_id)
    return transcript_schemas.StreamEndOut(
        meeting_id=meeting.id,
        last_segment_seq=stream.last_segment_seq,
        transcript_complete=complete,
    )


