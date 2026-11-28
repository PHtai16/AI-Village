"""Pure AST domain contracts, models, and ports for P11.05 Safe Post-Task Cleanup & Process Handoff."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    CleanupId,
    EventId,
    ProcessHandoffId,
    RunId,
    TaskId,
    ensure_not_secret,
    new_cleanup_id,
    new_process_handoff_id,
    scrub_secret_text,
    validate_aware_datetime,
    validate_safe_metadata,
)
from core.contracts.run import Run
from core.contracts.task import Task
from core.departments._common import DepartmentBehaviorError, DepartmentGovernanceError
from core.departments.process_models import CompileTaskRetrospectiveRequest, TaskRetrospectiveOutcome
from core.departments.resources_models import TaskResourceUsageSummary
from core.governance.models import TrustedAuthContext

CleanupStatus = Literal["COMPLETED", "PARTIAL", "BLOCKED", "SKIPPED"]
CleanupResourceKind = Literal[
    "TEMP_FILE",
    "TEMP_DIR",
    "EXECUTION_LEASE",
    "WORKER_SESSION",
    "ARTIFACT_VAULT",
    "EVIDENCE_RECORD",
    "LEDGER_ENTRY",
    "WORKSPACE_FILE",
]
CleanupAction = Literal[
    "DELETED",
    "RELEASED",
    "CLOSED",
    "PRESERVED",
    "BLOCKED_UNSAFE_PATH",
    "BLOCKED_SHARED_REF",
    "FAILED",
]
ProcessHandoffStatus = Literal[
    "DELIVERED_TO_PROCESS",
    "PENDING_PROCESS_LINK",
    "ALREADY_HANDED_OFF",
]


def _scrub_non_blank(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    scrubbed = scrub_secret_text(cleaned).strip()
    if not scrubbed:
        raise ValueError(f"{field_name} must not resolve to blank text.")
    return scrubbed


class CleanupError(DepartmentBehaviorError):
    """Structured base error for P11.05 post-task cleanup and Process handoff."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(error_code, message)


class CleanupValidationError(CleanupError):
    """Raised when cleanup request parameters violate path or contract constraints."""


class CleanupReferenceError(CleanupError):
    """Raised when referenced Task or Run does not exist."""


class CleanupStateError(CleanupError):
    """Raised when attempting cleanup on a still-active non-terminal Task or Run."""


class CleanupGovernanceError(DepartmentGovernanceError):
    """Raised when actor lacks permission to execute cleanup or Process handoff."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(error_code, message)


class CleanupConflictError(CleanupError):
    """Raised on idempotency payload mismatch or duplicate Process handoff."""


class CleanupPersistenceError(CleanupError):
    """Raised when durable cleanup SQLite transaction fails or rolls back."""


class TempCleanupTarget(BaseModel):
    """Candidate temporary file or directory inside ``managed_temp_root`` submitted for cleanup."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    resource_kind: Literal["TEMP_FILE", "TEMP_DIR"] = "TEMP_FILE"
    description: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Cleanup path must not contain NUL bytes.")
        return ensure_not_secret(value, "path").strip()

    @field_validator("description")
    @classmethod
    def _validate_desc(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _scrub_non_blank(value, "description")


class TempDeleteExecutionResult(BaseModel):
    """Truthful outcome produced by ``ManagedTempCleanupExecutorPort`` for one path."""

    model_config = ConfigDict(extra="forbid")

    target_ref: str
    resource_kind: Literal["TEMP_FILE", "TEMP_DIR"]
    action: Literal["DELETED", "PRESERVED", "BLOCKED_UNSAFE_PATH", "BLOCKED_SHARED_REF", "FAILED"]
    reason: str
    size_bytes: int | None = Field(default=None, ge=0)


class CleanupItemRecord(BaseModel):
    """Append-only itemized record of every resource deleted, released, preserved, or blocked."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    cleanup_id: CleanupId
    resource_kind: CleanupResourceKind
    target_ref: str
    action: CleanupAction
    reason: str
    size_bytes: int | None = Field(default=None, ge=0)
    created_at: datetime


class CleanupInventorySummary(BaseModel):
    """Post-task cleanup & resource inventory strictly preserving unavailable metrics as None."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    deleted_temp_count: int = Field(ge=0)
    deleted_bytes_total: int = Field(ge=0)
    released_lease_count: int = Field(ge=0)
    preserved_artifact_count: int = Field(ge=0)
    preserved_evidence_count: int = Field(ge=0)
    preserved_ledger_count: int = Field(ge=0)
    blocked_safety_count: int = Field(ge=0)
    resource_summary: TaskResourceUsageSummary | None = None
    open_alerts_count: int = Field(ge=0)
    critical_alerts_count: int = Field(ge=0)


class PostTaskCleanupRequest(BaseModel):
    """Validated request to safely clean temporary resources, inventory usage, and notify Process."""

    model_config = ConfigDict(extra="forbid")

    cleanup_id: CleanupId = Field(default_factory=new_cleanup_id)
    handoff_id: ProcessHandoffId = Field(default_factory=new_process_handoff_id)
    task_id: TaskId
    run_id: RunId | None = None
    temp_targets: list[TempCleanupTarget] = Field(default_factory=list, max_length=256)
    preserve_paths: list[str] = Field(default_factory=list, max_length=256)
    force_partial_on_failed_run: bool = False
    deliver_to_process: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    note: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idem(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, "idempotency_key").strip()

    @field_validator("note")
    @classmethod
    def _validate_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _scrub_non_blank(value, "note")

    @field_validator("preserve_paths")
    @classmethod
    def _validate_preserves(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(v, "preserve_paths").strip() for v in values if v.strip()]

    @field_validator("metadata")
    @classmethod
    def _validate_meta(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_safe_metadata(value, "metadata")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ProcessLearningHandoffRecord(BaseModel):
    """Durable structured post-task handoff packet delivered to Process Department."""

    model_config = ConfigDict(extra="forbid")

    handoff_id: ProcessHandoffId
    cleanup_id: CleanupId
    task_id: TaskId
    run_id: RunId | None = None
    retrospective_id: str | None = None
    resource_summary: TaskResourceUsageSummary | None = None
    cleanup_inventory: CleanupInventorySummary
    alerts_summary: dict[str, Any]
    handoff_status: ProcessHandoffStatus
    audit_event_id: EventId
    created_by: str
    created_at: datetime


class PostTaskCleanupRecord(BaseModel):
    """Persisted master cleanup record for one finished Task or Run."""

    model_config = ConfigDict(extra="forbid")

    cleanup_id: CleanupId
    task_id: TaskId
    run_id: RunId | None = None
    task_status_snapshot: str
    cleanup_status: CleanupStatus
    idempotency_key: str | None = None
    request_fingerprint: str
    inventory: CleanupInventorySummary
    preserved_refs: list[str]
    handoff_id: ProcessHandoffId | None = None
    audit_event_id: EventId
    performed_by: str
    created_at: datetime
    updated_at: datetime
    record_version: int = Field(default=1, ge=1)


class PostTaskCleanupOutcome(BaseModel):
    """Complete result returned after safe cleanup, inventory compilation, and Process handoff."""

    model_config = ConfigDict(extra="forbid")

    cleanup: PostTaskCleanupRecord
    items: list[CleanupItemRecord]
    handoff: ProcessLearningHandoffRecord | None = None
    idempotent_replay: bool = False


class ProtectedRefsSnapshot(BaseModel):
    """Protected filesystem paths and DB artifact/evidence/ledger IDs that cleanup must preserve."""

    model_config = ConfigDict(extra="forbid")

    protected_paths: list[str]
    shared_paths: list[str]
    artifact_ids: list[str]
    shared_artifact_ids: list[str]
    evidence_ids: list[str]
    ledger_entry_ids: list[str]


@runtime_checkable
class ManagedTempCleanupExecutorPort(Protocol):
    """Boundary port enforcing managed_temp_root containment and symlink safety on disk."""

    def delete_managed_temp_target(
        self,
        raw_path: str,
        *,
        resource_kind: Literal["TEMP_FILE", "TEMP_DIR"],
        protected_paths: set[str],
        shared_paths: set[str],
    ) -> TempDeleteExecutionResult: ...


@runtime_checkable
class ProcessRetrospectiveCompilerPort(Protocol):
    """Optional port linking cleanup handoff to ``ProcessDepartmentService.compile_task_retrospective``."""

    def compile_task_retrospective(
        self,
        request: CompileTaskRetrospectiveRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> TaskRetrospectiveOutcome: ...


@runtime_checkable
class CleanupTransactionContext(Protocol):
    """Transaction-scoped persistence contract executed atomically inside SQLite."""

    def get_task(self, task_id: str) -> Task | None: ...

    def get_run(self, run_id: str) -> Run | None: ...

    def find_cleanup_by_idempotency(
        self, task_id: str, run_id: str | None, idempotency_key: str
    ) -> PostTaskCleanupRecord | None: ...

    def list_cleanup_items(self, cleanup_id: str) -> list[CleanupItemRecord]: ...

    def get_handoff_by_id(self, handoff_id: str) -> ProcessLearningHandoffRecord | None: ...

    def get_handoff_for_task_run(
        self, task_id: str, run_id: str | None
    ) -> ProcessLearningHandoffRecord | None: ...

    def collect_protected_refs(self, task_id: str) -> ProtectedRefsSnapshot: ...

    def release_lingering_leases(
        self, task_id: str, *, run_id: str | None, released_at: datetime, reason: str
    ) -> list[str]: ...

    def summarize_task_resources(self, task_id: str) -> TaskResourceUsageSummary | None: ...

    def get_alerts_snapshot(self, task_id: str) -> dict[str, Any]: ...

    def get_retrospective_id_for_task(self, task_id: str) -> str | None: ...

    def insert_cleanup_record(self, record: PostTaskCleanupRecord) -> PostTaskCleanupRecord: ...

    def insert_cleanup_items(self, items: list[CleanupItemRecord]) -> list[CleanupItemRecord]: ...

    def insert_process_handoff(
        self, handoff: ProcessLearningHandoffRecord
    ) -> ProcessLearningHandoffRecord: ...

    def append_event(self, event: Event) -> Event: ...


@runtime_checkable
class CleanupRepository(Protocol):
    """Repository port isolating ``PostTaskCleanupService`` from SQLite details."""

    def execute_transaction(
        self,
        processor: Callable[[CleanupTransactionContext], Any],
    ) -> Any: ...

    def get_cleanup(self, cleanup_id: str) -> PostTaskCleanupOutcome | None: ...

    def list_cleanups_for_task(self, task_id: str) -> list[PostTaskCleanupRecord]: ...

    def get_handoff_for_task(
        self, task_id: str, *, run_id: str | None = None
    ) -> ProcessLearningHandoffRecord | None: ...
