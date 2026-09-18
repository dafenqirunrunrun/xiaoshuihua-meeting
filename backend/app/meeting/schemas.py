from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MeetingCreate(BaseModel):
    """PRD §37: MVP request only needs title + optional description."""

    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MeetingListOut(BaseModel):
    """GET /api/meetings —— 列表响应（MVP §Step 8B-6，复用 MeetingOut 语义）。

    不新增 activeMeeting / isResumable 等派生字段：状态 / 时间字段已可推导。
    """

    items: list[MeetingOut]
    total: int
    limit: int
    offset: int
