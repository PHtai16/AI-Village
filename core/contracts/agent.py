"""Core Agent Identity and minimal Runtime State reference contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    DepartmentId,
    RunId,
    SessionId,
    TaskId,
    ensure_not_secret,
    reject_secret_text,
    validate_aware_datetime,
    validate_capability_id,
    validate_unique_id_list,
)
from core.contracts.references import ConnectionRef

DEFAULT_DEPARTMENTS: frozenset[str] = frozenset(
    {
        "hr",
        "resources",
        "service",
        "marketing",
        "it",
        "reception",
        "process",
        "qa",
        "vault",
    }
)
RESERVED_PROVIDER_DEPARTMENT_NAMES: frozenset[str] = frozenset(
    {
        "antigravity",
        "google_gemini",
        "openai",
        "anthropic",
        "ollama",
        "openai_compatible",
    }
)

AgentSystemRole = Literal["orchestrator", "worker"]
AgentRuntimeStateStatus = Literal[
    "IDLE",
    "QUEUED",
    "ASSIGNED",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "BLOCKED",
    "IN_MEETING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]


class AgentIdentity(BaseModel):
    """Minimal static Agent identity contract aligned with AGENT_SPEC.md without manifest bloat."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    name: str = Field(min_length=3, max_length=64)
    role: str = Field(min_length=3, max_length=64)
    system_role: AgentSystemRole
    department: DepartmentId
    description: str | None = Field(default=None, max_length=1000)
    capability_ids: list[CapabilityId] = Field(default_factory=list, max_length=50)
    allowed_connections: list[ConnectionRef] = Field(default_factory=list, max_length=20)

    @field_validator("name", "role")
    @classmethod
    def _validate_display_fields(cls, value: str, info: object) -> str:
        """Ensure display name and role are non-blank and free of credentials."""
        return ensure_not_secret(value, getattr(info, "field_name", "agent_field"))

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        """Ensure optional description contains no secret patterns."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "description")
        return cleaned or None

    @field_validator("capability_ids")
    @classmethod
    def _validate_capabilities(cls, values: list[str]) -> list[str]:
        """Deduplicate and validate snake_case capability tags."""
        return validate_unique_id_list(values, validate_capability_id, "capability_ids")

    @model_validator(mode="after")
    def _validate_role_and_department_invariants(self) -> AgentIdentity:
        """Enforce AGENT_SPEC.md Section 2.1 Master exception and Department!=Provider rule."""
        if self.department in RESERVED_PROVIDER_DEPARTMENT_NAMES:
            raise ValueError(
                f"department '{self.department}' names an AI Provider; Department must represent capability domains."
            )

        is_master_id = self.agent_id == "core.master"
        is_orch_dept = self.department == "orchestration"
        is_orch_role = self.system_role == "orchestrator"

        if is_orch_role and not (is_master_id and is_orch_dept):
            raise ValueError(
                "Only agent_id='core.master' within department='orchestration' may hold system_role='orchestrator'."
            )
        if is_master_id and not (is_orch_role and is_orch_dept):
            raise ValueError(
                "agent_id='core.master' requires system_role='orchestrator' and department='orchestration'."
            )
        if is_orch_dept and not (is_master_id and is_orch_role):
            raise ValueError(
                "department='orchestration' is reserved exclusively for agent_id='core.master' with system_role='orchestrator'."
            )
        return self


Agent = AgentIdentity


class AgentRuntimeStateRef(BaseModel):
    """Dynamic runtime reference kept separate from static Agent identity."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    session_id: SessionId | None = None
    current_task_id: TaskId | None = None
    current_run_id: RunId | None = None
    status: AgentRuntimeStateStatus = "IDLE"
    active_connection: ConnectionRef | None = None
    last_active_at: datetime | None = None

    @field_validator("last_active_at")
    @classmethod
    def _validate_last_active_at(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware datetime if present."""
        if value is None:
            return None
        return validate_aware_datetime(value, "last_active_at")
