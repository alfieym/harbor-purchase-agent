from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    llm_configured: bool = Field(
        description="Whether an LLM API key is configured; the key itself is never exposed."
    )
    time: datetime = Field(description="Server time in UTC.")
