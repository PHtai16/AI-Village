"""User-level Task data contract separated cleanly from execution Run records."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    ChecklistItemId,
    ProjectId,
    RunId,
    TaskId,
    ensure_not_secret,
    new_task_id,
    validate_agent_id,
    validate_artifact_id,
    validate_aware_datetime,
    validate_checklist_item_id,
    validate_chronological,
    validate_safe_metadata,
    validate_unique_id_list,
)

TaskStatus = Literal[
    "QUEUED",
    "ASSIGNED",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "BLOCKED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
TaskPriority = Literal["LOW", "NORMAL", "HIGH", "URGENT"]

_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class Task(BaseModel):
    """Canonical Task contract representing one user goal without State Machine methods."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId = Field(default_factory=new_task_id)
    project_id: ProjectId | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=16000)
    requested_by: str = Field(min_length=1, max_length=100)
    status: TaskStatus = "QUEUED"
    priority: TaskPriority = "NORMAL"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    assigned_agent_ids: list[AgentId] = Field(default_factory=list, max_length=50)
    checklist_item_ids: list[ChecklistItemId] = Field(default_factory=list, max_length=200)
    active_run_id: RunId | None = None
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("title", "description", "requested_by")
    @classmethod
    def _validate_required_text(cls, value: str, info: object) -> str:
        """Ensure core descriptive fields are non-blank and free of credentials."""
        return ensure_not_secret(value, getattr(info, "field_name", "task_field"))

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_acceptance_criteria(cls, values: list[str]) -> list[str]:
        """Reject empty criteria entries or embedded secrets."""
        return [ensure_not_secret(item, "acceptance_criteria") for item in values]

    @field_validator("assigned_agent_ids")
    @classmethod
    def _validate_assigned_agents(cls, values: list[str]) -> list[str]:
        """Validate and deduplicate assigned Agent identifiers."""
        return validate_unique_id_list(values, validate_agent_id, "assigned_agent_ids")

    @field_validator("checklist_item_ids")
    @classmethod
    def _validate_checklist_ids(cls, values: list[str]) -> list[str]:
        """Validate and deduplicate referenced `chk_*` ChecklistItem identifiers."""
        return validate_unique_id_list(values, validate_checklist_item_id, "checklist_item_ids")

    @field_validator("artifact_ids")
    @classmethod
    def _validate_artifact_ids(cls, values: list[str]) -> list[str]:
        """Validate and deduplicate referenced `art_*` Artifact identifiers."""
        return validate_unique_id_list(values, validate_artifact_id, "artifact_ids")

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Enforce timezone-aware timestamps when provided."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(
        cls,
        value: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        """Reject secret keys or raw credential values in Task metadata."""
        return validate_safe_metadata(value, "metadata")

    @model_validator(mode="after")
    def _validate_timing_and_active_run(self) -> Task:
        """Enforce chronological consistency and clear active_run_id on terminal Task states."""
        validate_chronological(self.created_at, self.updated_at, "created_at", "updated_at")
        if self.status in _TERMINAL_TASK_STATUSES and self.active_run_id is not None:
            raise ValueError(
                f"Terminal Task status '{self.status}' must not retain a non-None active_run_id."
            )
        return self
