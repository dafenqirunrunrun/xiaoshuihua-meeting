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
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.device.meeting_binding import (
    bind_device_to_meeting,
    device_meeting_bindings,
)
from app.meeting.meeting_model import Meeting
from app.meeting.id_generator import SnowflakeGenerator, generate_meeting_code
from app.common.exceptions import InvalidMeetingStateTransition, MeetingNotFoundError
from app.meeting import schemas as meeting_schemas
from app.realtime import sse_service  # publish AFTER db.commit() only

# 合法 Meeting 状态（PRD §11，四态）。列表 status filter 只接受这些值。
VALID_MEETING_STATUSES = ("CREATED", "IN_PROGRESS", "FINALIZING", "COMPLETED")

# 列表默认 / 上限（Step 8B-6 §3）：一次不返回无限历史。
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100
CREATE_ID_MAX_ATTEMPTS = 8

_meeting_id_generator = SnowflakeGenerator(settings.meeting_id_worker_id)


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
    for attempt in range(CREATE_ID_MAX_ATTEMPTS):
        meeting = Meeting(
            id=_meeting_id_generator.next_id(),
            meeting_code=generate_meeting_code(),
            title=payload.title,
            description=payload.description,
            status="CREATED",
        )
        db.add(meeting)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if attempt + 1 == CREATE_ID_MAX_ATTEMPTS:
                raise
            continue
        db.refresh(meeting)
        return meeting

    raise RuntimeError("meeting identifier allocation exhausted")


def get_meeting(db: Session, meeting_id: str) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    return meeting


def list_meetings(
    db: Session,
    *,
    status: str | None = None,
    q: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    sort: str = "updated_desc",
    limit: int = LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[Meeting], int]:
    """Meeting 列表（Step 8B-6，Meeting Discovery）。

    - 默认按 `updated_at DESC, created_at DESC` 稳定排序（最近更新优先）。
    - 可选状态、标题/会议号/内部 ID 关键词和创建日期范围过滤。
    - MVP 无用户体系：返回当前业务库全部会议，不做 owner 隔离
      （后续接公司统一身份再扩展 user_id / tenant_id，见 architecture.md）。
    - limit 由 router 收敛到 [1, LIST_MAX_LIMIT]（默认 LIST_DEFAULT_LIMIT）。
    """
    keyword = (q or "").strip()
    stmt = select(Meeting)
    if status is not None:
        stmt = stmt.where(Meeting.status == status)
    if keyword:
        lowered = keyword.lower()
        stmt = stmt.where(
            or_(
                func.lower(Meeting.title).contains(lowered, autoescape=True),
                Meeting.meeting_code.contains(keyword, autoescape=True),
                func.lower(Meeting.id).contains(lowered, autoescape=True),
            )
        )
    if created_from is not None:
        stmt = stmt.where(func.date(Meeting.created_at) >= created_from.isoformat())
    if created_to is not None:
        stmt = stmt.where(func.date(Meeting.created_at) <= created_to.isoformat())
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))

    if sort == "created_desc":
        order_by = (Meeting.created_at.desc(), Meeting.id.desc())
    elif sort == "created_asc":
        order_by = (Meeting.created_at.asc(), Meeting.id.asc())
    else:
        order_by = (
            Meeting.updated_at.desc(),
            Meeting.created_at.desc(),
            Meeting.id.desc(),
        )
    rows = list(
        db.scalars(
            stmt.order_by(*order_by)
            .offset(offset)
            .limit(limit)
        )
    )
    return rows, total or 0


def update_meeting(
    db: Session,
    meeting_id: str,
    payload: meeting_schemas.MeetingUpdate,
) -> Meeting:
    """Rename a meeting without changing its lifecycle state."""
    with _MEETING_LOCK:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        title = payload.title.strip()
        if not title:
            raise ValueError("meeting title cannot be blank")
        meeting.title = title
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(meeting)
        sse_service.publish_meeting_status(meeting)
        return meeting


def delete_meeting(db: Session, meeting_id: str) -> None:
    """Delete a non-active meeting and every durable record it owns.

    SQLite foreign-key cascades are not assumed to be enabled in every MVP
    environment, so dependent rows are removed explicitly in one transaction.
    Active/finalizing meetings are protected from races with audio and agent
    workers; users must finish them before deletion.
    """
    from app.agent.summary_model import MeetingSummary
    from app.meeting_state.model import MeetingState
    from app.meeting_state.snapshot_model import (
        MeetingMemorySnapshot,
        MeetingStateHealth,
    )
    from app.transcript.segment_model import TranscriptSegment
    from app.transcript.stream_model import TranscriptStream

    with _MEETING_LOCK:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)
        if meeting.status not in {"CREATED", "COMPLETED"}:
            raise InvalidMeetingStateTransition(
                f"meeting {meeting_id} status {meeting.status} cannot be deleted "
                "(only CREATED or COMPLETED meetings can be deleted)"
            )

        dependent_models = (
            MeetingSummary,
            MeetingMemorySnapshot,
            MeetingStateHealth,
            MeetingState,
            TranscriptSegment,
            TranscriptStream,
        )
        try:
            for model in dependent_models:
                db.execute(delete(model).where(model.meeting_id == meeting_id))
            db.delete(meeting)
            db.commit()
        except Exception:
            db.rollback()
            raise

        # Clear process-local references only after the durable delete commits.
        from app.meeting_state.scheduler import meeting_state_scheduler

        meeting_state_scheduler.cancel_pending(meeting_id)
        device_meeting_bindings.unbind(settings.shuihua_audio_device_id, meeting_id)


def start_meeting(db: Session, meeting_id: str) -> Meeting:
    with _MEETING_LOCK:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(meeting_id)

        if meeting.status == "IN_PROGRESS":
            bind_device_to_meeting(
                db, settings.shuihua_audio_device_id, meeting_id
            )
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
            # Flush the state transition first, then establish the process-local
            # binding before commit. A binding conflict therefore rolls the
            # meeting back to CREATED, and a commit failure cannot leave the
            # device pointing at a meeting that never started successfully.
            db.flush()
            bind_device_to_meeting(
                db, settings.shuihua_audio_device_id, meeting_id
            )
            db.commit()
        except Exception:
            db.rollback()
            device_meeting_bindings.unbind(
                settings.shuihua_audio_device_id, meeting_id
            )
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
        # Stage 4 deliberately keeps the stale mapping until the next bind or
        # explicit cleanup. get_active_meeting() stops resolving it immediately
        # because FINALIZING is not active. The future hardware STOP flow must:
        # stop new PCM -> drain PCM -> Tencent end_stream -> trailing FINAL ->
        # then unbind, so the final sentence is never cut off or misattributed.
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


