from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


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

    # Chat provider: "deepseek" or "dashscope" (Alibaba Cloud Bailian / Qwen).
    # Only picks the client class; OPENAI_BASE_URL / OPENAI_MODEL still decide
    # the endpoint and model name.
    model_provider: Literal["deepseek", "dashscope"] = Field(
        default="deepseek",
        validation_alias=AliasChoices("HARBOR_MODEL_PROVIDER", "MODEL_PROVIDER"),
    )

    openai_base_url: str = Field(
        default=DEEPSEEK_BASE_URL,
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
        default="qwen3.7-text-embedding-flash",
        validation_alias=AliasChoices("OPENAI_EMBEDDING_MODEL", "HARBOR_OPENAI_EMBEDDING_MODEL"),
    )
    # Provider-side inputs per /embeddings request. DashScope's
    # qwen3.7-text-embedding-flash accepts at most 20 texts per call, so index
    # builds are chunked. (text-embedding-v4 accepts 10 — lower this if you switch.)
    openai_embedding_batch_size: int = Field(
        default=20,
        ge=1,
        validation_alias=AliasChoices(
            "OPENAI_EMBEDDING_BATCH_SIZE", "HARBOR_OPENAI_EMBEDDING_BATCH_SIZE"
        ),
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
    # Hard ceiling on one model+tool turn (S5-2): when the whole ReAct turn runs
    # longer than this, the request fails with a structured 504 instead of hanging.
    turn_timeout_seconds: float = Field(default=120.0, gt=0)

    # S6: durable session snapshots + journal live under this directory
    # (repo-root anchored, matching ``DEFAULT_DATA_PATH`` in factory.py).
    sessions_dir: Path = Path(__file__).resolve().parents[4] / "data" / "sessions"

    # S7: context governance. Compression starts early enough to leave room for
    # the summary call itself; the gate is the hard ceiling a turn must fit in.
    # ``legacy`` turns the whole middleware off and falls back to AgentScope's
    # built-in compression, which is the pre-S7 baseline (S7-5).
    context_strategy: Literal["governed", "legacy"] = Field(
        default="governed",
        validation_alias=AliasChoices("HARBOR_CONTEXT_STRATEGY", "CONTEXT_STRATEGY"),
    )
    context_trigger_ratio: float = Field(default=0.6, gt=0, le=0.9)
    context_gate_ratio: float = Field(default=0.85, gt=0, le=1.0)
    context_protect_turns: int = Field(default=2, ge=1)
    context_archive_dir: Path = (
        Path(__file__).resolve().parents[4] / "data" / "sessions" / "archives"
    )

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
