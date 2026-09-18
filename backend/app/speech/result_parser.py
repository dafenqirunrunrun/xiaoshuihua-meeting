"""腾讯云实时语音识别结果输出工具。"""

from __future__ import annotations

import json
from typing import Any


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
) -> None:
    _debug_raw(debug, response)
    voice_id = response.get("voice_id", "")
    for item in _sentence_items(response.get("sentences")):
        sentence_type = item.get("sentence_type", 0)
        title = "[ASR FINAL]" if sentence_type == 1 else "[ASR PARTIAL]"
        text = item.get("sentence", "") or item.get("text", "")
        print(title)
        if device_id:
            print(f"device_id={device_id}")
        print(f"voice_id={voice_id}")
        print(f"speaker={item.get('speaker_id', -1)}")
        print(f"sentence_id={item.get('sentence_id', '')}")
        print(f"sentence_type={sentence_type}")
        print(f"start_ms={item.get('start_time', '')}")
        print(f"end_ms={item.get('end_time', '')}")
        print(f"text={text}")


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
