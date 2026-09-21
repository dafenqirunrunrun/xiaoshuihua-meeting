"""Offline PCM inspection and the fixed four-case Tencent ASR A/B test."""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import sys
import threading
import wave
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import websocket

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.audio.pcm import apply_pcm16_gain
from app.speech.result_parser import TencentSentenceResult
from app.speech.tencent_client import (
    TencentAsrConfig,
    TencentRealtimeAsrClient,
    load_env_file,
)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
PCM_CHUNK_BYTES = 1024 * 1024
DEFAULT_SOCKET_TIMEOUT_SECONDS = 90.0
SOURCE_PCM = (
    BACKEND_ROOT
    / "data"
    / "audio_test"
    / "ESP32-001_20260920_170008_270003.pcm"
)
OUTPUT_DIR = (
    BACKEND_ROOT
    / "data"
    / "asr_ab_test"
    / "ESP32-001_20260920_170008_270003"
)
WAV_PATH = SOURCE_PCM.with_suffix(".wav")
GENDER_PREFIX_PATTERN = re.compile(r"^(男|女)[，,。]")


@dataclass(frozen=True)
class Experiment:
    label: str
    engine: str
    gain: float
    speaker_labels: bool


EXPERIMENTS = (
    Experiment("speaker2_gain1", "16k_zh_en_speaker_2.0", 1.0, True),
    Experiment("speaker2_gain4", "16k_zh_en_speaker_2.0", 4.0, True),
    Experiment("speaker2_gain8", "16k_zh_en_speaker_2.0", 8.0, True),
    Experiment("normal2_gain1", "16k_zh_en_2.0", 1.0, False),
)


def pcm_to_wav(
    pcm_path: Path,
    wav_path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
) -> None:
    """Wrap raw PCM bytes in a WAV container without changing any sample."""
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with pcm_path.open("rb") as source, wave.open(str(wav_path), "wb") as output:
        output.setframerate(sample_rate)
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        while chunk := source.read(PCM_CHUNK_BYTES):
            output.writeframesraw(chunk)


def inspect_wav(wav_path: Path) -> dict[str, int | float]:
    with wave.open(str(wav_path), "rb") as audio:
        frame_count = audio.getnframes()
        sample_rate = audio.getframerate()
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "frame_count": frame_count,
        "duration_seconds": frame_count / sample_rate,
    }


def inspect_pcm(pcm_path: Path) -> dict[str, int | float | bool]:
    pcm_bytes = pcm_path.stat().st_size
    if pcm_bytes % SAMPLE_WIDTH:
        raise ValueError(f"PCM byte count must be even: {pcm_bytes}")

    sample_count = 0
    min_sample = 32767
    max_sample = -32768
    peak_absolute = 0
    sum_squares = 0
    clipping_sample_count = 0

    with pcm_path.open("rb") as source:
        while chunk := source.read(PCM_CHUNK_BYTES):
            samples = array.array("h")
            samples.frombytes(chunk)
            if sys.byteorder != "little":
                samples.byteswap()
            for sample in samples:
                sample_count += 1
                min_sample = min(min_sample, sample)
                max_sample = max(max_sample, sample)
                absolute = abs(sample)
                peak_absolute = max(peak_absolute, absolute)
                sum_squares += sample * sample
                if sample in (-32768, 32767):
                    clipping_sample_count += 1

    rms = math.sqrt(sum_squares / sample_count) if sample_count else 0.0
    peak_ratio = peak_absolute / 32767 if peak_absolute else 0.0
    clipping_ratio = (
        clipping_sample_count / sample_count if sample_count else 0.0
    )
    rms_dbfs = 20 * math.log10(rms / 32767) if rms else float("-inf")
    peak_dbfs = (
        20 * math.log10(peak_absolute / 32767)
        if peak_absolute
        else float("-inf")
    )
    return {
        "pcm_bytes": pcm_bytes,
        "sample_count": sample_count,
        "duration_seconds": pcm_bytes / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH),
        "min_sample": min_sample if sample_count else 0,
        "max_sample": max_sample if sample_count else 0,
        "peak_absolute_amplitude": peak_absolute,
        "rms": rms,
        "peak_over_32767": peak_ratio,
        "clipping": clipping_sample_count > 0,
        "clipping_sample_count": clipping_sample_count,
        "clipping_ratio": clipping_ratio,
        "rms_dbfs": rms_dbfs,
        "peak_dbfs": peak_dbfs,
    }


def validate_wav(pcm_stats: dict[str, Any], wav_stats: dict[str, Any]) -> None:
    expected = {
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width": SAMPLE_WIDTH,
    }
    for field, value in expected.items():
        if wav_stats[field] != value:
            raise ValueError(
                f"WAV {field}={wav_stats[field]}, expected {value}"
            )
    duration_delta = abs(
        wav_stats["duration_seconds"] - pcm_stats["duration_seconds"]
    )
    if duration_delta > 1 / SAMPLE_RATE:
        raise ValueError(f"WAV/PCM duration mismatch: {duration_delta:.9f}s")


def prepare_gain_copy(source: Path, target: Path, gain: float) -> None:
    if target.exists() and target.stat().st_size == source.stat().st_size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with source.open("rb") as input_file, temporary.open("wb") as output_file:
        while chunk := input_file.read(PCM_CHUNK_BYTES):
            output_file.write(apply_pcm16_gain(chunk, gain))
    temporary.replace(target)


def gender_prefix_sentences(
    sentences: list[TencentSentenceResult],
) -> list[dict[str, Any]]:
    return [
        asdict(sentence)
        for sentence in sentences
        if GENDER_PREFIX_PATTERN.match(sentence.text)
    ]


def render_txt(
    sentences: list[dict[str, Any]], *, speaker_labels: bool
) -> str:
    blocks: list[str] = []
    for sentence in sentences:
        time_range = (
            f"{sentence['start_ms'] / 1000:.2f}s - "
            f"{sentence['end_ms'] / 1000:.2f}s"
        )
        speaker_id = sentence["speaker_id"]
        if speaker_labels and speaker_id >= 0:
            heading = f"[speaker_{speaker_id}] {time_range}"
        else:
            heading = time_range
        blocks.append(f"{heading}\n{sentence['text']}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def load_completed_result(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return result if result.get("completed") is True else None


def run_experiment(
    experiment: Experiment,
    source_pcm: Path,
    test_pcm: Path,
    output_dir: Path,
) -> dict[str, Any]:
    json_path = output_dir / f"{experiment.label}.json"
    txt_path = output_dir / f"{experiment.label}.txt"
    completed = load_completed_result(json_path)
    if completed is not None:
        if not txt_path.exists():
            txt_path.write_text(
                render_txt(
                    completed.get("sentences", []),
                    speaker_labels=experiment.speaker_labels,
                ),
                encoding="utf-8",
            )
        print(f"[SKIP] {experiment.label}: completed result already exists")
        return completed

    final_sentences: list[TencentSentenceResult] = []
    callback_lock = threading.Lock()

    def collect_final(sentence: TencentSentenceResult) -> None:
        if sentence.sentence_type != 1:
            return
        with callback_lock:
            final_sentences.append(sentence)

    base_config = TencentAsrConfig.from_env()
    config = replace(base_config, engine=experiment.engine)
    client = TencentRealtimeAsrClient(
        config=config,
        device_id="PCM-A-B-TEST",
        on_sentence=collect_final,
    )
    print(
        f"[RUN] {experiment.label} engine={experiment.engine} "
        f"gain={experiment.gain:g}"
    )
    client.recognize_file(test_pcm)

    sentence_dicts = [asdict(sentence) for sentence in final_sentences]
    unique_speaker_ids = sorted(
        {sentence.speaker_id for sentence in final_sentences if sentence.speaker_id >= 0}
    )
    anomalies = gender_prefix_sentences(final_sentences)
    result = {
        "completed": True,
        "engine": experiment.engine,
        "gain": experiment.gain,
        "source_pcm": str(source_pcm.resolve()),
        "test_pcm": str(test_pcm.resolve()),
        "sentences": sentence_dicts,
        "final_sentence_count": len(sentence_dicts),
        "unique_speaker_ids": unique_speaker_ids,
        "gender_prefix_count": len(anomalies),
        "gender_prefix_sentences": anomalies,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    txt_path.write_text(
        render_txt(sentence_dicts, speaker_labels=experiment.speaker_labels),
        encoding="utf-8",
    )
    (output_dir / f"{experiment.label}.error.txt").unlink(missing_ok=True)
    return result


def format_metric(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.9f}"
    return str(value)


def write_summary(
    path: Path,
    source_pcm: Path,
    pcm_stats: dict[str, Any],
    results: dict[str, dict[str, Any]],
    errors: dict[str, str],
) -> None:
    lines = [
        "Source:",
        str(source_pcm.resolve()),
        "",
        "Audio:",
    ]
    for field in (
        "pcm_bytes",
        "duration_seconds",
        "min_sample",
        "max_sample",
        "peak_absolute_amplitude",
        "rms",
        "peak_over_32767",
        "clipping",
        "clipping_sample_count",
        "clipping_ratio",
        "rms_dbfs",
        "peak_dbfs",
    ):
        lines.append(f"{field} = {format_metric(pcm_stats[field])}")

    for experiment in EXPERIMENTS:
        lines.extend(["", "--------------------------------", ""])
        lines.append(
            f"{experiment.engine} gain={experiment.gain:g} "
            f"({experiment.label})"
        )
        result = results.get(experiment.label)
        if result is None:
            lines.append(f"ERROR: {errors.get(experiment.label, 'not run')}")
            continue
        speakers = result["unique_speaker_ids"]
        lines.extend(
            [
                f"FINAL sentences: {result['final_sentence_count']}",
                "Unique speakers: " + (",".join(map(str, speakers)) or "none"),
                f"Gender-prefix anomalies: {result['gender_prefix_count']}",
            ]
        )
        for sentence in result["gender_prefix_sentences"]:
            lines.append(
                f"  sentence_id={sentence['sentence_id']} text={sentence['text']}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE_PCM)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--wav", type=Path, default=WAV_PATH)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create/validate WAV and print PCM statistics without calling Tencent.",
    )
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=DEFAULT_SOCKET_TIMEOUT_SECONDS,
        help="Socket operation timeout; prevents a dead Tencent connection hanging forever.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_pcm = args.source.resolve()
    output_dir = args.output_dir.resolve()
    wav_path = args.wav.resolve()
    if not source_pcm.is_file():
        raise FileNotFoundError(source_pcm)

    pcm_stats = inspect_pcm(source_pcm)
    pcm_to_wav(
        source_pcm,
        wav_path,
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        sample_width=SAMPLE_WIDTH,
    )
    wav_stats = inspect_wav(wav_path)
    validate_wav(pcm_stats, wav_stats)

    print(f"WAV={wav_path}")
    print("WAV parameters=" + json.dumps(wav_stats, ensure_ascii=False))
    print("PCM statistics=" + json.dumps(pcm_stats, ensure_ascii=False))
    if args.prepare_only:
        return 0

    load_env_file(BACKEND_ROOT / ".env")
    websocket.setdefaulttimeout(args.socket_timeout)
    temp_dir = output_dir / "temp"
    gain_files: dict[float, Path] = {}
    for gain in sorted({experiment.gain for experiment in EXPERIMENTS}):
        gain_path = temp_dir / f"gain{gain:g}.pcm"
        prepare_gain_copy(source_pcm, gain_path, gain)
        gain_files[gain] = gain_path

    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for experiment in EXPERIMENTS:
        try:
            results[experiment.label] = run_experiment(
                experiment,
                source_pcm,
                gain_files[experiment.gain],
                output_dir,
            )
        except Exception as exc:
            errors[experiment.label] = f"{type(exc).__name__}: {exc}"
            error_path = output_dir / f"{experiment.label}.error.txt"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(errors[experiment.label] + "\n", encoding="utf-8")
            print(f"[ERROR] {experiment.label}: {errors[experiment.label]}")

    write_summary(
        output_dir / "summary.txt",
        source_pcm,
        pcm_stats,
        results,
        errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
