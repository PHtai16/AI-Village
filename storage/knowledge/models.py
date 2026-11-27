"""Pydantic v2 contracts for Phase 08 Evidence Vault and Permission-Filtered Knowledge Search."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.identifiers import (
    ArtifactId,
    DepartmentId,
    EvidenceId,
    ProjectId,
    RunId,
    SemVer,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    new_evidence_id,
    sanitize_payload,
    validate_aware_datetime,
    validate_safe_payload,
)
from core.tools.models import ToolExecutionRecord
from engines.antigravity.diagnostics import redact_secrets
from storage.artifacts.models import ArtifactAccessScope, ArtifactRegistryEntry

EvidenceKind = Literal[
    "qa_log",
    "evidence_link",
    "research_source",
    "execution_trace",
    "build_test_log",
    "screenshot_proof",
]
KnowledgeCategory = Literal[
    "architecture",
    "standard",
    "playbook",
    "research_finding",
    "postmortem",
    "deliverable_summary",
]
KnowledgeApprovalStatus = Literal["DRAFT", "APPROVED", "ARCHIVED"]


def new_knowledge_id() -> str:
    """Generate a canonical `knw_*` Knowledge entry identifier."""
    return f"knw_{uuid.uuid4().hex[:16]}"


class EvidenceRecord(BaseModel):
    """Immutable evidence record linking QA verification, research URLs, traces, and proofs."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: EvidenceId = Field(default_factory=new_evidence_id)
    evidence_kind: EvidenceKind
    task_id: TaskId
    run_id: RunId | None = None
    artifact_id: ArtifactId | None = None
    recorded_by: str = Field(min_length=1, max_length=128)
    source_url: str | None = Field(default=None, max_length=2048)
    retrieved_at: datetime | None = None
    untrusted_source: bool = False
    content_sha256: Sha256Hex | None = None
    summary: str = Field(min_length=1, max_length=1000)
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("recorded_by", "source_url")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "evidence_field"))

    @field_validator("summary")
    @classmethod
    def _scrub_summary(cls, value: str) -> str:
        cleaned = redact_secrets(value).strip()
        if not cleaned:
            raise ValueError("Evidence summary must not be blank.")
        return cleaned[:1000]

    @field_validator("redacted_payload")
    @classmethod
    def _sanitize_payload_dict(cls, value: dict[str, Any]) -> dict[str, Any]:
        scrubbed = sanitize_payload(value)
        if not isinstance(scrubbed, dict):
            raise ValueError("Evidence payload must deserialize to an object.")
        validate_safe_payload(scrubbed, "redacted_payload")
        return scrubbed

    @field_validator("created_at", "retrieved_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class QaInspectionBundle(BaseModel):
    """Aggregated QA inspection payload linking evidence, artifacts, and tool execution traces."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRegistryEntry] = Field(default_factory=list)
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list)
    has_verified_pass_artifact: bool = False


class KnowledgeEntry(BaseModel):
    """Permission-scoped reusable knowledge item backed by Markdown and optional evidence links."""

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str = Field(default_factory=new_knowledge_id, min_length=8, max_length=64)
    title: str = Field(min_length=3, max_length=200)
    category: KnowledgeCategory
    version: SemVer = "1.0.0"
    access_scope: ArtifactAccessScope = "task"
    owner_agent_id: str = Field(min_length=1, max_length=128)
    department: DepartmentId | None = None
    task_id: TaskId | None = None
    project_id: ProjectId | None = None
    source_artifact_id: ArtifactId | None = None
    source_evidence_id: EvidenceId | None = None
    tags: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_markdown: str = Field(min_length=1, max_length=200_000)
    content_sha256: Sha256Hex
    approval_status: KnowledgeApprovalStatus = "APPROVED"
    created_at: datetime
    updated_at: datetime

    @field_validator("title", "owner_agent_id")
    @classmethod
    def _validate_text(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "knowledge_field"))

    @field_validator("content_markdown")
    @classmethod
    def _scrub_markdown(cls, value: str) -> str:
        cleaned = redact_secrets(value).strip()
        if not cleaned:
            raise ValueError("content_markdown must not be blank.")
        return cleaned

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(t, "tags") for t in values]

    @field_validator("metadata")
    @classmethod
    def _validate_meta(cls, value: dict[str, Any]) -> dict[str, Any]:
        scrubbed = sanitize_payload(value)
        if not isinstance(scrubbed, dict):
            raise ValueError("Knowledge metadata must be an object.")
        validate_safe_payload(scrubbed, "knowledge_metadata")
        return scrubbed

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_ts(cls, value: datetime, info: object) -> datetime:
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class KnowledgeSearchQuery(BaseModel):
    """Structured metadata-first Knowledge search request."""

    model_config = ConfigDict(extra="forbid")

    query_text: str = Field(default="", max_length=300)
    category: KnowledgeCategory | None = None
    department: DepartmentId | None = None
    task_id: TaskId | None = None
    tags: list[str] = Field(default_factory=list, max_length=16)
    approval_status: KnowledgeApprovalStatus | None = "APPROVED"
    limit: int = Field(default=20, ge=1, le=100)


class KnowledgeSearchHit(BaseModel):
    """Ranked metadata search hit referencing an authorized KnowledgeEntry."""

    model_config = ConfigDict(extra="forbid")

    entry: KnowledgeEntry
    score: float = Field(ge=0.0)
    matched_fields: list[str] = Field(default_factory=list)


class KnowledgeSearchResult(BaseModel):
    """Deterministic metadata/keyword search outcome without unimplemented vector claims."""

    model_config = ConfigDict(extra="forbid")

    query_text: str
    search_mode: Literal["metadata_keyword"] = "metadata_keyword"
    semantic_search_enabled: Literal[False] = False
    total_hits: int = Field(ge=0)
    hits: list[KnowledgeSearchHit] = Field(default_factory=list)
