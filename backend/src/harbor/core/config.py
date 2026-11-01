from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from HARBOR_* and OPENAI_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="HARBOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "harbor-purchase-agent"
    app_version: str = "0.1.0"
    api_prefix: str = "/api"
    host: str = "127.0.0.1"
    port: int = 8010
    log_level: str = "INFO"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    openai_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("OPENAI_BASE_URL", "HARBOR_OPENAI_BASE_URL"),
    )
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENAI_API_KEY", "HARBOR_OPENAI_API_KEY"),
    )
    openai_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("OPENAI_MODEL", "HARBOR_OPENAI_MODEL"),
    )
    openai_embedding_model: str = Field(
        default="text-embedding-v4",
        validation_alias=AliasChoices("OPENAI_EMBEDDING_MODEL", "HARBOR_OPENAI_EMBEDDING_MODEL"),
    )
    # Hard ceiling on the embedding HTTP call (S3-6): when the vector service
    # hangs, the search falls back to keyword_2gram within this budget.
    openai_embedding_timeout_seconds: float = Field(default=5.0, gt=0)
    # Unfiltered semantic window used to report rejected candidates (S2-2).
    search_candidate_pool_size: int = Field(default=100, ge=1)
    # S3: cross-encoder rerank (optional quality layer).
    search_rerank_enabled: bool = Field(default=True)
    search_rerank_model: str = Field(default="BAAI/bge-reranker-base")
    search_rerank_top_k: int = Field(default=30, ge=1)

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
