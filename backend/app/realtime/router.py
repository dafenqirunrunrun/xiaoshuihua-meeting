"""实时事件路由。"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.realtime import sse_service
from app.realtime.event_broker import event_broker

router = APIRouter(tags=["realtime"])


@router.get("/meetings/{meeting_id}/events")
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
    if after_segment_seq < 0:
        raise HTTPException(
            status_code=400,
            detail=f"after_segment_seq must be >= 0, got {after_segment_seq}",
        )

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


@router.get("/internal/debug/sse-subscribers")
def sse_subscribers() -> dict:
    return {"total_subscribers": event_broker.total_subscribers()}
