from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "simpsons-insight-agent"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    database_url: str = "sqlite+aiosqlite:///./data/reviews.db"

    headless: bool = False
    max_reviews: int = Field(default=500, ge=1, le=500)
    browser_profile_dir: Path = Path("./data/browser-profile")
    diagnostics_dir: Path = Path("./data/diagnostics")
    model_cache_dir: Path = Path("./data/model-cache")
    author_hash_key_path: Path = Path("./data/author-hash.key")

    openai_api_key: str | None = None
    openai_model_default: str = "gpt-5.4-mini-2026-03-17"
    openai_model_premium: str = "gpt-5.6-sol"
    cloud_batch_reviews: int = 40
    cloud_batch_chars: int = 60_000

    sentiment_model: str = "lxyuan/distilbert-base-multilingual-cased-sentiments-student"
    embedding_model: str = "intfloat/multilingual-e5-small"
    model_local_files_only: bool = False
    scrape_locale: str = "zh-TW"
    scrape_timeout_ms: int = 30_000
    scroll_wait_ms: int = 1_200
    no_growth_limit: int = 5
    persist_batch_size: int = 25
    browser_restart_limit: int = Field(default=2, ge=0, le=5)
    diagnostics_trace: bool = False
    sentiment_batch_size: int = Field(default=32, ge=1, le=128)
    source_http_timeout_seconds: float = Field(default=30.0, ge=5, le=120)
    source_http_retries: int = Field(default=2, ge=0, le=3)
    ptt_request_interval_seconds: float = Field(default=1.0, ge=0.5, le=10)
    dcard_request_interval_seconds: float = Field(default=2.0, ge=1, le=20)

    @property
    def allowed_llm_models(self) -> tuple[str, str]:
        return (self.openai_model_default, self.openai_model_premium)

    def ensure_directories(self) -> None:
        for directory in (
            Path("./data"),
            self.browser_profile_dir,
            self.diagnostics_dir,
            self.model_cache_dir,
            self.author_hash_key_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
