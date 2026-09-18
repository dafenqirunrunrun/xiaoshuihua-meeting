"""会议智能体结果路由。"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agent import summary_repository as summary_repo
from app.agent import summary_schemas
from app.agent.finalizer import run_regenerate_background
from app.agent.summary_service import create_regeneration
from app.db import get_db
from app.meeting import service as meeting_service

router = APIRouter(prefix="/meetings", tags=["agent"])


@router.get("/{meeting_id}/summary", response_model=summary_schemas.SummaryOut)
def get_summary(meeting_id: str, db: Session = Depends(get_db)):
    meeting_service.get_meeting(db, meeting_id)
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
    item = create_regeneration(db, meeting_id)
    background_tasks.add_task(run_regenerate_background, meeting_id)
    return summary_schemas.RegenerationAccepted(
        meeting_id=item.meeting_id, version=item.version, status=item.status
    )
