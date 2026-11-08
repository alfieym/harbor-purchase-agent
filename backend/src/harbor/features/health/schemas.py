from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    service: str
    version: str
    environment: str
    llm_configured: bool = Field(
        description="Whether an LLM API key is configured; the key itself is never exposed."
    )
    model: str | None = Field(
        description="The configured model name, or null when no key is set (S5-3)."
    )
    dependencies: dict[str, str] = Field(
        description="Status of each dependency (llm / catalog), e.g. ok / unconfigured (S5-3)."
    )
    time: datetime = Field(description="Server time in UTC.")
