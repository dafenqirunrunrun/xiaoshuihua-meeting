"""Application configuration.

All values come from environment variables / .env.
Nothing here is hard-coded credentials.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "小水华会议助手 MVP V0.1"
    debug: bool = True

    # Database (MVP uses SQLite; swap to company data platform later)
    database_url: str = "sqlite:///./data/xiaoshuihua.db"

    # DeepSeek (internal LLM). Values come from real internal config at 联调 time.
    deepseek_base_url: str = ""
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4.1-flash"

    # SSE heartbeat interval (seconds)
    sse_heartbeat_interval: float = 15.0

    # SSE per-subscriber bounded queue size (Step 5 Part 8): a subscriber whose
    # queue is full is EVICTED, never blocks the Producer. Tests can lower this.
    sse_queue_maxsize: int = 100

    # LLM call limits
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 1
    # Low variance is preferable for structured meeting facts. These are sent
    # through the OpenAI-compatible provider request, not applied to FakeProvider.
    llm_temperature: float = 0.1
    llm_max_output_tokens: int = 4096

    # Which provider the summary pipeline uses: "fake" (default, offline
    # deterministic tests) or "deepseek" (env-only config, real internal LLM).
    llm_provider: str = "fake"

    # Summary pipeline (Step 7A)
    # Structured-output (Pydantic) validation attempts per generation. Each
    # attempt after the first is driven by a repair prompt that includes the
    # offending raw output + the parse error, so a full retry is bounded.
    summary_max_attempts: int = 2
    # When true, a parse failure triggers a repair prompt (finite retries above);
    # when false, the first Pydantic failure immediately marks FAILED.
    summary_parse_repair: bool = True

    # Linear gain applied only to the ASR copy; raw PCM recordings are unchanged.
    asr_audio_gain: float = 1.0

    # Maximum time Finish waits for the ESP32 socket, PCM sender, Tencent ASR
    # receiver and transcript dispatcher to drain before refusing to freeze an
    # incomplete transcript.
    audio_session_stop_timeout_seconds: float = 40.0

    # Physical audio source used by the current single-room MVP deployment.
    shuihua_audio_device_id: str = "ESP32-001"

    # Tencent ASR settings are read from the existing backend .env file.
    tencent_asr_app_id: str = ""
    tencent_asr_secret_id: str = ""
    tencent_asr_secret_key: str = ""
    tencent_asr_engine: str = "16k_zh_en_speaker_2.0"
    tencent_asr_debug: bool = False

    # --- Meeting State Agent (Step AGENT-1) --------------------------------
    # Master switch. When false the whole downstream consumer is inert: no
    # scheduling, no LLM, no state row — Transcript / SSE / Summary behave
    # EXACTLY as before. The agent is a downstream consumer, never a
    # precondition for transcript commit.
    meeting_agent_enabled: bool = True
    # Batch/flush thresholds. A batch is flushed when EITHER bound is reached
    # (or when the meeting finishes / freezes — see meeting_state_agent).
    # Char count is the real cost driver for the prompt, so it is checked too.
    meeting_agent_batch_max_segments: int = 8
    meeting_agent_batch_max_chars: int = 1200
    # Realtime understanding is near-real-time, not subtitle-real-time. FINAL
    # signals only move one pending high-watermark; these thresholds decide when
    # the sole per-meeting worker consumes that range in one LLM call.
    meeting_agent_realtime_min_segments: int = 8
    meeting_agent_realtime_target_interval_seconds: float = 25.0
    meeting_agent_realtime_silence_seconds: float = 10.0
    meeting_agent_realtime_max_wait_seconds: float = 45.0
    # Realtime favours freshness: a timed-out pass remains pending and may be
    # coalesced with later FINALs, but it is never retried inside the request.
    meeting_agent_llm_timeout_seconds: float = 25.0
    # Structured-output (Pydantic) validation attempts per state update, with
    # the same bounded repair-prompt semantics as the Summary pipeline.
    meeting_agent_max_attempts: int = 1
    meeting_agent_parse_repair: bool = False
    # Hard bound on the batch body fed to the LLM. No silent truncation: a batch
    # larger than this is SKIPPED with a logged reason (the state simply does not
    # advance — the transcript is untouched and a later batch can still progress).
    meeting_agent_max_batch_chars: int = 12_000
    # Hard bound on the accumulated state text included in the prompt (it is the
    # only part of the prompt that can grow unboundedly across the meeting).
    # Exceeding it fails the update loudly rather than silently clipping context.
    meeting_agent_max_state_chars: int = 24_000
    # --- Final Summary V2 (Step AGENT-2) ------------------------------------
    # Master switch for "Meeting State -> Final Summary". When false the summary
    # pipeline behaves EXACTLY as it did before AGENT-2 (full-transcript prompt),
    # regardless of whether a meeting state exists. This is the gray-release
    # kill switch: turning it off is a complete return to the legacy path, never
    # a partial state.
    final_summary_v2_enabled: bool = True
    # Upper bound used only when the complete FINAL transcript makes the V2
    # prompt too large. The fallback is priority-ordered (decisions / action
    # items first), so capacity protection keeps the most load-bearing evidence.
    final_summary_max_evidence_segments: int = 60
    # Neighbour window around each cited segment (0 disables). A decision cited
    # at seq 8 is easier to judge with seq 7 and 9 alongside it, and the extra
    # context does not need to be cited by the state to be worth showing.
    final_summary_evidence_neighbor_window: int = 1
    # Hard bound on the assembled V2 prompt (state + evidence). Same stance as
    # the rest of the pipeline: refuse loudly rather than silently truncate.
    final_summary_max_input_chars: int = 100_000
    # Final generation may be substantially heavier than one incremental state
    # update, so use the same proven upper bound as the provider-level default.
    final_summary_llm_timeout_seconds: float = 120.0

    # Model name for the Final Summary Agent. Empty means "use the provider's
    # own configured model" (DeepSeekProvider already owns deepseek_model).
    # Set it only to pin the summary to a different model than the agent's.
    final_summary_model: str = ""


settings = Settings()
