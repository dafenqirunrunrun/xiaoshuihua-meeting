"""会议生命周期路由。"""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
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
async def finish_meeting(
    meeting_id: str,
    background_tasks: BackgroundTasks,
    finalize_audio: Annotated[
        bool,
        Query(
            description=(
                "关闭并排空正式音频链路，然后冻结转写并自动生成纪要；"
                "内部 Mock/回放调用可保持 false 并单独发送 stream-end"
            )
        ),
    ] = False,
    db: Session = Depends(get_db),
):
    """IN_PROGRESS -> FINALIZING.

    On the way out, schedules a FORCED flush of the meeting-state agent (Step
    AGENT-1): the meeting is ending, so whatever is still buffered below the
    batch thresholds must be folded into the state now rather than waiting for
    FINALs that will never arrive. Fired after the response and on its own DB
    session; disabled-agent and no-work cases are both no-ops. The agent never
    gates this transition — the meeting finishes regardless of what the agent
    does (or fails to do).
    """
    if not finalize_audio:
        meeting = meeting_service.finish_meeting(db, meeting_id)
        if meeting.status == "FINALIZING":
            from app.meeting_state import service as meeting_state_service

            background_tasks.add_task(
                meeting_state_service.run_agent_background,
                meeting_id,
                force_flush=True,
            )
        return meeting

    # Product path: stop accepting new PCM first, but keep the meeting
    # IN_PROGRESS while Tencent emits its trailing FINAL sentence(s). Only after
    # every queue has drained do we transition and freeze the transcript.
    from datetime import datetime, timezone

    from app.agent.finalizer import run_finalizer_background
    from app.audio.session_manager import (
        AudioSessionStopTimeout,
        audio_session_manager,
    )
    from app.config import settings
    from app.transcript import schemas as transcript_schemas
    from app.transcript import stream_service

    device_id = settings.shuihua_audio_device_id
    session = audio_session_manager.begin_stop(device_id, meeting_id)
    try:
        try:
            await audio_session_manager.wait_until_drained(
                session,
                timeout_seconds=settings.audio_session_stop_timeout_seconds,
            )
        except AudioSessionStopTimeout as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "音频链路尚未完成排空，会议仍在进行中；请稍后重试结束会议"
                ),
            ) from exc

        meeting = meeting_service.finish_meeting(db, meeting_id)
        payload = transcript_schemas.StreamEndEvent(
            event_id=f"audio-end-{meeting_id}"[:64],
            meeting_id=meeting_id,
            emitted_at=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        _meeting, _stream, _complete, newly_accepted = (
            stream_service.handle_stream_end(db, meeting_id, payload)
        )
        if newly_accepted:
            background_tasks.add_task(run_finalizer_background, meeting_id)
        return meeting
    finally:
        # Reconnects after this point are harmless: the meeting is FINALIZING or
        # COMPLETED, so the audio WebSocket gate rejects them until the next
        # meeting is started and the stale device binding is replaced.
        audio_session_manager.end_stop(device_id)
