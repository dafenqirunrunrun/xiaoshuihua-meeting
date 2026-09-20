"""Meeting state machine and lifecycle.

States (PRD §11): CREATED → IN_PROGRESS → FINALIZING → COMPLETED.
This step implements the first two business transitions:
  CREATED --start--> IN_PROGRESS
  IN_PROGRESS --finish--> FINALIZING
FINALIZING → COMPLETED is done by the Finalizer / Summary pipeline (later step).

Controller contract (from Step 2 spec):
  start:
    CREATED → IN_PROGRESS (record started_at, idempotent)
    already IN_PROGRESS → return current meeting, do NOT touch started_at
    FINALIZING / COMPLETED → 409
    missing meeting → 404
  finish:
    IN_PROGRESS → FINALIZING (record ended_at, idempotent)
    already FINALIZING → return current meeting, do NOT touch ended_at
    already COMPLETED → do NOT re-enter FINALIZING (idempotent: return current)
    CREATED → 409
    missing meeting → 404
"""

import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.meeting_model import Meeting
from app.common.exceptions import InvalidMeetingStateTransition, MeetingNotFoundError
from app.meeting import schemas as meeting_schemas
from app.realtime import sse_service  # publish AFTER db.commit() only

# 合法 Meeting 状态（PRD §11，四态）。列表 status filter 只接受这些值。
VALID_MEETING_STATUSES = ("CREATED", "IN_PROGRESS", "FINALIZING", "COMPLETED")

# 列表默认 / 上限（Step 8B-6 §3）：一次不返回无限历史。
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Concurrency model ---------------------------------------------------
# MVP: single process, single uvicorn worker, SQLite (which serializes writes
# application-wide and has no real row locks). An in-process RLock makes
# concurrent start/finish on the same meeting deterministic and free of
# lost-update / illegal-transition races within this process.
#
# PostgreSQL / company data platform hardening (not done now, noted for later):
#   - Replace this RLock with row-level locking: `SELECT ... FOR UPDATE` (or
#     FOR NO KEY UPDATE) on the meeting row inside the same transaction that
#     performs the guarded state check + UPDATE. That removes the need for the
#     app-level lock and works across multiple processes/instances.
#   - Do NOT rely on last-write-wins `meeting.status = x`; always re-read under
#     the row lock and re-validate the current state before mutating.
#   - Keep the unique constraints (event_id, meeting+seq) for idempotency.
_MEETING_LOCK = threading.RLock()


def create_meeting(db: Session, payload: meeting_schemas.MeetingCreate) -> Meeting:
    meeting = Meeting(
        id=f"meeting_{uuid.uuid4().hex[:8]}",
        title=payload.title,
        description=payload.description,
        status="CREATED",
    )
    db.add(meeting)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    db.refresh(meeting)
    return meeting


def get_meeting(db: Session, meeting_id: str) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    return meeting


def list_meetings(
    db: Session,
    *,
    status: str | None = None,
    limit: int = LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[Meeting], int]:
    """Meeting 列表（Step 8B-6，Meeting Discovery）。

    - 按 `updated_at DESC, created_at DESC` 稳定排序（最近更新优先）。
    - 可选 status filter（四态之一），由 router 负责 422 校验（无效值不达这里）。
    - MVP 无用户体系：返回当前业务库全部会议，不做 owner 隔离
      （后续接公司统一身份再扩展 user_id / tenant_id，见 architecture.md）。
    - limit 由 router 收敛到 [1, LIST_MAX_LIMIT]（默认 LIST_DEFAULT_LIMIT）。
    """
    q = select(Meeting)
    if status is not None:
        q = q.where(Meeting.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = list(
        db.scalars(
            q.order_by(Meeting.updated_at.desc(), Meeting.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    return rows, total or 0


def start_meeting(db: Session, meeting_id: str) -> Meeting:
    with _MEETING_LOCK:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "IN_PROGRESS":
            return meeting  # idempotent: keep original started_at

        if meeting.status != "CREATED":
            # FINALIZING / COMPLETED (or anything else) cannot start
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot start (only CREATED)"
            )

        meeting.status = "IN_PROGRESS"
        if meeting.started_at is None:
            meeting.started_at = _now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(meeting)
        # Commit-before-publish (Step 5 Part 6): only broadcast on the real
        # transition (the idempotent branch above returned early). Non-blocking.
        sse_service.publish_meeting_status(meeting)
        return meeting


def finish_meeting(db: Session, meeting_id: str) -> Meeting:
    with _MEETING_LOCK:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "FINALIZING":
            return meeting  # idempotent: keep original ended_at
        if meeting.status == "COMPLETED":
            return meeting  # already finalized, never re-enter FINALIZING

        if meeting.status != "IN_PROGRESS":
            # CREATED or anything unexpected
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot finish (need IN_PROGRESS)"
            )

        meeting.status = "FINALIZING"
        if meeting.ended_at is None:
            meeting.ended_at = _now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(meeting)
        # Commit-before-publish (Step 5 Part 6). Real transition only.
        sse_service.publish_meeting_status(meeting)
        return meeting


def complete_after_summary(db: Session, meeting_id: str) -> Meeting:
    """FINALIZING → COMPLETED — THE ONLY Meeting-completion path (Step 7B §8).

    Called exclusively by the Finalizer AFTER the Summary has reached a
    terminal state (COMPLETED or FAILED). It is idempotent: an already
    COMPLETED meeting returns unchanged (no state regression, no re-touch of
    completed_at). CREATED / IN_PROGRESS are illegal transitions (409) — a
    meeting cannot be completed before its transcript is frozen + summary
    terminal.

    Precondition (checked by the caller): summary terminal. This method does
    not verify the summary itself — the Finalizer owns that gate.
    """
    with _MEETING_LOCK:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "COMPLETED":
            return meeting  # idempotent no-op

        if meeting.status != "FINALIZING":
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot be completed "
                f"(must be FINALIZING; only the Finalizer transitions to COMPLETED)"
            )

        meeting.status = "COMPLETED"
        if meeting.completed_at is None:
            meeting.completed_at = _now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        db.refresh(meeting)
        # Commit-before-publish (Step 5 Part 6 / Step 7B §11).
        sse_service.publish_meeting_status(meeting)
        return meeting


