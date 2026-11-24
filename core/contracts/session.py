"""Logical Session contract separating AI Village sessions from Provider threads."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    AgentId,
    LegacySpikeSessionRef,
    RunId,
    SessionId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    new_session_id,
    validate_aware_datetime,
    validate_chronological,
)

SessionStatus = Literal["INITIALIZED", "ACTIVE", "IDLE", "CLOSED", "TERMINATED", "FAILED"]


class Session(BaseModel):
    """Execution session record with strict UUID v4 identity and legacy spike reference support."""

    model_config = ConfigDict(extra="forbid")

    session_id: SessionId = Field(default_factory=new_session_id)
    legacy_session_ref: LegacySpikeSessionRef | None = None
    agent_id: AgentId
    task_id: TaskId | None = None
    run_id: RunId | None = None
    invocation_id: str | None = Field(default=None, min_length=1, max_length=80)
    provider_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    provider_conversation_id: str | None = Field(default=None, min_length=1, max_length=160)
    workspace_reference: str | None = Field(default=None, min_length=1, max_length=512)
    status: SessionStatus = "INITIALIZED"
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("invocation_id", "provider_conversation_id")
    @classmethod
    def _validate_non_secret_ids(cls, value: str | None, info: object) -> str | None:
        """Validate optional invocation and provider thread identifiers cleanly."""
        if value is None:
            return None
        field_name = getattr(info, "field_name", "identifier")
        return ensure_not_secret(value, field_name)

    @field_validator("workspace_reference")
    @classmethod
    def _validate_workspace_reference(cls, value: str | None) -> str | None:
        """Ensure workspace reference string contains no traversal escapes or secrets."""
        if value is None:
            return None
        cleaned = ensure_not_secret(value, "workspace_reference")
        normalized = cleaned.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if ".." in parts:
            raise ValueError(
                f"workspace_reference must not contain '..' traversal segments: {value!r}"
            )
        return cleaned

    @field_validator("started_at", "finished_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Enforce timezone-aware session timestamps."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def _validate_isolation_and_lifecycle(self) -> Session:
        """Enforce distinctness between logical Session ID, Provider thread, and Task/Run IDs."""
        if (
            self.provider_conversation_id is not None
            and self.provider_conversation_id.lower() == self.session_id
        ):
            raise ValueError(
                "provider_conversation_id must stay distinct from AI Village logical session_id."
            )
        if self.invocation_id is not None:
            if self.invocation_id == self.session_id:
                raise ValueError("invocation_id must not equal session_id.")
            if self.invocation_id.startswith(("task_", "run_", "art_", "evt_")):
                raise ValueError(
                    "invocation_id must not masquerade as a domain Task, Run, Artifact, or Event ID."
                )
        if self.status == "INITIALIZED":
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("INITIALIZED session must not pre-populate started_at or finished_at.")
        validate_chronological(self.started_at, self.finished_at, "started_at", "finished_at")
        return self
