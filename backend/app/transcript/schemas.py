"""Transcript Event Contract. PRD §15, §16-§20.

This is the most important cross-team data contract.
Both PARTIAL and FINAL events share the same envelope.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TranscriptEvent(BaseModel):
    """A single PARTIAL / FINAL transcript event. PRD §15."""

    event_id: str = Field(min_length=1, max_length=64)
    meeting_id: str = Field(min_length=1, max_length=64)
    utterance_id: str = Field(min_length=1, max_length=64)
    segment_seq: int | None = Field(default=None, ge=1)  # FINAL requires it; PARTIAL may omit
    speaker_id: str = Field(default="unknown", max_length=64)
    text: str = Field(default="", max_length=4000)
    status: Literal["PARTIAL", "FINAL"]
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    emitted_at: int | None = Field(default=None)  # epoch ms


class StreamEndEvent(BaseModel):
    """TRanscript Stream End. PRD §25."""

    event_id: str = Field(min_length=1, max_length=64)
    meeting_id: str = Field(min_length=1, max_length=64)
    event_type: Literal["TRANSCRIPT_STREAM_END"] = "TRANSCRIPT_STREAM_END"
    last_segment_seq: int | None = Field(default=None, ge=1)
    emitted_at: int | None = Field(default=None)


class TranscriptSegmentOut(BaseModel):
    """FINAL transcript segment returned to the frontend. PRD §33."""

    model_config = ConfigDict(from_attributes=True)

    segment_seq: int
    utterance_id: str
    speaker_id: str
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


class TranscriptSnapshot(BaseModel):
    """GET /transcript response: FINAL segments only, ordered by segment_seq. PRD §32/§39, arch §6."""

    meeting_id: str
    segments: list[TranscriptSegmentOut]


class StreamEndOut(BaseModel):
    """POST stream-end response (Step 4 §11)."""

    meeting_id: str
    last_segment_seq: int | None
    transcript_complete: bool
