"""腾讯云实时语音识别 V2 WebSocket 最小客户端。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import websocket

from app.config import settings
from app.speech import result_parser
from app.speech.result_parser import TencentSentenceResult

DEFAULT_ENGINE = "16k_zh_en_2.0"
SLICE_SIZE = 6400
SLICE_INTERVAL = 0.2
RECEIVE_END_TIMEOUT = 30.0
ASR_HOST = "asr.cloud.tencent.com"
ASR_PATH_TEMPLATE = "/asr/v2/{appid}"
SentenceCallback = Callable[[TencentSentenceResult], None]

logger = logging.getLogger(__name__)


class TencentAsrConfigError(RuntimeError):
    """腾讯云实时语音识别配置错误。"""


class TencentAsrServiceError(RuntimeError):
    """腾讯云实时语音识别服务端错误。"""

    def __init__(self, *, code: int, message: str, voice_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.voice_id = voice_id


@dataclass(frozen=True)
class TencentAsrConfig:
    app_id: str
    secret_id: str
    secret_key: str
    engine: str = DEFAULT_ENGINE
    debug: bool = False

    @classmethod
    def from_env(cls) -> "TencentAsrConfig":
        app_id = _read_required_env("TENCENT_ASR_APP_ID", settings.tencent_asr_app_id)
        secret_id = _read_required_env("TENCENT_ASR_SECRET_ID", settings.tencent_asr_secret_id)
        secret_key = _read_required_env("TENCENT_ASR_SECRET_KEY", settings.tencent_asr_secret_key)
        engine = (
            os.getenv("TENCENT_ASR_ENGINE", settings.tencent_asr_engine).strip()
            or DEFAULT_ENGINE
        )
        debug_value = os.getenv("TENCENT_ASR_DEBUG")
        debug = (
            debug_value.strip().lower() in {"1", "true", "yes", "on"}
            if debug_value is not None
            else settings.tencent_asr_debug
        )
        return cls(
            app_id=app_id,
            secret_id=secret_id,
            secret_key=secret_key,
            engine=engine,
            debug=debug,
        )


class TencentRealtimeAsrClient:
    """按腾讯云实时语音识别 V2 文档直接实现的 WebSocket 客户端。"""

    def __init__(
        self,
        config: TencentAsrConfig | None = None,
        device_id: str = "",
        on_sentence: SentenceCallback | None = None,
    ) -> None:
        """Create a client.

        ``on_sentence`` is invoked on the Tencent receiver thread, not on the
        FastAPI event-loop thread. Callback failures are isolated from provider
        receive so downstream business code cannot terminate the ASR session.
        """
        self.config = config or TencentAsrConfig.from_env()
        self.device_id = device_id
        self.on_sentence = on_sentence
        self._stream_conn: Any | None = None
        self._stream_receiver: _AsrReceiver | None = None
        self._stream_voice_id = ""

    def start_stream(self) -> None:
        """Open one long-lived Tencent ASR session for incremental PCM feeds."""
        if self._stream_conn is not None:
            raise RuntimeError("腾讯云 ASR session 已经启动")

        voice_id = str(uuid.uuid1())
        ws_url = _build_signed_websocket_url(self.config, voice_id)
        ws_conn = websocket.create_connection(ws_url)
        try:
            first_response = _recv_json(ws_conn)
            first_response["voice_id"] = first_response.get("voice_id") or voice_id
            if first_response.get("code", -1) != 0:
                raise TencentAsrServiceError(
                    code=first_response.get("code", -1),
                    message=first_response.get("message", ""),
                    voice_id=first_response.get("voice_id", voice_id),
                )

            result_parser.print_start(first_response, debug=self.config.debug)
            receiver = _AsrReceiver(
                ws_conn,
                voice_id,
                self.config.debug,
                device_id=self.device_id,
                on_sentence=self.on_sentence,
            )
            receiver.start()
            self._stream_conn = ws_conn
            self._stream_receiver = receiver
            self._stream_voice_id = voice_id
        except Exception:
            ws_conn.close()
            raise

    def feed_audio(self, pcm_data: bytes) -> None:
        if self._stream_conn is None:
            raise RuntimeError("腾讯云 ASR session 尚未启动")
        self._stream_conn.send_binary(pcm_data)

    def end_stream(self) -> None:
        """Finish the current session and release its WebSocket resources."""
        ws_conn = self._stream_conn
        receiver = self._stream_receiver
        self._stream_conn = None
        self._stream_receiver = None
        try:
            if ws_conn is not None:
                ws_conn.send(json.dumps({"type": "end"}, separators=(",", ":")))
            if receiver is not None:
                receiver.join(RECEIVE_END_TIMEOUT)
                if receiver.is_alive():
                    raise TimeoutError(f"{RECEIVE_END_TIMEOUT} 秒内未收到腾讯云 ASR 结束消息")
                if receiver.error:
                    raise receiver.error
        finally:
            if ws_conn is not None:
                ws_conn.close()

    def request_parameter_names(self) -> list[str]:
        names = sorted(_base_request_params(self.config, "voice-id-placeholder").keys())
        return [*names, "signature"]

    def recognize_file(self, pcm_path: Path) -> None:
        if not pcm_path.exists():
            raise FileNotFoundError(
                f"测试音频不存在：{pcm_path}\n"
                "请放置 16kHz / 16bit / mono / signed PCM little-endian 文件。"
            )
        if not pcm_path.is_file():
            raise FileNotFoundError(f"测试音频路径不是文件：{pcm_path}")

        voice_id = str(uuid.uuid1())
        ws_url = _build_signed_websocket_url(self.config, voice_id)
        ws_conn = websocket.create_connection(ws_url)
        receiver = _AsrReceiver(
            ws_conn,
            voice_id,
            self.config.debug,
            device_id=self.device_id,
            on_sentence=self.on_sentence,
        )

        try:
            first_response = _recv_json(ws_conn)
            first_response["voice_id"] = first_response.get("voice_id") or voice_id
            if first_response.get("code", -1) != 0:
                raise TencentAsrServiceError(
                    code=first_response.get("code", -1),
                    message=first_response.get("message", ""),
                    voice_id=first_response.get("voice_id", voice_id),
                )

            result_parser.print_start(first_response, debug=self.config.debug)
            receiver.start()

            with pcm_path.open("rb") as audio_file:
                while True:
                    chunk = audio_file.read(SLICE_SIZE)
                    if not chunk:
                        break
                    ws_conn.send_binary(chunk)
                    time.sleep(SLICE_INTERVAL)

            ws_conn.send(json.dumps({"type": "end"}, separators=(",", ":")))
            receiver.join(RECEIVE_END_TIMEOUT)
            if receiver.is_alive():
                raise TimeoutError(f"{RECEIVE_END_TIMEOUT} 秒内未收到腾讯云 ASR 结束消息")
            if receiver.error:
                raise receiver.error
        finally:
            ws_conn.close()


class _AsrReceiver(threading.Thread):
    """Tencent receive loop; sentence callbacks run on this thread."""

    def __init__(
        self,
        ws_conn: Any,
        voice_id: str,
        debug: bool,
        device_id: str = "",
        on_sentence: SentenceCallback | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self._ws_conn = ws_conn
        self._voice_id = voice_id
        self._debug = debug
        self._device_id = device_id
        self._on_sentence = on_sentence
        self._partial_fingerprints: dict[int, tuple[str, int, int]] = {}
        self._final_fingerprints: dict[int, set[tuple[str, int, int, int]]] = {}
        self.error: Exception | None = None

    def _is_new_revision(self, sentence: TencentSentenceResult) -> bool:
        sentence_id = sentence.sentence_id
        if sentence.sentence_type == 0:
            if sentence_id in self._final_fingerprints:
                return False
            fingerprint = (sentence.text, sentence.start_ms, sentence.end_ms)
            if self._partial_fingerprints.get(sentence_id) == fingerprint:
                return False
            self._partial_fingerprints[sentence_id] = fingerprint
            return True

        fingerprint = (
            sentence.text,
            sentence.start_ms,
            sentence.end_ms,
            sentence.speaker_id,
        )
        seen_finals = self._final_fingerprints.setdefault(sentence_id, set())
        if fingerprint in seen_finals:
            return False
        if seen_finals:
            logger.warning(
                "[ASR] conflicting FINAL revision voice_id=%s sentence_id=%d",
                sentence.voice_id,
                sentence_id,
            )
        seen_finals.add(fingerprint)
        self._partial_fingerprints.pop(sentence_id, None)
        return True

    def _emit_sentences(self, sentences: list[TencentSentenceResult]) -> None:
        if self._on_sentence is None:
            return
        for sentence in sentences:
            if not self._is_new_revision(sentence):
                continue
            try:
                self._on_sentence(sentence)
            except Exception as exc:
                logger.warning(
                    "[ASR] sentence callback failed voice_id=%s sentence_id=%d: %s",
                    sentence.voice_id,
                    sentence.sentence_id,
                    exc,
                )

    def run(self) -> None:
        try:
            while True:
                response = _recv_json(self._ws_conn)
                response["voice_id"] = self._voice_id

                if response.get("code", 0) != 0:
                    result_parser.print_error(response, debug=self._debug)
                    self.error = TencentAsrServiceError(
                        code=response.get("code", -1),
                        message=response.get("message", ""),
                        voice_id=response.get("voice_id", self._voice_id),
                    )
                    return

                # ``result`` is the real /asr/v2 shape (one sentence per message,
                # text in ``voice_text_str``, lifecycle in ``slice_type``);
                # ``sentences`` is the legacy list shape. Both are accepted.
                if response.get("sentences") or response.get("result"):
                    sentences = result_parser.print_sentences(
                        response, debug=self._debug, device_id=self._device_id
                    )
                    self._emit_sentences(sentences)

                if response.get("final", 0) == 1:
                    result_parser.print_end(response, debug=self._debug)
                    return
        except Exception as exc:
            self.error = exc


def load_env_file(path: Path) -> None:
    """加载简单 KEY=VALUE 格式的本地环境文件，不覆盖已有环境变量。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_required_env(name: str, fallback: str = "") -> str:
    value = os.getenv(name, fallback).strip()
    if not value:
        raise TencentAsrConfigError(f"缺少环境变量：{name}")
    return value


def _base_request_params(config: TencentAsrConfig, voice_id: str) -> dict[str, str | int]:
    timestamp = int(time.time())
    return {
        "secretid": config.secret_id,
        "timestamp": timestamp,
        "expired": timestamp + 24 * 60 * 60,
        "nonce": random.randint(1, 1_000_000_000),
        "engine_model_type": config.engine,
        "voice_id": voice_id,
        "voice_format": 1,
        "needvad": 1,
        "sentence_strategy": 0,
        "convert_num_mode": 1,
    }


def _build_signed_websocket_url(config: TencentAsrConfig, voice_id: str) -> str:
    params = _base_request_params(config, voice_id)
    sorted_params = sorted(params.items(), key=lambda item: item[0])
    query = "&".join(f"{key}={value}" for key, value in sorted_params)
    path = ASR_PATH_TEMPLATE.format(appid=config.app_id)
    sign_string = f"{ASR_HOST}{path}?{query}"
    signature = _sign(sign_string, config.secret_key)
    encoded_signature = quote(signature, safe="")
    return f"wss://{ASR_HOST}{path}?{query}&signature={encoded_signature}"


def _sign(sign_string: str, secret_key: str) -> str:
    digest = hmac.new(
        secret_key.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _recv_json(ws_conn: Any) -> dict[str, Any]:
    raw_message = ws_conn.recv()
    if not raw_message:
        raise RuntimeError("腾讯云 ASR WebSocket 连接已关闭")
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    return json.loads(raw_message)
