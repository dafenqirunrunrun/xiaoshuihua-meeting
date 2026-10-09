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

    # Snowflake node number. Every concurrently running backend instance that
    # shares a database must use a different value in the range 0..1023.
    meeting_id_worker_id: int = 0

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
    tencent_asr_engine: str = "16k_zh_en_2.0"
    tencent_asr_debug: bool = False
    # Product-level speaker labels are disabled independently of the provider
    # engine. This keeps an accidental provider-side speaker id from leaking
    # into transcripts or downstream Agent prompts.
    asr_speaker_labels_enabled: bool = False

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
    meeting_agent_realtime_min_segments: int = 3
    meeting_agent_realtime_target_interval_seconds: float = 4.0
    meeting_agent_realtime_silence_seconds: float = 1.5
    meeting_agent_realtime_max_wait_seconds: float = 8.0
    # Burst mode keeps live understanding responsive during heated discussion:
    # a dense run of FINALs should trigger immediately even if the normal
    # quiet/interval window has not elapsed.
    meeting_agent_realtime_burst_segments: int = 8
    meeting_agent_realtime_burst_window_seconds: float = 10.0
    # Realtime favours freshness: a timed-out pass remains pending and may be
    # coalesced with later FINALs, but it is never retried inside the request.
    meeting_agent_llm_timeout_seconds: float = 25.0
    meeting_agent_max_output_tokens: int = 900
    # The fast fact extractor runs on every ready window. User-facing prose is
    # intentionally refreshed less often so it cannot dominate output latency.
    meeting_agent_live_summary_interval_seconds: float = 12.0
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
    # A current Meeting State only needs its cited evidence plus a short closing
    # window. This preserves late corrections without re-sending the meeting.
    final_summary_tail_segments: int = 6
    # A healthy state may finish slightly behind the frozen transcript. When the
    # gap stays within this bound, send the complete unprocessed tail; a larger
    # gap is treated as severe staleness and falls back to the full transcript.
    final_summary_max_unprocessed_tail_segments: int = 12
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

    # --- 水华队长 AI Referee Sidecar (ADR-002) ------------------------------
    # Master switch. When false the whole sidecar is inert: no trigger, no
    # queue, no LLM, no report row. Transcript / SSE / Meeting State / Summary
    # behave EXACTLY as before. The referee is an ON-DEMAND consumer of
    # persisted FINAL, never a precondition for transcript commit.
    captain_enabled: bool = True
    # The referee analyses the most recent N seconds of FINAL transcript,
    # measured on the ASR-relative `start_ms` axis (NOT wall clock). 3 minutes
    # covers the run-up to a disagreement; longer windows dilute the focus and
    # raise the cost of a report the user asked for exactly once.
    captain_window_seconds: float = 180.0
    # Hard bound on the assembled context body. Meeting the bound drops the
    # OLDEST segments first (the freshest discussion matters most for a
    # referee), and a window that is still empty afterwards is refused rather
    # than analysed — see captain/context.py.
    captain_max_context_chars: int = 12_000
    # Minimum window size for a report to be meaningful. Below this the referee
    # tells the user there is not enough material instead of inventing a debate.
    captain_min_segments: int = 2
    # Throttle: at most one NEW report per meeting per interval. Guards against
    # a user hammering the button (the wake word itself is deduped by anchor).
    captain_min_interval_seconds: float = 30.0
    # Exit criteria for one analysis. Bounded repair shares the Summary
    # pipeline's semantics: a SHAPE failure is repairable, a PROVIDER failure
    # is not.
    captain_llm_timeout_seconds: float = 60.0
    captain_max_output_tokens: int = 2000
    captain_max_attempts: int = 2
    captain_parse_repair: bool = True

    # --- Captain two-stage assessment ---------------------------------------
    # When true, one trigger runs TWO calls: a small Quick pass whose whole
    # output is four fields, then the full Deep pass. The point is that the
    # Quick pass CANNOT become slow — its schema has nowhere to put a long
    # analysis — rather than that it is asked politely to be brief.
    #
    # Default is FALSE so every existing offline test, scenario fixture and
    # A/B search arm keeps exercising the single-call path unchanged. Turning
    # it on is a deliberate decision, not a side effect of upgrading.
    captain_two_stage_enabled: bool = False
    # Output budget for the Quick pass. Separate from the Deep budget because
    # the two have opposite requirements: Quick is bounded by its latency
    # target, Deep by the amount of judgement material it must produce. Sized
    # well below `captain_max_output_tokens` so a Quick call that starts
    # rambling is cut off before it can approach the report's cost.
    captain_quick_max_output_tokens: int = 700
    # Output budget for the Deep pass, used ONLY when two-stage mode is on.
    #
    # Larger than `captain_max_output_tokens` (2000) because the Deep schema is
    # structurally bigger than the flat report it replaces: the flat shape has
    # ~7 keys, the Deep one nests ~25 across claims/evidence/counter_evidence/
    # confirmed_facts/assumptions/missing_evidence/evidence_strength/citations.
    # Measured against the real gateway, a five-line window produced a JSON body
    # that was CUT OFF MID-STRING at 2000 tokens — the model was still writing
    # `citations` when the budget ran out, and the repair pass then hit the same
    # wall. Raising the budget is the correct fix; trimming the schema to fit a
    # budget sized for a different shape would be adapting the product to a
    # constant.
    captain_deep_max_output_tokens: int = 4096
    # Timeout for the Quick pass, deliberately much tighter than the Deep one.
    # A Quick pass that exceeds this is DEGRADED (dropped, Deep continues) —
    # that is the entire purpose of the bound.
    captain_quick_timeout_seconds: float = 20.0
    # Bounded queue depth for the dispatch worker. A full queue refuses the
    # trigger loudly (503) instead of growing without bound.
    captain_queue_maxsize: int = 16
    # Wake word. Kept as a setting so a deployment can retune phrasing without
    # a code change; `captain/wake_word.py` normalises both sides before match.
    captain_wake_words: str = "水华队长,水华队长。"

    # --- Captain external evidence search (Phase 3-2, ADR-003) --------------
    # Master switch for the WHOLE search enrichment layer. When false, no policy
    # evaluation, no provider construction, no cache lookup — the referee
    # behaves exactly as it did before Phase 3-2. Search is ENRICHMENT, never a
    # precondition: with this on and the provider broken, reports still complete.
    captain_external_search_enabled: bool = True
    # Provider selection. This is the A/B switch (Phase 3-2 §11): changing it to
    # "exa" once that provider exists must require no code change anywhere else.
    # Unknown values FAIL CLOSED (SearchProviderError) rather than picking a
    # default — a typo must not make an A/B run compare a provider to itself.
    captain_search_provider: str = "fake"
    # Forces FakeSearchProvider regardless of `captain_search_provider`. Exists
    # so CI can be deterministic even when a deployment is configured for a real
    # provider; the alternative (pointing tests at an unreachable host) turns a
    # passing suite into a flaky one.
    captain_search_force_fake: bool = False

    # HARD deadline for ONE search call (Phase 3-2 §5, retuned in Phase C.5).
    # On expiry the result is DISCARDED, not awaited — a late answer costs
    # latency and returns nothing, so the budget can only ever cost evidence,
    # never the report.
    #
    # Scope, verified before this was retuned: this number binds the PROVIDER
    # CALL and nothing else. The LLM timeouts are separate settings
    # (`LLM_TIMEOUT_SECONDS`, `captain_quick_timeout_seconds`) and are untouched
    # by it. That distinction is what makes raising it safe — the value now has
    # to cover a real Brave round trip rather than a fake's simulated one.
    #
    # 900 -> 2500 in Phase C.5, for two reasons that only together justify it:
    #
    #   1. Research now runs AFTER Quick has been delivered (`_generate_two_stage`
    #      calls the enrichment thunk past the `on_quick` boundary), so a longer
    #      search cannot touch Quick's 1-2s target. Under the previous ordering
    #      this value sat directly on Quick's critical path and raising it would
    #      have made that target unreachable by construction.
    #   2. 900ms was chosen as a guess, not a measurement, and it is shorter than
    #      a real search API round trip from this network. Measured against the
    #      gateway, ~2s is the honest floor for a query that returns sources, and
    #      a budget below the floor is a budget that always expires — which makes
    #      the whole layer dead code that reports `timeout` every run.
    #
    # Still overridable from `.env` (`CAPTAIN_SEARCH_TIMEOUT_MS`); the default is
    # a starting point for Phase 3-2E to tune, not a finding.
    captain_search_timeout_ms: int = 2500
    # Provider's own result cap. Kept <= 5 (Phase 3-2 §13).
    captain_search_max_results: int = 5
    # How many sources may reach the prompt. Separate from `max_results` on
    # purpose: a provider may return 5 and we still only budget for 3. This is a
    # TOKEN budget, and it is the number Phase 3-2E varies to measure relevance.
    captain_search_max_sources_for_llm: int = 3

    # Brave Search API key (LLM Context endpoint). Empty by default: the
    # provider reports `not_configured` per search rather than crashing startup,
    # the same lazy-key stance DeepSeekProvider takes (D14).
    brave_search_api_key: str = ""

    # --- 演示输入（会议室页「模拟参会人发言」） ------------------------------
    # Lets a human type simulated participant lines into a live meeting so the
    # whole chain (FINAL -> SSE -> Meeting State -> 水华队长) can be exercised
    # without an ESP32 / Tencent ASR in the room. ON by default because the
    # product demo depends on it; set false to REMOVE the route entirely (404),
    # not to forbid a user. It writes real FINAL rows, so a deployment that
    # considers this an abuse surface — rather than the product feature it is —
    # can switch it off without touching transcript, SSE or the referee.
    demo_ingress_enabled: bool = True


settings = Settings()
