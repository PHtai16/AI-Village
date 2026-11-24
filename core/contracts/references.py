"""Safe reference contracts: ConnectionRef, ProjectRef, ChecklistItem, NotificationRef."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    AgentId,
    ChecklistItemId,
    CredentialRefHandle,
    EventId,
    NotificationId,
    ProjectId,
    RunId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    new_checklist_item_id,
    new_notification_id,
    new_project_id,
    reject_secret_text,
    validate_aware_datetime,
    validate_chronological,
)

ChecklistItemStatus = Literal[
    "PENDING",
    "IN_PROGRESS",
    "WAITING_APPROVAL",
    "BLOCKED",
    "COMPLETED",
    "FAILED",
    "SKIPPED",
    "CANCELLED",
]
NotificationSeverity = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
NotificationType = Literal[
    "TASK_UPDATE",
    "RUN_FAILED",
    "APPROVAL_REQUIRED",
    "QA_RESULT",
    "RESOURCE_WARNING",
    "SECURITY_ALERT",
    "SYSTEM_NOTICE",
]


class ConnectionRef(BaseModel):
    """Non-secret connection reference bound by Core models without CredentialResolver logic."""

    model_config = ConfigDict(extra="forbid")

    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_ref: str | None = Field(default=None, min_length=1, max_length=128)
    credential_ref: CredentialRefHandle | None = None

    @field_validator("model_id", "project_ref")
    @classmethod
    def _validate_optional_non_secret(cls, value: str | None, info: object) -> str | None:
        """Ensure optional routing and billing scope fields never hold raw keys."""
        if value is None:
            return None
        field_name = getattr(info, "field_name", "reference")
        return ensure_not_secret(value, field_name)

    def to_safe_summary(self) -> dict[str, str | None]:
        """Return safe display metadata with `credential_ref` masked."""
        masked_cred: str | None
        if self.credential_ref is None:
            masked_cred = None
        elif len(self.credential_ref) > 4:
            masked_cred = f"{self.credential_ref[:4]}***"
        else:
            masked_cred = "configured"
        return {
            "provider_id": self.provider_id,
            "connection_id": self.connection_id,
            "model_id": self.model_id,
            "project_ref": self.project_ref,
            "credential_ref": masked_cred,
        }


class ProjectRef(BaseModel):
    """Pure organizational grouping reference; never acts as a parallel Runtime or State Machine."""

    model_config = ConfigDict(extra="forbid")

    project_id: ProjectId = Field(default_factory=new_project_id)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name", "description")
    @classmethod
    def _validate_project_text(cls, value: str | None, info: object) -> str | None:
        """Reject secret-bearing strings or blank names."""
        if value is None:
            return None
        field_name = getattr(info, "field_name", "project_field")
        if field_name == "name":
            return ensure_not_secret(value, field_name)
        cleaned = value.strip()
        reject_secret_text(cleaned, field_name)
        return cleaned or None


class ChecklistItem(BaseModel):
    """Evidence-gated task step contract without Kanban UI logic or fabricated percentages."""

    model_config = ConfigDict(extra="forbid")

    checklist_item_id: ChecklistItemId = Field(default_factory=new_checklist_item_id)
    task_id: TaskId
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    assigned_agent_id: AgentId | None = None
    status: ChecklistItemStatus = "PENDING"
    order: int = Field(ge=0, le=10000)
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        """Ensure step title is non-blank and free of secrets."""
        return ensure_not_secret(value, "title")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        """Ensure optional step description is clean and non-secret."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "description")
        return cleaned or None

    @field_validator("evidence_reference")
    @classmethod
    def _validate_evidence_ref(cls, value: str | None) -> str | None:
        """Validate evidence reference without confusing it with Task/Run/Session IDs."""
        if value is None:
            return None
        cleaned = ensure_not_secret(value, "evidence_reference")
        if cleaned.startswith(("task_", "run_", "sess-")):
            raise ValueError(
                "evidence_reference must reference verification evidence or artifacts, not Task/Run/Session IDs."
            )
        return cleaned

    @field_validator("created_at", "completed_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Enforce timezone-aware timestamps when supplied."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def _validate_completion_evidence_and_chronology(self) -> ChecklistItem:
        """Prevent COMPLETED status without independent evidence and completion timestamp."""
        if self.status == "COMPLETED":
            if self.evidence_reference is None:
                raise ValueError(
                    "ChecklistItem cannot be COMPLETED without an evidence_reference."
                )
            if self.completed_at is None:
                raise ValueError(
                    "ChecklistItem cannot be COMPLETED without a real completed_at timestamp."
                )
        elif self.completed_at is not None:
            raise ValueError(
                "completed_at must remain None unless ChecklistItem status is COMPLETED."
            )
        if self.created_at is not None and self.completed_at is not None:
            validate_chronological(self.created_at, self.completed_at, "created_at", "completed_at")
        return self


class NotificationRef(BaseModel):
    """Lightweight notification reference linking back to canonical Event/Task/Run records."""

    model_config = ConfigDict(extra="forbid")

    notification_id: NotificationId = Field(default_factory=new_notification_id)
    task_id: TaskId | None = None
    run_id: RunId | None = None
    event_id: EventId
    severity: NotificationSeverity = "INFO"
    notification_type: NotificationType
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware creation timestamp."""
        return validate_aware_datetime(value, "created_at")
