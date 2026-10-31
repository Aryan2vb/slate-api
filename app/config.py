from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    # Readable coloured lines locally, one JSON object per line in production. Unset: chosen from `env`.
    log_format: Literal["console", "json"] | None = None

    database_url: str = "postgresql+asyncpg://slate:slate@localhost:5432/slate"

    # Auth: Supabase Auth (Google provider). Tokens are verified locally against the project's JWKS.
    supabase_url: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_service_role_key: SecretStr = SecretStr("")

    # Development only: also accept the bearer token "dev" as DEV_USER_ID, so the app can be used without signing in.
    # Refused at startup when ENV=prod.
    dev_auth: bool = False
    dev_user_id: UUID = UUID("00000000-0000-4000-8000-00000000d3e7")

    storage_bucket: str = "meeting-audio"
    upload_url_ttl_s: int = 900

    stt_provider: Literal["deepgram", "groq", "fake"] = "deepgram"
    deepgram_api_key: SecretStr = SecretStr("")
    # "multi" auto-handles ~55 languages including mid-sentence switching (Hindi + English). A BCP-47 code such
    # as "en" pins one language. Deepgram's own default is English-only, which returns nothing for Hindi speech.
    stt_language: str = "multi"
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

    # Per-user limits on endpoints that spend provider credit (requests per rolling hour).
    rate_limit_processing_per_hour: int = 30  # finish + reprocess (Deepgram + LLM)
    rate_limit_catchup_per_hour: int = 60  # catch-up (LLM)
    rate_limit_client_logs_per_hour: int = 720  # app log batches (one every ~5 s at most)

    @property
    def resolved_log_format(self) -> Literal["console", "json"]:
        return self.log_format or ("console" if self.env == "dev" else "json")

    @model_validator(mode="after")
    def _no_dev_auth_in_prod(self) -> "Settings":
        if self.dev_auth and self.env == "prod":
            raise ValueError("DEV_AUTH must not be enabled with ENV=prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
