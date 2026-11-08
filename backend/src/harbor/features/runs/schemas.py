"""HTTP contract for the AG-UI run endpoints (S14)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunMessage(BaseModel):
    """The subset of an AG-UI message this app needs: role + text."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    role: str
    content: str


class RunStartRequest(BaseModel):
    """``POST /agui/runs`` body.

    AG-UI's ``RunAgentInput`` carries the conversation; ``buyerId`` and
    ``shoppingSessionId`` are Harbor extensions that scope the turn (the
    checklist's multi-tenant contract). ``runId`` is optional so a client can
    retry idempotently.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    thread_id: str | None = Field(default=None, alias="threadId")
    run_id: str | None = Field(default=None, alias="runId")
    buyer_id: str = Field(alias="buyerId")
    shopping_session_id: str = Field(alias="shoppingSessionId")
    messages: list[RunMessage] = Field(min_length=1)

    @model_validator(mode="after")
    def _has_a_user_message(self) -> RunStartRequest:
        if not self.latest_user_text:
            raise ValueError("messages must contain a non-empty user message")
        return self

    @property
    def latest_user_text(self) -> str | None:
        for message in reversed(self.messages):
            if message.role == "user" and message.content.strip():
                return message.content
        return None


class RunStartResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    thread_id: str = Field(alias="threadId")


class RunSnapshotResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    thread_id: str = Field(alias="threadId")
    status: str
    last_seq: int = Field(alias="lastSeq")
    error: str | None = None
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
