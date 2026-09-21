"""Pure Tencent sentence -> frozen external Transcript DTO translation."""

from app.speech.result_parser import TencentSentenceResult
from app.transcript.asr_schemas import AsrTranscriptEvent


def to_asr_transcript_event(
    sentence: TencentSentenceResult,
    *,
    meeting_id: str,
) -> AsrTranscriptEvent:
    if sentence.sentence_type == 0:
        event_type = "transcript.partial"
        speaker_id = -1
    elif sentence.sentence_type == 1:
        event_type = "transcript.final"
        speaker_id = sentence.speaker_id if sentence.speaker_id >= 0 else -1
    else:
        raise ValueError(f"unsupported Tencent sentence_type={sentence.sentence_type}")

    return AsrTranscriptEvent(
        type=event_type,
        meetingId=meeting_id,
        asrSessionId=sentence.voice_id,
        sentenceId=sentence.sentence_id,
        speakerId=speaker_id,
        startMs=sentence.start_ms,
        endMs=sentence.end_ms,
        text=sentence.text,
        provider="tencent",
    )


__all__ = ["to_asr_transcript_event"]
