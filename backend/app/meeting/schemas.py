from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.datetime_utils import ensure_utc


class MeetingCreate(BaseModel):
    """PRD §37: MVP request only needs title + optional description."""

    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class MeetingUpdate(BaseModel):
    """Editable meeting metadata; lifecycle fields are intentionally excluded."""

    title: str = Field(min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("meeting title cannot be blank")
        return title


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    meeting_code: str
    title: str
    description: str | None
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator(
        "started_at",
        "ended_at",
        "completed_at",
        "created_at",
        "updated_at",
        mode="before",
    )
    @classmethod
    def normalize_utc_datetimes(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)


class MeetingListOut(BaseModel):
    """GET /api/meetings —— 列表响应（MVP §Step 8B-6，复用 MeetingOut 语义）。

    不新增 activeMeeting / isResumable 等派生字段：状态 / 时间字段已可推导。
    """

    items: list[MeetingOut]
    total: int
    limit: int
    offset: int
