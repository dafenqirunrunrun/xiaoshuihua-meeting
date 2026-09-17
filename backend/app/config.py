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


settings = Settings()
