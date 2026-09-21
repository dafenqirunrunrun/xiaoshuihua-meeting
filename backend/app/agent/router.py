"""会议智能体结果路由。"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agent import summary_repository as summary_repo
from app.agent import summary_schemas
from app.agent.final_summary_schemas import project_summary_evidence
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

    # Step UI-INTEL-2B: attach the OPTIONAL structured evidence projection.
    # Built here rather than as a model property so a projection failure can
    # only ever degrade ONE optional field — it must never turn a readable
    # summary into a 500 (an unparseable raw_model_output is a known, expected
    # state: legacy rows and FAILED reasons are not JSON at all).
    #
    # `summary_evidence` is not an ORM attribute, so it has to be passed
    # explicitly: `from_attributes=True` would otherwise leave it at its None
    # default and the field would silently never be populated.
    return summary_schemas.SummaryOut(
        id=item.id,
        meeting_id=item.meeting_id,
        version=item.version,
        status=item.status,
        overview=item.overview,
        key_topics=item.key_topics,
        conclusions=item.conclusions,
        open_questions=item.open_questions,
        model_name=item.model_name,
        raw_model_output=item.raw_model_output,
        summary_evidence=project_summary_evidence(item.raw_model_output),
    )


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
