"""Domain contracts, read/write ports, and fail-closed errors for P09.07 + P10.01 Process Retrospectives."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.artifact import ArtifactVerificationStatus
from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    CapabilityId,
    DepartmentId,
    EventId,
    EvidenceId,
    RunId,
    SessionId,
    Sha256Hex,
    SnakeId,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_sha256_hex,
    validate_snake_id,
)
from core.contracts.references import ChecklistItem
from core.contracts.run import Run
from core.contracts.session import Session
from core.contracts.task import Task
from core.departments._common import DepartmentBehaviorError
from core.events.journal import JournalEntry
from core.tools.models import ToolExecutionRecord
from engines.antigravity.diagnostics import redact_secrets

_RETRO_ID_RE = re.compile(r"^ret_[a-z0-9]{8,60}$")

DurationSource = Literal[
    "timestamps_measured",
    "run_telemetry_measured",
    "tool_duration_sum",
    "explicit_input",
    "UNAVAILABLE",
]
CostStatus = Literal[
    "VERIFIED_MEASURED",
    "ESTIMATED_FROM_TOOLS_OR_RUN",
    "UNAVAILABLE_FROM_PROVIDER",
]
SkillUsageStatus = Literal[
    "OBSERVED",
    "DECLARED_OR_ALLOWED_ONLY_NOT_INVOKED",
    "UNAVAILABLE_NO_SKILL_DATA",
]
SkillOutcome = Literal[
    "SUCCEEDED",
    "FAILED",
    "NOT_INVOKED",
    "UNVERIFIED",
    "UNAVAILABLE",
]


def new_retrospective_id() -> str:
    """Generate canonical retrospective identifier (`ret_*`)."""
    return f"ret_{uuid.uuid4().hex[:14]}"


def validate_retrospective_id(value: str) -> str:
    """Validate canonical `ret_*` identifier syntax cleanly."""
    cleaned = ensure_not_secret(value, "retrospective_id")
    if not _RETRO_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"retrospective_id must match '^ret_[a-z0-9]{{8,60}}$', got {value!r}."
        )
    return cleaned


def scrub_text(value: str, *, max_length: int = 1000) -> str:
    """Redact potential secret patterns from observed upstream strings before persistence."""
    return redact_secrets(str(value)).strip()[:max_length]


def compute_summary_fingerprint(payload: dict[str, Any]) -> str:
    """Compute canonical deterministic SHA-256 over factual execution history payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProcessValidationError(DepartmentBehaviorError):
    """Raised when retrospective inputs, identifiers, or pattern queries fail validation."""


class ProcessTaskNotFoundError(DepartmentBehaviorError):
    """Raised fail-closed when compiling a retrospective for a nonexistent Task or mismatched Run."""


class ProcessOwnershipError(DepartmentBehaviorError):
    """Raised when an unauthorized actor attempts to read or modify another Task's retrospective."""


class DuplicateRetrospectiveConflictError(DepartmentBehaviorError):
    """Raised when attempting conflicting retrospective creation on an existing record."""


class StaleRetrospectiveVersionError(DepartmentBehaviorError):
    """Raised on optimistic locking mismatch when updating a persisted Task retrospective."""


class TaskRetrospectiveInput(BaseModel):
    """Structured factual summary of tools, errors, fixes, and duration observed on one Task."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    departments_involved: list[DepartmentId] = Field(min_length=1, max_length=9)
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list)
    errors_observed: list[str] = Field(default_factory=list, max_length=32)
    fixes_applied: list[str] = Field(default_factory=list, max_length=32)
    pattern_tag: str | None = Field(default=None, max_length=64)
    duration_ms: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    timestamp: datetime

    @field_validator("errors_observed", "fixes_applied")
    @classmethod
    def _validate_notes(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "retrospective_list")
        return [ensure_not_secret(v, field_name) for v in values]

    @field_validator("pattern_tag")
    @classmethod
    def _validate_tag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_snake_id(value, "pattern_tag")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ProcessRetrospectiveRecord(BaseModel):
    """Backward-compatible P09.07 retrospective summary view backed by persistent storage."""

    model_config = ConfigDict(extra="forbid")

    retrospective_id: str
    task_id: TaskId
    run_id: RunId | None = None
    departments_involved: list[DepartmentId]
    tools_used: list[str]
    failed_tool_ids: list[str]
    errors_observed: list[str]
    fixes_applied: list[str]
    pattern_tag: str | None = None
    duration_ms: int = Field(ge=0)
    cost_usd: float | None = None
    recorded_at: datetime
    audit_event_id: EventId


class SkillCandidateProposal(BaseModel):
    """Candidate skill draft proposed ONLY from observed repeated patterns (never self-approved)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    suggested_skill_id: str
    pattern_key: str
    occurrence_count: int = Field(ge=2)
    evidence_task_ids: list[TaskId]
    evidence_retrospective_ids: list[str]
    observed_errors: list[str]
    observed_tools: list[str]
    rationale: str
    status: Literal["PROPOSED_CANDIDATE"] = "PROPOSED_CANDIDATE"
    benchmark_status: Literal["NOT_BENCHMARKED"] = "NOT_BENCHMARKED"
    approved_for_production: Literal[False] = False
    proposed_at: datetime
    audit_event_id: EventId


class RetrospectiveStepRecord(BaseModel):
    """Single factual execution step observed via Checklist, Worker Session, or Journal."""

    model_config = ConfigDict(extra="forbid")

    step_id: str | None = None
    checklist_item_id: str | None = None
    title: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=64)
    agent_id: AgentId | None = None
    department: DepartmentId | None = None
    session_id: SessionId | None = None
    run_id: RunId | None = None
    evidence_reference: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    source: Literal["checklist_item", "worker_session", "event_journal", "manual_input"]

    @field_validator("title", "status")
    @classmethod
    def _scrub_labels(cls, value: str) -> str:
        return scrub_text(value, max_length=300) or "unknown"

    @field_validator("started_at", "finished_at")
    @classmethod
    def _validate_ts(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class RetrospectiveToolSummary(BaseModel):
    """Per-tool execution metrics aggregated strictly from deduplicated ToolExecutionRecords."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(min_length=1, max_length=128)
    invocation_count: int = Field(ge=1)
    success_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    denied_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    retry_count: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(ge=0)
    known_cost_usd: float | None = Field(default=None, ge=0.0)
    execution_ids: list[str] = Field(default_factory=list)


class RetrospectiveErrorRecord(BaseModel):
    """Secret-scrubbed error captured strictly from observed failures or errors."""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)
    stage: str | None = None
    tool_id: str | None = None
    execution_id: str | None = None
    run_id: RunId | None = None
    session_id: SessionId | None = None
    source: Literal[
        "run_error",
        "tool_execution",
        "event_journal",
        "qa_verification",
        "manual_input",
    ]
    occurred_at: datetime | None = None

    @field_validator("error_code", "message")
    @classmethod
    def _scrub_fields(cls, value: str, info: object) -> str:
        return scrub_text(value, max_length=1000) or "UNSPECIFIED_ERROR"


class RetrospectiveFixRecord(BaseModel):
    """Evidence-backed fix or recovery observed after an error (never fabricated)."""

    model_config = ConfigDict(extra="forbid")

    fix_id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    resolved_error_code: str | None = None
    evidence_id: EvidenceId | None = None
    artifact_id: ArtifactId | None = None
    execution_id: str | None = None
    run_id: RunId | None = None
    source: Literal[
        "tool_retry_recovery",
        "run_retry_recovery",
        "qa_reverification",
        "conflict_decision",
        "evidence_record",
        "manual_input",
    ]
    recorded_at: datetime | None = None

    @field_validator("description")
    @classmethod
    def _scrub_desc(cls, value: str) -> str:
        return scrub_text(value, max_length=1000) or "Observed recovery."


class RetrospectiveProviderUsage(BaseModel):
    """Non-secret Provider/Connection/Model attribution captured from Runs/Sessions."""

    model_config = ConfigDict(extra="forbid")

    provider_id: SnakeId
    connection_id: SnakeId | None = None
    model_id: str | None = None
    run_id: RunId | None = None


class SkillUsageRecord(BaseModel):
    """Truthful Skill status distinguishing declared, allowed, invoked, and executed facts."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=128)
    agent_id: AgentId | None = None
    declared_in_capabilities: bool = False
    allowed_in_manifest: bool = False
    approved_in_catalog: bool = False
    invoked: bool = False
    participated_in_execution: bool = False
    outcome: SkillOutcome = "NOT_INVOKED"
    evidence_event_id: EventId | None = None
    notes: str | None = Field(default=None, max_length=500)


class TaskExecutionSummary(BaseModel):
    """P10.01 canonical, data-driven Task retrospective and execution history record."""

    model_config = ConfigDict(extra="forbid")

    retrospective_id: str
    retrospective_version: int = Field(ge=1)
    task_id: TaskId
    run_id: RunId | None = None
    run_ids: list[RunId] = Field(default_factory=list)
    session_ids: list[SessionId] = Field(default_factory=list)
    agent_ids: list[AgentId] = Field(default_factory=list)
    departments_involved: list[DepartmentId] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    duration_source: DurationSource = "UNAVAILABLE"
    execution_status: str = Field(min_length=1, max_length=64)
    qa_verification_status: ArtifactVerificationStatus | Literal["UNVERIFIED"] = "UNVERIFIED"
    steps: list[RetrospectiveStepRecord] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    tool_summaries: list[RetrospectiveToolSummary] = Field(default_factory=list)
    total_tool_calls: int = Field(ge=0)
    tool_execution_ids: list[str] = Field(default_factory=list)
    failed_tool_ids: list[str] = Field(default_factory=list)
    errors_observed: list[RetrospectiveErrorRecord] = Field(default_factory=list)
    fixes_applied: list[RetrospectiveFixRecord] = Field(default_factory=list)
    artifact_ids: list[ArtifactId] = Field(default_factory=list)
    output_paths: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    provider_refs: list[RetrospectiveProviderUsage] = Field(default_factory=list)
    cost_usd: float | None = Field(default=None, ge=0.0)
    cost_status: CostStatus = "UNAVAILABLE_FROM_PROVIDER"
    skill_usage_status: SkillUsageStatus = "UNAVAILABLE_NO_SKILL_DATA"
    skill_usages: list[SkillUsageRecord] = Field(default_factory=list)
    skills_invoked: list[str] = Field(default_factory=list)
    timeline_observed_tools: list[str] = Field(default_factory=list)
    sources_consulted: list[str] = Field(default_factory=list)
    pattern_tag: str | None = None
    source_fingerprint: Sha256Hex
    recorded_at: datetime
    updated_at: datetime
    audit_event_id: EventId

    @field_validator("retrospective_id")
    @classmethod
    def _validate_ret_id(cls, value: str) -> str:
        return validate_retrospective_id(value)

    @field_validator("source_fingerprint")
    @classmethod
    def _validate_fp(cls, value: str) -> str:
        return validate_sha256_hex(value)

    @field_validator("recorded_at", "updated_at")
    @classmethod
    def _validate_required_ts(cls, value: datetime, info: object) -> datetime:
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class AgentSkillSnapshot(BaseModel):
    """Read-only Agent capability vs allowed-skill snapshot gathered from Agent Registry."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    department: DepartmentId | Literal["orchestration"]
    declared_capabilities: list[CapabilityId] = Field(default_factory=list)
    skills_allowed: list[str] = Field(default_factory=list)


class WorkerStepSnapshot(BaseModel):
    """Read-only Worker Session binding snapshot without importing orchestration models."""

    model_config = ConfigDict(extra="forbid")

    session_id: SessionId
    run_id: RunId
    agent_id: AgentId
    step_id: str
    checklist_item_id: str | None = None
    provider_id: SnakeId
    connection_id: SnakeId | None = None
    model_id: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskArtifactFact(BaseModel):
    """Normalized read-only Artifact fact combining Vault and legacy Metadata rows."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId
    run_id: RunId
    owner_agent_id: AgentId
    relative_path: str | None = None
    department: DepartmentId | None = None
    verification_status: ArtifactVerificationStatus = "UNVERIFIED"
    evidence_reference: str | None = None
    created_at: datetime | None = None


class TaskEvidenceFact(BaseModel):
    """Normalized immutable Evidence record fact linked to a Task."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: EvidenceId
    evidence_kind: str
    run_id: RunId | None = None
    artifact_id: ArtifactId | None = None
    source_url: str | None = None
    summary: str
    created_at: datetime


class ResolvedConflictFact(BaseModel):
    """Normalized resolved worker conflict or Master decision trace linked to a Task."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    run_id: RunId
    resolution_note: str
    decision_id: str | None = None
    resolved_at: datetime


class TaskExecutionEvidenceBundle(BaseModel):
    """Complete read-only bundle of genuine persisted facts for one Task."""

    model_config = ConfigDict(extra="forbid")

    task: Task
    runs: list[Run] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)
    worker_steps: list[WorkerStepSnapshot] = Field(default_factory=list)
    checklist_items: list[ChecklistItem] = Field(default_factory=list)
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list)
    journal_entries: list[JournalEntry] = Field(default_factory=list)
    artifacts: list[TaskArtifactFact] = Field(default_factory=list)
    evidence_records: list[TaskEvidenceFact] = Field(default_factory=list)
    resolved_conflicts: list[ResolvedConflictFact] = Field(default_factory=list)
    agent_snapshots: list[AgentSkillSnapshot] = Field(default_factory=list)
    sources_consulted: list[str] = Field(default_factory=list)


class CompileTaskRetrospectiveRequest(BaseModel):
    """Input request instructing Process Department to compile a data-driven Task retrospective."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    expected_record_version: int | None = Field(default=None, ge=1)
    workspace_root: str | None = Field(default=None, min_length=1, max_length=512)
    target_path: str | None = Field(default=None, min_length=1, max_length=512)
    extra_tool_executions: list[ToolExecutionRecord] = Field(default_factory=list)
    extra_errors: list[str] = Field(default_factory=list, max_length=32)
    extra_fixes: list[str] = Field(default_factory=list, max_length=32)
    pattern_tag: str | None = Field(default=None, max_length=64)
    timestamp: datetime

    @field_validator("extra_errors", "extra_fixes")
    @classmethod
    def _validate_extra_notes(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "extra_notes")
        return [ensure_not_secret(v, field_name) for v in values]

    @field_validator("pattern_tag")
    @classmethod
    def _validate_tag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_snake_id(value, "pattern_tag")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class TaskRetrospectiveRevision(BaseModel):
    """Immutable historical snapshot entry for a persisted Task retrospective."""

    model_config = ConfigDict(extra="forbid")

    retrospective_id: str
    task_id: TaskId
    revision_number: int = Field(ge=1)
    source_fingerprint: Sha256Hex
    summary: TaskExecutionSummary
    legacy_record: ProcessRetrospectiveRecord | None = None
    recorded_by: str
    recorded_at: datetime
    audit_event_id: EventId


class StoredTaskRetrospective(BaseModel):
    """Persisted aggregate view of one canonical Task retrospective."""

    model_config = ConfigDict(extra="forbid")

    retrospective_id: str
    task_id: TaskId
    primary_run_id: RunId | None = None
    execution_status: str
    active_revision: int = Field(ge=1)
    record_version: int = Field(ge=1)
    source_fingerprint: Sha256Hex
    summary: TaskExecutionSummary
    legacy_record: ProcessRetrospectiveRecord | None = None
    linked_run_ids: list[RunId] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    last_audit_event_id: EventId


class TaskRetrospectiveOutcome(BaseModel):
    """Result returned after compiling or deduplicating a Task retrospective."""

    model_config = ConfigDict(extra="forbid")

    stored: StoredTaskRetrospective
    summary: TaskExecutionSummary
    created: bool
    revision_created: bool
    deduplicated: bool


@runtime_checkable
class ExecutionHistoryReaderPort(Protocol):
    """Read-only port isolating ``core.departments.process`` from concrete SQLite queries."""

    def gather_task_execution_bundle(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
    ) -> TaskExecutionEvidenceBundle | None:
        """Load factual Task, Run, Session, Tool, Event, Artifact, and Evidence records."""


@runtime_checkable
class ProcessRetrospectiveRepository(Protocol):
    """Persistence contract for durable, idempotent, versioned Task retrospectives."""

    def upsert_retrospective_atomic(
        self,
        *,
        summary: TaskExecutionSummary,
        legacy_record: ProcessRetrospectiveRecord | None,
        actor_id: str,
        audit_event: Event,
        expected_record_version: int | None = None,
    ) -> tuple[StoredTaskRetrospective, bool, bool]:
        """Atomically insert or version-advance a retrospective with anti-duplication."""

    def get_by_task(self, task_id: str, *, revision: int | None = None) -> StoredTaskRetrospective | None:
        """Fetch stored retrospective for ``task_id`` (optionally pinned to ``revision``)."""

    def get_by_retrospective_id(
        self,
        retrospective_id: str,
        *,
        revision: int | None = None,
    ) -> StoredTaskRetrospective | None:
        """Fetch stored retrospective by ``retrospective_id``."""

    def get_by_run(self, run_id: str) -> StoredTaskRetrospective | None:
        """Fetch the Task retrospective linked to ``run_id``."""

    def list_retrospectives(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        execution_status: str | None = None,
        limit: int = 200,
    ) -> list[StoredTaskRetrospective]:
        """List stored Task retrospectives filtered by task, run, or execution status."""

    def list_revisions(self, retrospective_id: str) -> list[TaskRetrospectiveRevision]:
        """List immutable historical revisions for ``retrospective_id``."""


PatternCategory = Literal["repeated_error", "redundant_operations", "missing_skill_gap"]
ImprovementStatus = Literal["SUGGESTED", "CANDIDATE_CREATED", "RESOLVED", "DISMISSED"]


def new_improvement_id() -> str:
    """Generate canonical P10.02 SuggestedImprovement identifier (`imp_*`)."""
    return f"imp_{uuid.uuid4().hex[:14]}"


class PatternAnalysisRequest(BaseModel):
    """Input configuration to scan persisted Task retrospectives for verifiable patterns."""

    model_config = ConfigDict(extra="forbid")

    task_ids: list[TaskId] = Field(default_factory=list, max_length=500)
    department: DepartmentId | None = None
    min_occurrences: int = Field(default=3, ge=2, le=100)
    redundant_call_threshold: int = Field(default=2, ge=2, le=50)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ImprovementMetrics(BaseModel):
    """Strictly measured cross-task quantitative evidence backing a suggested improvement."""

    model_config = ConfigDict(extra="forbid")

    occurrence_count: int = Field(ge=2)
    distinct_task_count: int = Field(ge=2)
    scanned_task_count: int = Field(ge=2)
    affected_task_ratio: float = Field(ge=0.0, le=1.0)
    error_occurrences: int = Field(default=0, ge=0)
    retry_count_total: int = Field(default=0, ge=0)
    redundant_extra_calls: int = Field(default=0, ge=0)
    avg_duration_ms: float | None = Field(default=None, ge=0.0)
    total_duration_ms: int | None = Field(default=None, ge=0)
    cost_usd_total: float | None = Field(default=None, ge=0.0)


class SuggestedImprovement(BaseModel):
    """Durable, evidence-backed continuous improvement suggestion mined across multiple Tasks."""

    model_config = ConfigDict(extra="forbid")

    improvement_id: str
    pattern_type: PatternCategory
    pattern_key: str
    suggested_skill_id: str
    title: str = Field(min_length=3, max_length=200)
    rationale: str = Field(min_length=5, max_length=2000)
    recommended_action: str = Field(min_length=5, max_length=1000)
    evidence_task_ids: list[TaskId] = Field(min_length=2)
    evidence_retrospective_ids: list[str] = Field(min_length=2)
    evidence_run_ids: list[RunId] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    observed_error_codes: list[str] = Field(default_factory=list)
    observed_tools: list[str] = Field(default_factory=list)
    observed_fixes: list[str] = Field(default_factory=list)
    departments_involved: list[DepartmentId] = Field(default_factory=list)
    metrics: ImprovementMetrics
    status: ImprovementStatus = "SUGGESTED"
    source_fingerprint: Sha256Hex
    record_version: int = Field(default=1, ge=1)
    detected_at: datetime
    updated_at: datetime
    audit_event_id: EventId

    @field_validator("pattern_key", "suggested_skill_id")
    @classmethod
    def _validate_keys(cls, value: str, info: object) -> str:
        return validate_snake_id(value, getattr(info, "field_name", "pattern_key"))

    @field_validator("title", "rationale", "recommended_action")
    @classmethod
    def _scrub_prose(cls, value: str, info: object) -> str:
        return scrub_text(value, max_length=2000) or "Pattern observed."

    @field_validator("observed_error_codes", "observed_tools", "observed_fixes")
    @classmethod
    def _scrub_lists(cls, values: list[str]) -> list[str]:
        return [scrub_text(v, max_length=500) for v in values if scrub_text(v, max_length=500)]

    @field_validator("detected_at", "updated_at")
    @classmethod
    def _validate_dts(cls, value: datetime, info: object) -> datetime:
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class PatternAnalysisOutcome(BaseModel):
    """Structured result returned by ``analyze_patterns_and_suggest_improvements``."""

    model_config = ConfigDict(extra="forbid")

    scanned_retrospective_count: int = Field(ge=0)
    created_improvements: list[SuggestedImprovement] = Field(default_factory=list)
    updated_improvements: list[SuggestedImprovement] = Field(default_factory=list)
    unchanged_improvements: list[SuggestedImprovement] = Field(default_factory=list)
    all_improvements: list[SuggestedImprovement] = Field(default_factory=list)
    audit_event_id: EventId | None = None


@runtime_checkable
class ProcessImprovementRepository(Protocol):
    """Persistence contract for durable P10.02 multi-task pattern improvements."""

    def upsert_improvement_atomic(
        self,
        *,
        improvement: SuggestedImprovement,
        actor_id: str,
        audit_event: Event,
    ) -> tuple[SuggestedImprovement, bool, bool]:
        """Atomically persist or deduplicate a pattern improvement linked to real Tasks."""

    def get_improvement(self, improvement_id: str) -> SuggestedImprovement | None:
        """Fetch persisted ``SuggestedImprovement`` by ``improvement_id``."""

    def get_improvement_by_pattern(
        self,
        pattern_type: PatternCategory,
        pattern_key: str,
    ) -> SuggestedImprovement | None:
        """Fetch persisted ``SuggestedImprovement`` by unique ``(pattern_type, pattern_key)``."""

    def list_improvements(
        self,
        *,
        pattern_type: PatternCategory | None = None,
        status: ImprovementStatus | None = None,
    ) -> list[SuggestedImprovement]:
        """List persisted improvement suggestions ordered by update time."""

    def update_improvement_status(
        self,
        improvement_id: str,
        *,
        status: ImprovementStatus,
        updated_at: datetime,
    ) -> SuggestedImprovement:
        """Update lifecycle status of an existing improvement suggestion."""
