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
    # Structured-output (Pydantic) validation attempts per state update, with
    # the same bounded repair-prompt semantics as the Summary pipeline.
    meeting_agent_max_attempts: int = 2
    meeting_agent_parse_repair: bool = True
    # Hard bound on the batch body fed to the LLM. No silent truncation: a batch
    # larger than this is SKIPPED with a logged reason (the state simply does not
    # advance — the transcript is untouched and a later batch can still progress).
    meeting_agent_max_batch_chars: int = 12_000
    # Hard bound on the accumulated state text included in the prompt (it is the
    # only part of the prompt that can grow unboundedly across the meeting).
    # Exceeding it fails the update loudly rather than silently clipping context.
    meeting_agent_max_state_chars: int = 24_000
    # Safety valve on ONE drain pass: how many batches a single trigger may
    # process. Bounds the work (and LLM cost) of a single call; whatever remains
    # is flushed by the next FINAL or by the finish-time forced flush.
    meeting_agent_max_batches_per_run: int = 4

    # --- Final Summary V2 (Step AGENT-2) ------------------------------------
    # Master switch for "Meeting State -> Final Summary". When false the summary
    # pipeline behaves EXACTLY as it did before AGENT-2 (full-transcript prompt),
    # regardless of whether a meeting state exists. This is the gray-release
    # kill switch: turning it off is a complete return to the legacy path, never
    # a partial state.
    final_summary_v2_enabled: bool = True
    # Upper bound on how many transcript segments are carried into the V2 prompt
    # as EVIDENCE. The state's source_segment_seqs are the primary selection;
    # this bound stops a pathological meeting from reconstituting the whole
    # transcript (which would defeat the point of consuming the state at all).
    # Selection is priority-ordered (decisions / action items first), so the
    # bound never silently drops the most load-bearing grounding.
    final_summary_max_evidence_segments: int = 60
    # Neighbour window around each cited segment (0 disables). A decision cited
    # at seq 8 is easier to judge with seq 7 and 9 alongside it, and the extra
    # context does not need to be cited by the state to be worth showing.
    final_summary_evidence_neighbor_window: int = 1
    # Hard bound on the assembled V2 prompt (state + evidence). Same stance as
    # the rest of the pipeline: refuse loudly rather than silently truncate.
    final_summary_max_input_chars: int = 120_000

    # Model name for the Final Summary Agent. Empty means "use the provider's
    # own configured model" (DeepSeekProvider already owns deepseek_model).
    # Set it only to pin the summary to a different model than the agent's.
    final_summary_model: str = ""


settings = Settings()
