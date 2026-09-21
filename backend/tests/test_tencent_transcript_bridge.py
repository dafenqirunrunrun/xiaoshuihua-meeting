from __future__ import annotations

import json

from sqlalchemy import func, select

from app.db import SessionLocal
from app.meeting.meeting_model import Meeting
from app.speech.result_parser import TencentSentenceResult, parse_sentences
from app.speech.tencent_client import _AsrReceiver
from app.speech.transcript_bridge import to_asr_transcript_event
from app.transcript import asr_adapter, asr_service
from app.transcript.segment_model import TranscriptSegment


def _sentence(
    *,
    voice_id: str = "voice-A",
    sentence_id: int = 1,
    sentence_type: int = 0,
    speaker_id: int = 0,
    start_ms: int = 100,
    end_ms: int = 300,
    text: str = "今天",
) -> TencentSentenceResult:
    return TencentSentenceResult(
        voice_id=voice_id,
        sentence_id=sentence_id,
        sentence_type=sentence_type,
        speaker_id=speaker_id,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
    )


def _raw_sentence(**over) -> dict:
    item = {
        "sentence_id": 1,
        "sentence_type": 0,
        "speaker_id": 0,
        "start_time": 100,
        "end_time": 300,
        "sentence": "今天",
    }
    item.update(over)
    return item


class _FakeTencentSocket:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = iter(responses)

    def recv(self) -> str:
        return json.dumps(next(self._responses), ensure_ascii=False)


def _run_receiver(
    sentence_responses: list[dict], callback
) -> _AsrReceiver:
    responses = [
        {"code": 0, "voice_id": "voice-A", "sentences": sentences}
        for sentences in sentence_responses
    ]
    responses.append({"code": 0, "voice_id": "voice-A", "final": 1})
    receiver = _AsrReceiver(
        _FakeTencentSocket(responses),
        "voice-A",
        False,
        on_sentence=callback,
    )
    receiver.run()
    return receiver


def test_partial_and_final_normalization() -> None:
    partial = to_asr_transcript_event(
        _sentence(sentence_type=0, speaker_id=0), meeting_id="meeting-1"
    )
    final = to_asr_transcript_event(
        _sentence(sentence_type=1, speaker_id=1), meeting_id="meeting-1"
    )

    assert partial.type == "transcript.partial"
    assert partial.speakerId == -1
    assert final.type == "transcript.final"
    assert final.speakerId == 1
    assert partial.asrSessionId == final.asrSessionId == "voice-A"
    assert partial.sentenceId == final.sentenceId == 1

    partial_internal = asr_adapter.to_internal_event(
        partial, path_meeting_id="meeting-1"
    )
    final_internal = asr_adapter.to_internal_event(
        final, path_meeting_id="meeting-1"
    )
    assert partial_internal.utterance_id == final_internal.utterance_id


def test_reconnect_voice_ids_produce_distinct_utterance_identity() -> None:
    first = to_asr_transcript_event(
        _sentence(voice_id="voice-A", sentence_type=1), meeting_id="meeting-1"
    )
    second = to_asr_transcript_event(
        _sentence(voice_id="voice-B", sentence_type=1), meeting_id="meeting-1"
    )

    first_internal = asr_adapter.to_internal_event(
        first, path_meeting_id="meeting-1"
    )
    second_internal = asr_adapter.to_internal_event(
        second, path_meeting_id="meeting-1"
    )
    assert first_internal.utterance_id == "voice-A:1"
    assert second_internal.utterance_id == "voice-B:1"


def test_repeated_and_changed_partial_revisions() -> None:
    emitted: list[TencentSentenceResult] = []
    receiver = _run_receiver(
        [
            [_raw_sentence(sentence="今天")],
            [_raw_sentence(sentence="今天")],
            [_raw_sentence(sentence="今天我们")],
        ],
        emitted.append,
    )

    assert receiver.error is None
    assert [item.text for item in emitted] == ["今天", "今天我们"]


def test_receiver_keeps_one_voice_id_for_the_whole_session() -> None:
    emitted: list[TencentSentenceResult] = []
    socket = _FakeTencentSocket(
        [
            {
                "code": 0,
                "voice_id": "unexpected-response-id",
                "sentences": [_raw_sentence()],
            },
            {"code": 0, "final": 1},
        ]
    )
    receiver = _AsrReceiver(
        socket, "voice-A", False, on_sentence=emitted.append
    )

    receiver.run()

    assert receiver.error is None
    assert [item.voice_id for item in emitted] == ["voice-A"]


def test_duplicate_final_and_partial_after_final_are_ignored() -> None:
    emitted: list[TencentSentenceResult] = []
    final = _raw_sentence(sentence_type=1, speaker_id=1, sentence="完成")
    receiver = _run_receiver(
        [[final], [final], [_raw_sentence(sentence="迟到的 partial")]],
        emitted.append,
    )

    assert receiver.error is None
    assert len(emitted) == 1
    assert emitted[0].sentence_type == 1


def test_changed_final_is_emitted_for_domain_conflict_handling() -> None:
    emitted: list[TencentSentenceResult] = []
    receiver = _run_receiver(
        [
            [_raw_sentence(sentence_type=1, sentence="版本一")],
            [_raw_sentence(sentence_type=1, sentence="版本二")],
        ],
        emitted.append,
    )

    assert receiver.error is None
    assert [item.text for item in emitted] == ["版本一", "版本二"]


def test_malformed_sentence_is_skipped_and_receiver_continues() -> None:
    emitted: list[TencentSentenceResult] = []
    receiver = _run_receiver(
        [
            [
                _raw_sentence(sentence_id=None),
                _raw_sentence(start_time=500, end_time=100),
                _raw_sentence(sentence_type=9),
            ],
            [_raw_sentence(sentence_id=2, sentence="后续有效")],
        ],
        emitted.append,
    )

    assert receiver.error is None
    assert [(item.sentence_id, item.text) for item in emitted] == [
        (2, "后续有效")
    ]


def test_parser_uses_text_fallback_and_unknown_final_speaker() -> None:
    parsed = parse_sentences(
        {
            "voice_id": "voice-A",
            "sentences": [
                _raw_sentence(
                    sentence_type=1,
                    speaker_id=-3,
                    sentence="",
                    text="fallback",
                )
            ],
        }
    )
    assert len(parsed) == 1
    assert parsed[0].text == "fallback"
    assert parsed[0].speaker_id == -1


def test_callback_exception_does_not_stop_receiver() -> None:
    callback_sentence_ids: list[int] = []

    def failing_callback(sentence: TencentSentenceResult) -> None:
        callback_sentence_ids.append(sentence.sentence_id)
        raise RuntimeError("downstream failed")

    receiver = _run_receiver(
        [
            [_raw_sentence(sentence_id=1)],
            [_raw_sentence(sentence_id=2)],
        ],
        failing_callback,
    )

    assert receiver.error is None
    assert callback_sentence_ids == [1, 2]


def test_bridge_ingests_through_existing_asr_service() -> None:
    meeting_id = "meeting-tencent-bridge"
    db = SessionLocal()
    try:
        db.add(Meeting(id=meeting_id, title="Tencent bridge", status="IN_PROGRESS"))
        db.commit()

        partial = to_asr_transcript_event(
            _sentence(sentence_type=0, speaker_id=1, text="识别中"),
            meeting_id=meeting_id,
        )
        final = to_asr_transcript_event(
            _sentence(sentence_type=1, speaker_id=0, text="识别完成"),
            meeting_id=meeting_id,
        )

        asr_service.ingest_asr_event(db, meeting_id, partial)
        assert db.scalar(select(func.count()).select_from(TranscriptSegment)) == 0

        asr_service.ingest_asr_event(db, meeting_id, final)
        segments = list(db.scalars(select(TranscriptSegment)))
        assert len(segments) == 1
        assert segments[0].speaker_id == "speaker_0"
        assert segments[0].utterance_id == "voice-A:1"

        asr_service.ingest_asr_event(db, meeting_id, final)
        assert db.scalar(select(func.count()).select_from(TranscriptSegment)) == 1
    finally:
        db.close()
