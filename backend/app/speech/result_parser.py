"""腾讯云实时语音识别结果输出工具。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TencentSentenceResult:
    voice_id: str
    sentence_id: int
    sentence_type: int
    speaker_id: int
    start_ms: int
    end_ms: int
    text: str


def parse_sentences(response: dict[str, Any]) -> list[TencentSentenceResult]:
    """Normalize valid Tencent sentence items and skip malformed ones."""
    voice_id = response.get("voice_id")
    if not isinstance(voice_id, str) or not voice_id:
        if _sentence_items(response.get("sentences")):
            logger.warning("[ASR] malformed sentences skipped: missing voice_id")
        return []

    results: list[TencentSentenceResult] = []
    for index, item in enumerate(_sentence_items(response.get("sentences"))):
        try:
            sentence_id = _required_int(item, "sentence_id")
            sentence_type = _required_int(item, "sentence_type")
            start_ms = _required_int(item, "start_time")
            end_ms = _required_int(item, "end_time")

            if sentence_id < 0:
                raise ValueError("sentence_id must be >= 0")
            if sentence_type not in (0, 1):
                raise ValueError(f"unknown sentence_type={sentence_type}")
            if start_ms < 0 or end_ms < 0:
                raise ValueError("start_time and end_time must be >= 0")
            if end_ms < start_ms:
                raise ValueError("end_time must be >= start_time")

            speaker_value = item.get("speaker_id", -1)
            speaker_id = (
                speaker_value
                if isinstance(speaker_value, int)
                and not isinstance(speaker_value, bool)
                and speaker_value >= 0
                else -1
            )
            sentence_text = item.get("sentence")
            if not isinstance(sentence_text, str) or not sentence_text:
                sentence_text = item.get("text", "")
            text = sentence_text if isinstance(sentence_text, str) else ""

            results.append(
                TencentSentenceResult(
                    voice_id=voice_id,
                    sentence_id=sentence_id,
                    sentence_type=sentence_type,
                    speaker_id=speaker_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                )
            )
        except ValueError as exc:
            logger.warning(
                "[ASR] malformed sentence skipped voice_id=%s index=%d: %s",
                voice_id,
                index,
                exc,
            )
    return results


def _required_int(item: dict[str, Any], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _debug_raw(enabled: bool, response: dict[str, Any]) -> None:
    if enabled:
        print("[ASR DEBUG]")
        print(json.dumps(response, ensure_ascii=False, indent=2))


def print_start(response: dict[str, Any], *, debug: bool = False) -> None:
    _debug_raw(debug, response)
    print(f"[ASR START] voice_id={response.get('voice_id', '')}")
    speaker_context_id = response.get("speaker_context_id")
    if speaker_context_id:
        print(f"speaker_context_id={speaker_context_id}")


def print_sentences(
    response: dict[str, Any], *, debug: bool = False, device_id: str = ""
) -> list[TencentSentenceResult]:
    _debug_raw(debug, response)
    sentences = parse_sentences(response)
    for sentence in sentences:
        title = "[ASR FINAL]" if sentence.sentence_type == 1 else "[ASR PARTIAL]"
        print(title)
        if device_id:
            print(f"device_id={device_id}")
        print(f"voice_id={sentence.voice_id}")
        print(f"speaker={sentence.speaker_id}")
        print(f"sentence_id={sentence.sentence_id}")
        print(f"sentence_type={sentence.sentence_type}")
        print(f"start_ms={sentence.start_ms}")
        print(f"end_ms={sentence.end_ms}")
        print(f"text={sentence.text}")
    return sentences


def print_end(response: dict[str, Any], *, debug: bool = False) -> None:
    _debug_raw(debug, response)
    print("[ASR END]")
    print(f"voice_id={response.get('voice_id', '')}")
    print(f"code={response.get('code', 0)}")
    print(f"message={response.get('message', '')}")


def print_error(response: dict[str, Any], *, debug: bool = False) -> None:
    _debug_raw(debug, response)
    print("[ASR ERROR]")
    print(f"voice_id={response.get('voice_id', '')}")
    print(f"code={response.get('code', -1)}")
    print(f"message={response.get('message', '')}")


def _sentence_items(sentences: Any) -> list[dict[str, Any]]:
    if isinstance(sentences, list):
        return [item for item in sentences if isinstance(item, dict)]
    if not isinstance(sentences, dict):
        return []

    sentence_list = sentences.get("sentence_list")
    if isinstance(sentence_list, list):
        return [item for item in sentence_list if isinstance(item, dict)]

    sentence_keys = {
        "sentence",
        "text",
        "sentence_type",
        "sentence_id",
        "speaker_id",
        "start_time",
        "end_time",
    }
    if sentence_keys.intersection(sentences.keys()):
        return [sentences]

    return []
