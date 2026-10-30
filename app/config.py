from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://slate:slate@localhost:5432/slate"

    # Auth: Supabase Auth (Google provider). Tokens are verified locally against the project's JWKS.
    supabase_url: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_service_role_key: SecretStr = SecretStr("")

    storage_bucket: str = "meeting-audio"
    upload_url_ttl_s: int = 900

    stt_provider: Literal["deepgram", "groq", "fake"] = "deepgram"
    deepgram_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")

    llm_provider: Literal["groq", "anthropic", "openai-compatible", "fake"] = "groq"
    llm_model: str | None = None  # provider default when unset
    # openai-compatible only (OpenAI, OpenRouter, Together, Ollama...): endpoint and its key.
    llm_base_url: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")

    run_worker_in_process: bool = True
    worker_poll_interval_s: float = 2.0
    job_stale_after_s: int = 900

    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
