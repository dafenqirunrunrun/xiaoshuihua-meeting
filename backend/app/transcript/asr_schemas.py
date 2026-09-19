"""External ASR Transcript DTO — collaborator-facing ingress format (ASR-0A).

This is **not** our internal Transcript Event Contract. It is the *normalized
ASR output* agreed with the upstream Audio / Tencent ASR producer::

    Audio / Tencent ASR
      -> normalized PARTIAL / FINAL        (collaborator's responsibility)
      -> [THIS DTO]                        external, camelCase, no segmentSeq
      -> app.transcript.asr_adapter        pure field translation + identity derive
      -> TranscriptEvent                   our internal contract
      -> app.transcript.service            existing Transcript Domain (UNCHANGED)

Deliberate differences from the internal contract:

  ============================  =========================  =====================
  aspect                        external (this DTO)        internal
  ============================  =========================  =====================
  field naming                  camelCase                  snake_case
  event discriminator           ``type``                   ``status``
  transcript ordering           **absent**                 ``segment_seq``
  identity                      **absent**                 ``utterance_id`` /
                                                           ``event_id``
  speaker                       ``speakerId: int`` (-1)    ``speaker_id: str``
  ============================  =========================  =====================

The collaborator explicitly does NOT own: ``segmentSeq`` (transcript ordering),
DB ids, the repository, SSE, the frontend, or Summary. Those belong to the
Transcript Domain. See ``docs/architecture.md`` "ASR Boundary".

Validation is **fail-closed** (``extra="forbid"``): an unknown field is contract
drift and must surface loudly at integration time rather than be silently
ignored. Only the camelCase spellings are accepted — the snake_case Python
attribute names below are an internal convenience, not a second wire format.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bound for collaborator-supplied identifiers.
#
# `asrSessionId` is capped at 48 (not 128) ON PURPOSE: the internal
# `utterance_id` is derived as `f"{asrSessionId}:{sentenceId}"` and must fit the
# internal 64-char bound (TranscriptEvent.utterance_id, DB String(64)).
# Worst case = 48 + 1 + len(str(2_147_483_647)) = 48 + 1 + 10 = 59 <= 64.
# Rejecting an over-long session id at the boundary keeps the derivation exact
# and injective — far better than silently hashing or truncating an identity.
_MAX_SESSION_ID = 48
_MAX_SENTENCE_ID = 2_147_483_647  # 32-bit signed; comfortably within SQLite int


class AsrTranscriptEvent(BaseModel):
    """One normalized ASR event exactly as the collaborator emits it.

    PARTIAL (speaker may still be unknown, ``speakerId = -1``)::

        {"type": "transcript.partial", "meetingId": "meeting_001",
         "asrSessionId": "sess-1", "sentenceId": 2, "speakerId": -1,
         "startMs": 950, "endMs": 6480, "text": "我们今天", "provider": "tencent"}

    FINAL (speaker resolved, same ``sentenceId`` as the PARTIALs it replaces)::

        {"type": "transcript.final", "meetingId": "meeting_001",
         "asrSessionId": "sess-1", "sentenceId": 2, "speakerId": 0,
         "startMs": 950, "endMs": 7900, "text": "我们今天讨论会议助手。",
         "provider": "tencent"}

    Note there is NO ``segmentSeq`` here, by contract (ASR-0A §8): sentence
    numbering is scoped to one ASR session and is NOT the meeting-wide
    transcript ordering. The ingress allocates the ordering.
    """

    model_config = ConfigDict(extra="forbid")

    # --- event discriminator -------------------------------------------------
    type: Literal["transcript.partial", "transcript.final"]

    # --- meeting / session / sentence identity -------------------------------
    meetingId: str = Field(min_length=1, max_length=64)
    asrSessionId: str = Field(min_length=1, max_length=_MAX_SESSION_ID)
    sentenceId: int = Field(ge=0, le=_MAX_SENTENCE_ID)

    # --- speaker: INT, -1 means "speaker not yet identified" -----------------
    # Allowed, never an error: an early PARTIAL legitimately has no speaker yet.
    speakerId: int = Field(ge=-1, le=1_000_000)

    # --- timing (ms, relative to the ASR session timeline) ------------------
    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)

    # --- content -------------------------------------------------------------
    text: str = Field(max_length=4000)

    # --- provider metadata (opaque; never hardcode a vendor downstream) ------
    provider: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_timing(self) -> "AsrTranscriptEvent":
        if self.endMs < self.startMs:
            raise ValueError(
                f"endMs ({self.endMs}) must be >= startMs ({self.startMs})"
            )
        return self

    @property
    def is_final(self) -> bool:
        """True for ``transcript.final``. The ONLY discriminator the ingress uses."""
        return self.type == "transcript.final"


__all__ = ["AsrTranscriptEvent"]
