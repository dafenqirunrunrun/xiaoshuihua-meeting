"""ASR Transcript Adapter — external collaborator DTO -> internal domain DTO.

This module is **pure translation**. It has no DB session, no repository, no
SSE, no lock, no transaction, no meeting-status logic, and — importantly — it
never allocates ``segment_seq`` (ASR-0A §1/§3). Ordering is a Transcript-Domain
concern owned by the ingress application layer, inside the meeting lock.

What it does:

  1. field translation      camelCase -> snake_case (1:1, no renames)
  2. speaker normalization  ``speakerId: int`` (-1 = unknown) -> internal ``str``
  3. identity derivation    ``utterance_id`` and ``event_id`` (see below)

Identity rules
--------------

``utterance_id`` — determines PARTIAL in-place replacement and the
PARTIAL -> FINAL hand-off on the frontend. A sentence's PARTIALs and its FINAL
MUST share one identity::

    utterance_id = f"{asrSessionId}:{sentenceId}"

This is injective given the DTO bounds (session id <= 48 chars, sentence id is
an int), and <= 64 chars, so it fits ``TranscriptEvent.utterance_id``.

``event_id`` — the internal FINAL idempotency key. Derived deterministically so
that a retried FINAL of the same ASR sentence reuses the same key and is
absorbed by the EXISTING ``UNIQUE(event_id)`` constraint (Case A) without any
schema change::

    FINAL   event_id = "asrf_" + sha256(meetingId|asrSessionId|sentenceId|FINAL)[:32]
    PARTIAL event_id = "asrp_" + sha256(meetingId|asrSessionId|sentenceId|PARTIAL|rev)[:32]

A PARTIAL must NOT reuse the sentence's single FINAL-style id, because one
sentence emits MANY PARTIAL revisions. Its digest therefore mixes in the
revision content (``text`` + timing), giving distinct ids to distinct revisions
while remaining stable for an identical retry. This is safe rather than merely
minimal: ``handle_partial`` never reads ``event_id`` (it is not persisted), so
the only requirement is distinctness + determinism for traceability.

The internal ``meeting_id`` is part of the FINAL digest because
``UNIQUE(event_id)`` is **global**, not per-meeting.

Deliberately NOT done here (see ``docs/architecture.md`` ASR Boundary):

  - ``provider`` / ``asrSessionId`` / ``sentenceId`` are consumed for identity
    derivation but are NOT persisted. The DB has no columns for them and this
    step must not add any (ASR-0A §8). Their values survive only for the
    lifetime of the ingress call.
"""

from __future__ import annotations

import hashlib

from app.transcript import schemas as transcript_schemas
from app.transcript.asr_schemas import AsrTranscriptEvent

# Internal speaker representation for "speaker not yet identified" (PRD §19).
SPEAKER_UNKNOWN = "unknown"

# Internal id namespace prefixes (kept short: length is shared with the digest).
_FINAL_EVENT_PREFIX = "asrf_"
_PARTIAL_EVENT_PREFIX = "asrp_"
_DIGEST_CHARS = 32

# Internal contract bounds — asserted at derivation time, not assumed.
_MAX_INTERNAL_ID = 64


def _digest(*parts: object) -> str:
    """Stable, collision-resistant digest over ``parts`` (hex, truncated)."""
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:_DIGEST_CHARS]


def normalize_speaker_id(speaker_id: int) -> str:
    """Map the external integer speaker to the internal string form.

    ``-1`` means "not yet identified" and is an ORDINARY value — never an error
    (ASR-0A §6). It maps to the internal ``unknown`` sentinel so that no
    ``speaker_-1`` ever reaches persistence or the frontend.
    """
    if speaker_id < 0:
        return SPEAKER_UNKNOWN
    return f"speaker_{speaker_id}"


def derive_utterance_id(asr_session_id: str, sentence_id: int) -> str:
    """Stable identity shared by a sentence's PARTIALs and its FINAL."""
    utterance_id = f"{asr_session_id}:{sentence_id}"
    if len(utterance_id) > _MAX_INTERNAL_ID:
        raise ValueError(
            f"derived utterance_id too long ({len(utterance_id)} > {_MAX_INTERNAL_ID}): "
            f"asrSessionId={asr_session_id!r} sentenceId={sentence_id}"
        )
    return utterance_id


def derive_final_event_id(meeting_id: str, asr_session_id: str, sentence_id: int) -> str:
    """Deterministic FINAL idempotency key for one ASR sentence (Case A reuse)."""
    return _FINAL_EVENT_PREFIX + _digest(
        meeting_id, asr_session_id, sentence_id, "FINAL"
    )


def derive_partial_event_id(
    meeting_id: str,
    asr_session_id: str,
    sentence_id: int,
    *,
    text: str,
    start_ms: int,
    end_ms: int,
) -> str:
    """Deterministic per-REVISION PARTIAL id (must not equal the FINAL key)."""
    return _PARTIAL_EVENT_PREFIX + _digest(
        meeting_id, asr_session_id, sentence_id, "PARTIAL", text, start_ms, end_ms
    )


def to_internal_event(
    external: AsrTranscriptEvent,
    *,
    path_meeting_id: str,
) -> transcript_schemas.TranscriptEvent:
    """Translate one external ASR event into the internal TranscriptEvent.

    ``path_meeting_id`` is passed explicitly (not read from ``external``) so the
    caller's path/payload consistency check stays in the ingress, where the HTTP
    boundary is known. This function performs no such check itself.

    ``segment_seq`` is intentionally left ``None`` for both PARTIAL and FINAL:
    it is NOT the adapter's to allocate. The ingress fills it for FINAL inside
    the meeting lock.
    """
    is_final = external.is_final
    utterance_id = derive_utterance_id(external.asrSessionId, external.sentenceId)

    if is_final:
        event_id = derive_final_event_id(
            path_meeting_id, external.asrSessionId, external.sentenceId
        )
        status = "FINAL"
    else:
        event_id = derive_partial_event_id(
            path_meeting_id,
            external.asrSessionId,
            external.sentenceId,
            text=external.text,
            start_ms=external.startMs,
            end_ms=external.endMs,
        )
        status = "PARTIAL"

    return transcript_schemas.TranscriptEvent(
        event_id=event_id,
        meeting_id=path_meeting_id,
        utterance_id=utterance_id,
        segment_seq=None,  # Transcript-owned; allocated by the ingress
        speaker_id=normalize_speaker_id(external.speakerId),
        text=external.text,
        status=status,  # type: ignore[arg-type]  # Literal narrowed by branch
        start_ms=external.startMs,
        end_ms=external.endMs,
    )


__all__ = [
    "SPEAKER_UNKNOWN",
    "derive_final_event_id",
    "derive_partial_event_id",
    "derive_utterance_id",
    "normalize_speaker_id",
    "to_internal_event",
]
