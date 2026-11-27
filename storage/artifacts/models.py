"""Pydantic v2 contracts for Phase 08 Artifact Registry, Product Preview, Comparison, Trash, and Backups."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.artifact import ArtifactVerificationStatus
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    DepartmentId,
    RunId,
    SemVer,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_payload,
    validate_safe_relative_path,
)
from engines.antigravity.diagnostics import redact_secrets

ArtifactAccessScope = Literal["private", "task", "project", "global"]
ArtifactLifecycleStatus = Literal["ACTIVE", "TRASHED", "PURGED"]
PreviewKind = Literal["TEXT", "DOCX", "XLSX", "PDF", "IMAGE", "BINARY_METADATA_ONLY"]
ComparisonMethod = Literal["deterministic_structural", "deterministic_text_diff", "binary_hash_only"]


def new_backup_id() -> str:
    """Generate a canonical `bkp_*` Vault backup identifier."""
    return f"bkp_{uuid.uuid4().hex[:16]}"


class VaultError(RuntimeError):
    """Structured fail-closed error raised by Artifact Vault, Trash, and Backup services."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)[:500]
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class VaultApprovalRequiredError(VaultError):
    """Raised fail-closed when a destructive or restore operation awaits operator approval."""

    def __init__(
        self,
        *,
        approval_action: str,
        resource: str,
        request_fingerprint: str,
        message: str,
    ) -> None:
        super().__init__("APPROVAL_REQUIRED", message)
        self.approval_action = approval_action
        self.resource = resource
        self.request_fingerprint = request_fingerprint


class ArtifactRegistryEntry(BaseModel):
    """Authoritative versioned Artifact entry backed by content-addressed storage."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId
    logical_key: str = Field(min_length=1, max_length=160)
    version: SemVer
    version_number: int = Field(ge=1)
    previous_artifact_id: ArtifactId | None = None
    task_id: TaskId
    run_id: RunId
    owner_agent_id: AgentId
    relative_path: str = Field(min_length=1, max_length=260)
    blob_sha256: Sha256Hex
    storage_relpath: str = Field(min_length=1, max_length=260)
    size_bytes: int = Field(ge=0)
    content_type: str = Field(min_length=3, max_length=128)
    verification_status: ArtifactVerificationStatus = "UNVERIFIED"
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=256)
    access_scope: ArtifactAccessScope = "task"
    department: DepartmentId | None = None
    tags: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    lifecycle_status: ArtifactLifecycleStatus = "ACTIVE"
    trashed_at: datetime | None = None
    trashed_by: str | None = None
    trash_reason: str | None = None
    purged_at: datetime | None = None
    created_at: datetime
    referencing_task_ids: list[TaskId] = Field(default_factory=list)

    @field_validator("relative_path")
    @classmethod
    def _validate_relpath(cls, value: str) -> str:
        return validate_safe_relative_path(value, "relative_path")

    @field_validator("logical_key", "content_type", "trashed_by", "trash_reason")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "vault_field"))

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(t, "tags") for t in values]

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_safe_payload(value, "artifact_metadata")
        return value

    @field_validator("created_at", "trashed_at", "purged_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def _enforce_verification_rule(self) -> ArtifactRegistryEntry:
        if self.verification_status == "VERIFIED_PASS" and self.evidence_reference is None:
            raise ValueError("VERIFIED_PASS artifact requires non-null evidence_reference.")
        return self


class ArtifactVersionHistory(BaseModel):
    """Ordered version lineage for one logical artifact key."""

    model_config = ConfigDict(extra="forbid")

    logical_key: str
    latest_artifact_id: ArtifactId
    version_count: int = Field(ge=1)
    versions: list[ArtifactRegistryEntry]


class ArtifactPreviewResult(BaseModel):
    """Bounded format-aware preview extracted deterministically from verified bytes."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId
    content_type: str
    preview_kind: PreviewKind
    truncated: bool
    size_bytes: int = Field(ge=0)
    sha256: Sha256Hex
    preview_data: dict[str, Any]


class ArtifactComparisonResult(BaseModel):
    """Deterministic file comparison metadata without LLM-fabricated diffs."""

    model_config = ConfigDict(extra="forbid")

    left_artifact_id: ArtifactId
    right_artifact_id: ArtifactId
    left_version: SemVer
    right_version: SemVer
    identical_bytes: bool
    left_sha256: Sha256Hex
    right_sha256: Sha256Hex
    size_delta_bytes: int
    comparison_method: ComparisonMethod
    summary_metrics: dict[str, Any] = Field(default_factory=dict)
    diff_lines: list[str] = Field(default_factory=list)
    llm_generated: Literal[False] = False


class TrashActionResult(BaseModel):
    """Structured result of trashing, restoring, or purging an Artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId
    lifecycle_status: ArtifactLifecycleStatus
    shared_with_other_tasks: bool
    referencing_task_ids: list[TaskId] = Field(default_factory=list)
    blob_deleted: bool
    message: str


class BulkTrashReport(BaseModel):
    """Outcome of safe bulk trash protecting artifacts referenced by other tasks."""

    model_config = ConfigDict(extra="forbid")

    requested_ids: list[str]
    trashed_ids: list[str] = Field(default_factory=list)
    skipped_protected: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class VaultBackupRecord(BaseModel):
    """Metadata and cryptographic checksums for one Vault + SQLite snapshot backup."""

    model_config = ConfigDict(extra="forbid")

    backup_id: str
    backup_relpath: str
    db_sha256: Sha256Hex
    manifest_sha256: Sha256Hex
    blob_count: int = Field(ge=0)
    created_by: str
    created_at: datetime
    retention_days: int = Field(ge=1)


class VaultRestoreReport(BaseModel):
    """Verified outcome of restoring SQLite database and Vault blobs from backup."""

    model_config = ConfigDict(extra="forbid")

    backup_id: str
    restored_db_path: str
    restored_blob_count: int = Field(ge=0)
    integrity_ok: bool
    foreign_key_ok: bool
    restored_at: datetime
