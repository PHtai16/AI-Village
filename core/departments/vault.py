"""Phase 09 / P09.09 Vault Department: governed artifact intake, metadata indexing, version lineage, and safe reuse."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.artifact import ArtifactVerificationStatus
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    DepartmentId,
    EventId,
    EvidenceId,
    RunId,
    SemVer,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_payload,
    validate_safe_relative_path,
    validate_semver,
)
from core.departments._common import (
    DepartmentBehaviorError,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source

VaultAccessScope = Literal["private", "task", "project", "global"]


class VaultDepartmentError(DepartmentBehaviorError):
    """Raised when Vault intake, metadata validation, or reuse guards fail."""


@runtime_checkable
class ArtifactVaultPort(Protocol):
    """Structural interface decoupled from ``storage.artifacts`` matching ``ArtifactVaultService``."""

    def register_artifact(
        self,
        *,
        auth_context: TrustedAuthContext,
        task_id: str,
        run_id: str,
        owner_agent_id: str,
        logical_key: str,
        relative_path: str,
        timestamp: datetime,
        source_path: Any = None,
        content_bytes: bytes | None = None,
        version: str | None = None,
        content_type: str | None = None,
        verification_status: ArtifactVerificationStatus = "UNVERIFIED",
        evidence_reference: str | None = None,
        access_scope: VaultAccessScope = "task",
        department: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Persist content-addressed artifact version."""

    def attach_task_reference(
        self,
        artifact_id: str,
        *,
        referencing_task_id: str,
        referencing_run_id: str | None = None,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> Any:
        """Protect shared artifact via cross-task reference."""

    def get_version_history(
        self,
        logical_key: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> Any:
        """List ascending SemVer versions for ``logical_key``."""

    def read_verified_bytes(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> tuple[Any, bytes]:
        """Verify SHA-256 and read blob bytes."""

    def preview_artifact(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> Any:
        """Produce deterministic preview for artifact."""

    def compare_artifacts(
        self,
        left_artifact_id: str,
        right_artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> Any:
        """Compare two artifact versions deterministically."""


@runtime_checkable
class KnowledgeVaultPort(Protocol):
    """Structural interface decoupled from ``storage.knowledge`` matching ``EvidenceAndKnowledgeService``."""

    def record_knowledge(
        self,
        *,
        auth_context: TrustedAuthContext,
        title: str,
        category: str,
        content_markdown: str,
        timestamp: datetime,
        version: str = "1.0.0",
        access_scope: VaultAccessScope = "task",
        department: str | None = None,
        task_id: str | None = None,
        source_artifact_id: str | None = None,
        source_evidence_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        approval_status: str = "APPROVED",
    ) -> Any:
        """Index reusable knowledge note cleanly."""


class VaultSubmissionRequest(BaseModel):
    """Governed document submission into Vault with optional Knowledge catalog indexing."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId
    owner_agent_id: AgentId
    logical_key: str = Field(min_length=2, max_length=160)
    relative_path: str = Field(min_length=1, max_length=260)
    content_bytes: bytes | None = None
    source_path: str | None = Field(default=None, max_length=512)
    version: SemVer | None = None
    content_type: str | None = Field(default=None, max_length=128)
    verification_status: ArtifactVerificationStatus = "UNVERIFIED"
    evidence_reference: str | None = Field(default=None, max_length=256)
    access_scope: VaultAccessScope = "task"
    department: DepartmentId | None = None
    tags: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    knowledge_title: str | None = Field(default=None, min_length=3, max_length=200)
    knowledge_category: Literal[
        "architecture",
        "standard",
        "playbook",
        "research_finding",
        "postmortem",
        "deliverable_summary",
    ] = "deliverable_summary"
    knowledge_summary_markdown: str | None = Field(default=None, min_length=3, max_length=50_000)
    source_evidence_id: EvidenceId | None = None
    timestamp: datetime

    @field_validator("logical_key", "knowledge_title")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "vault_label"))

    @field_validator("relative_path")
    @classmethod
    def _validate_rel(cls, value: str) -> str:
        return validate_safe_relative_path(value, "relative_path")

    @field_validator("version")
    @classmethod
    def _validate_ver(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_semver(value)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(t, "tags") for t in values]

    @field_validator("metadata")
    @classmethod
    def _validate_meta(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_safe_payload(value, "metadata")
        return value

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class VaultSubmissionOutcome(BaseModel):
    """Structured Vault archival result linking Artifact entry and optional Knowledge entry."""

    model_config = ConfigDict(extra="forbid")

    artifact: Any
    knowledge_entry: Any | None = None
    audit_event_id: EventId


class VaultReuseRequest(BaseModel):
    """Request to reference and retrieve an existing verified Vault artifact into a new Task."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId
    target_task_id: TaskId
    target_run_id: RunId | None = None
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class VaultReuseOutcome(BaseModel):
    """Verified cross-task artifact reuse payload with cryptographic preview and bytes."""

    model_config = ConfigDict(extra="forbid")

    artifact: Any
    content_bytes: bytes
    preview: Any
    audit_event_id: EventId


class VaultDepartmentService:
    """Vault Department coordinator orchestrating archival, lineage, preview, and cross-task reuse."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        artifact_vault: ArtifactVaultPort,
        knowledge_vault: KnowledgeVaultPort | None = None,
    ) -> None:
        """Bind Vault coordinator cleanly via structural Vault & Knowledge ports."""
        self._pm = permission_manager
        self._journal = journal
        self._vault = artifact_vault
        self._knowledge = knowledge_vault

    def submit_document(
        self,
        request: VaultSubmissionRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> VaultSubmissionOutcome:
        """Archive a new versioned artifact with metadata and optional Knowledge entry."""
        if request.verification_status == "VERIFIED_PASS" and not request.evidence_reference:
            raise VaultDepartmentError(
                "QA_EVIDENCE_REQUIRED",
                "Cannot submit Vault document as VERIFIED_PASS without evidence_reference.",
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="vault.write",
            resource=f"vault:dept_submit:{request.logical_key}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"vault-sub-{request.logical_key}",
        )

        entry = self._vault.register_artifact(
            auth_context=auth_context,
            task_id=request.task_id,
            run_id=request.run_id,
            owner_agent_id=request.owner_agent_id,
            logical_key=request.logical_key,
            relative_path=request.relative_path,
            source_path=request.source_path,
            content_bytes=request.content_bytes,
            version=request.version,
            content_type=request.content_type,
            verification_status=request.verification_status,
            evidence_reference=request.evidence_reference,
            access_scope=request.access_scope,
            department=request.department,
            tags=list(request.tags),
            metadata=dict(request.metadata),
            timestamp=request.timestamp,
        )

        knowledge_entry: Any | None = None
        if request.knowledge_title and request.knowledge_summary_markdown and self._knowledge is not None:
            knowledge_entry = self._knowledge.record_knowledge(
                auth_context=auth_context,
                title=request.knowledge_title,
                category=request.knowledge_category,
                content_markdown=request.knowledge_summary_markdown,
                version=entry.version,
                access_scope=request.access_scope,
                department=request.department,
                task_id=request.task_id,
                source_artifact_id=entry.artifact_id,
                source_evidence_id=request.source_evidence_id,
                tags=list(request.tags),
                metadata={"logical_key": request.logical_key, **request.metadata},
                approval_status="APPROVED" if request.verification_status == "VERIFIED_PASS" else "DRAFT",
                timestamp=request.timestamp,
            )

        ev = append_department_event(
            self._journal,
            event_type="artifact.recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            artifact_id=entry.artifact_id,
            evidence_id=request.source_evidence_id,
            timestamp=request.timestamp,
            correlation_id=f"vault-dept-{entry.artifact_id}",
            payload={
                "department": "vault",
                "logical_key": request.logical_key,
                "version": entry.version,
                "blob_sha256": entry.blob_sha256,
                "knowledge_indexed": knowledge_entry is not None,
            },
        )
        return VaultSubmissionOutcome(
            artifact=entry,
            knowledge_entry=knowledge_entry,
            audit_event_id=ev.event_id,
        )

    def get_version_history_with_preview(
        self,
        logical_key: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> tuple[Any, Any]:
        """Return full SemVer version lineage and deterministic preview of the latest version."""
        history = self._vault.get_version_history(
            logical_key,
            auth_context=auth_context,
            timestamp=timestamp,
        )
        preview = self._vault.preview_artifact(
            history.latest_artifact_id,
            auth_context=auth_context,
            timestamp=timestamp,
        )
        return history, preview

    def compare_versions(
        self,
        left_artifact_id: str,
        right_artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> Any:
        """Compare two Vault artifact versions deterministically."""
        return self._vault.compare_artifacts(
            left_artifact_id,
            right_artifact_id,
            auth_context=auth_context,
            timestamp=timestamp,
        )

    def reuse_artifact_in_task(
        self,
        request: VaultReuseRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> VaultReuseOutcome:
        """Attach cross-task reference, verify SHA-256 bytes, and deliver old artifact safely to new Task."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="vault.read",
            resource=f"vault:reuse:{request.artifact_id}",
            task_id=request.target_task_id,
            run_id=request.target_run_id,
            timestamp=request.timestamp,
            correlation_id=f"vault-reuse-{request.target_task_id}",
        )
        updated_entry = self._vault.attach_task_reference(
            request.artifact_id,
            referencing_task_id=request.target_task_id,
            referencing_run_id=request.target_run_id,
            auth_context=auth_context,
            timestamp=request.timestamp,
        )
        verified_entry, raw_bytes = self._vault.read_verified_bytes(
            updated_entry.artifact_id,
            auth_context=auth_context,
            timestamp=request.timestamp,
        )
        preview = self._vault.preview_artifact(
            verified_entry.artifact_id,
            auth_context=auth_context,
            timestamp=request.timestamp,
        )
        ev = append_department_event(
            self._journal,
            event_type="artifact.recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.target_task_id,
            run_id=request.target_run_id,
            actor_id=event_actor_id(auth_context),
            artifact_id=verified_entry.artifact_id,
            timestamp=request.timestamp,
            correlation_id=f"vault-reuse-{request.target_task_id}",
            payload={
                "department": "vault",
                "operation": "reuse_artifact_in_task",
                "artifact_id": verified_entry.artifact_id,
                "logical_key": verified_entry.logical_key,
                "version": verified_entry.version,
                "blob_sha256": verified_entry.blob_sha256,
            },
        )
        return VaultReuseOutcome(
            artifact=verified_entry,
            content_bytes=raw_bytes,
            preview=preview,
            audit_event_id=ev.event_id,
        )
