"""Meeting service.

Step 1 scope: create + get (validate data layer end-to-end).
Full state machine (start/finish/transcript/stream_end) lands in later steps.
"""

import uuid

from app import models
from app.schemas import meeting as meeting_schemas
from sqlalchemy.orm import Session


def create_meeting(db: Session, payload: meeting_schemas.MeetingCreate) -> models.Meeting:
    meeting = models.Meeting(
        id=f"meeting_{uuid.uuid4().hex[:8]}",
        title=payload.title,
        description=payload.description,
        status="CREATED",
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


def get_meeting(db: Session, meeting_id: str) -> models.Meeting | None:
    return db.get(models.Meeting, meeting_id)
