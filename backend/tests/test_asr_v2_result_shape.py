"""Real /asr/v2 ``result`` shape parsing.

The Tencent ``/asr/v2`` endpoint sends one ``result`` object per message,
with the text in ``voice_text_str`` and the lifecycle in ``slice_type``.
An earlier parser only understood the legacy ``sentences`` /
``sentence_type`` / ``sentence_id`` shape, so every real recognition result
was silently dropped and transcripts were always empty — while the legacy
tests stayed green because they only exercised the legacy shape.

These tests pin the real shape so that regression stays visible.
"""

from __future__ import annotations

from app.speech.result_parser import parse_sentences


def _result(**overrides: object) -> dict:
    payload = {
        "slice_type": 0,
        "index": 0,
        "start_time": 0,
        "end_time": 1000,
        "voice_text_str": "你好",
        "word_size": 0,
        "word_list": [],
        "emotion_type": None,
        "speaker_info": None,
    }
    payload.update(overrides)
    return {"code": 0, "message": "success", "voice_id": "v-1", "result": payload}


def test_starting_slice_is_partial() -> None:
    (sentence,) = parse_sentences(_result(slice_type=0))
    assert sentence.sentence_type == 0
    assert sentence.sentence_id == 0
    assert sentence.text == "你好"


def test_continuing_slice_is_still_partial() -> None:
    (sentence,) = parse_sentences(_result(slice_type=1, end_time=1400))
    assert sentence.sentence_type == 0


def test_ending_slice_is_final() -> None:
    """slice_type 2 must map to sentence_type 1: both the transcript bridge
    and the dispatcher key on ``sentence_type == 1`` to treat a sentence as
    durable FINAL."""
    (sentence,) = parse_sentences(_result(slice_type=2, voice_text_str="你好世界"))
    assert sentence.sentence_type == 1
    assert sentence.text == "你好世界"


def test_index_becomes_sentence_id() -> None:
    (sentence,) = parse_sentences(_result(index=7))
    assert sentence.sentence_id == 7


def test_times_are_read_from_start_and_end_time() -> None:
    (sentence,) = parse_sentences(_result(start_time=56000, end_time=57320))
    assert (sentence.start_ms, sentence.end_ms) == (56000, 57320)


def test_absent_speaker_is_unknown_not_a_synthetic_label() -> None:
    (sentence,) = parse_sentences(_result(speaker_info=None))
    assert sentence.speaker_id == -1


def test_speaker_info_nested_id_is_honoured() -> None:
    (sentence,) = parse_sentences(_result(speaker_info={"speaker_id": 2}))
    assert sentence.speaker_id == 2


def test_unknown_slice_type_is_skipped_not_raised() -> None:
    assert parse_sentences(_result(slice_type=9)) == []


def test_end_before_start_is_skipped() -> None:
    assert parse_sentences(_result(start_time=500, end_time=100)) == []


def test_missing_voice_id_yields_nothing() -> None:
    response = _result()
    response.pop("voice_id")
    assert parse_sentences(response) == []


def test_legacy_sentences_shape_still_parses() -> None:
    """The internal mock producer and the pre-existing suites use this shape;
    the v2 fix must not break them."""
    response = {
        "code": 0,
        "voice_id": "voice-A",
        "sentences": [
            {
                "sentence_id": 0,
                "sentence_type": 0,
                "start_time": 0,
                "end_time": 500,
                "sentence": "你好",
                "speaker_id": -1,
            },
            {
                "sentence_id": 0,
                "sentence_type": 1,
                "start_time": 0,
                "end_time": 900,
                "sentence": "你好世界",
                "speaker_id": 1,
            },
        ],
    }
    partial, final = parse_sentences(response)
    assert (partial.sentence_type, partial.speaker_id) == (0, -1)
    assert (final.sentence_type, final.speaker_id, final.text) == (1, 1, "你好世界")
