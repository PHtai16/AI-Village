"""Pure AST domain contracts, policies, validation helpers, and ports for P11.04 Resource Ledger."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    EventId,
    NotificationId,
    ProjectId,
    ResourceAlertId,
    ResourceLedgerId,
    ResourcePolicyId,
    RunId,
    TaskId,
    ensure_not_secret,
    new_resource_alert_id,
    new_resource_ledger_id,
    new_resource_policy_id,
    scrub_secret_text,
    validate_aware_datetime,
    validate_safe_metadata,
)
from core.contracts.references import NotificationRef
from core.contracts.run import Run, RunTelemetryStatus
from core.contracts.task import Task
from core.departments._common import DepartmentBehaviorError, DepartmentGovernanceError
from core.departments.reception_models import ActionInboxItem, ActionInboxStatus

ResourceScopeKind = Literal["TASK", "PROJECT", "CONNECTION", "QUOTA_BUCKET", "PROVIDER_MODEL"]
QuotaScopeKind = Literal[
    "CONNECTION_VERIFIED",
    "PROJECT_SHARED",
    "ACCOUNT_SHARED",
    "ORG_SHARED",
    "UNKNOWN_SHARED",
]
DurationSource = Literal["PROVIDER_MEASURED", "ADAPTER_MEASURED", "HOST_SAMPLED", "UNAVAILABLE"]
MemorySource = Literal["HOST_SAMPLED", "CONTAINER_MEASURED", "PROVIDER_REPORTED", "UNAVAILABLE"]
TokenUsageStatus = Literal["VERIFIED_MEASURED", "ESTIMATED", "UNAVAILABLE_FROM_PROVIDER"]
CostSource = Literal[
    "PROVIDER_BILLED",
    "ESTIMATED_LOCAL_PRICING_TABLE",
    "LOCAL_FREE_ZERO",
    "UNAVAILABLE_FROM_PROVIDER",
]
ResourceAlertCategory = Literal["BUDGET_COST", "DURATION", "MEMORY", "TOKENS", "QUOTA"]
ResourceAlertSeverity = Literal["WARNING", "CRITICAL"]
ResourceAlertStatus = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "DISMISSED"]
QuotaIndependenceStatus = Literal["VERIFIED_INDEPENDENT", "QUOTA_SHARED_OR_UNPROVEN"]


def _normalize_optional_slug(value: str | None, field_name: str) -> str | None:
    """Normalize optional provider/connection/model/project/account slugs without leaking secrets."""
    if value is None:
        return None
    cleaned = ensure_not_secret(value, field_name).strip().lower()
    if not cleaned:
        return None
    return cleaned


def _scrub_non_blank(value: str, field_name: str) -> str:
    """Validate non-blank text and scrub credential patterns before persistence."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    scrubbed = scrub_secret_text(cleaned).strip()
    if not scrubbed:
        raise ValueError(f"{field_name} must not be blank.")
    return scrubbed


def _scrub_optional_text(value: str | None, field_name: str) -> str | None:
    """Normalize optional prose/label strings with credential scrubbing."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    scrubbed = scrub_secret_text(cleaned).strip()
    if not scrubbed:
        raise ValueError(f"{field_name} must not resolve to blank text.")
    return scrubbed


def derive_quota_bucket_key(
    *,
    provider_id: str,
    connection_id: str | None,
    project_ref: str | None,
    account_ref: str | None,
    quota_scope_kind: QuotaScopeKind,
    independent_quota_proof: bool,
) -> tuple[str, QuotaScopeKind, bool]:
    """Compute canonical shared quota bucket key without assuming unverified key independence."""
    norm_provider = ensure_not_secret(provider_id, "provider_id").strip().lower()
    norm_conn = _normalize_optional_slug(connection_id, "connection_id")
    norm_proj = _normalize_optional_slug(project_ref, "project_ref")
    norm_acct = _normalize_optional_slug(account_ref, "account_ref")
    if (
        quota_scope_kind == "CONNECTION_VERIFIED"
        and independent_quota_proof
        and norm_conn is not None
    ):
        return f"conn:{norm_provider}:{norm_conn}", "CONNECTION_VERIFIED", True
    if norm_proj is not None:
        return f"project:{norm_provider}:{norm_proj}", "PROJECT_SHARED", False
    if norm_acct is not None:
        return f"account:{norm_provider}:{norm_acct}", "ACCOUNT_SHARED", False
    if quota_scope_kind == "ORG_SHARED":
        return f"org:{norm_provider}", "ORG_SHARED", False
    return f"unproven_shared:{norm_provider}", "UNKNOWN_SHARED", False


class ResourceLedgerError(DepartmentBehaviorError):
    """Structured base error raised for P11.04 resource ledger & budget operations."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(error_code, message)


class ResourceLedgerValidationError(ResourceLedgerError):
    """Raised when resource usage payloads, telemetry honesty, or policies fail validation."""


class ResourceLedgerReferenceError(ResourceLedgerError):
    """Raised when referenced Task, Run, Approval, Alert, or Budget Policy does not exist."""


class ResourceLedgerGovernanceError(DepartmentGovernanceError):
    """Raised when actor permissions, ownership, or approval rules forbid a resource operation."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(error_code, message)


class ResourceLedgerConflictError(ResourceLedgerError):
    """Raised on idempotency payload mismatch or optimistic version conflict."""


class ResourceLedgerPersistenceError(ResourceLedgerError):
    """Raised when durable SQLite ledger transactions fail or roll back."""


class RegisterBudgetPolicyRequest(BaseModel):
    """Validated request to register or update a budget/quota threshold policy."""

    model_config = ConfigDict(extra="forbid")

    policy_id: ResourcePolicyId = Field(default_factory=new_resource_policy_id)
    scope_kind: ResourceScopeKind
    scope_ref: str = Field(min_length=1, max_length=256)
    provider_id: str | None = Field(default=None, min_length=1, max_length=128)
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_ref: str | None = Field(default=None, min_length=1, max_length=128)
    account_ref: str | None = Field(default=None, min_length=1, max_length=128)
    quota_scope_kind: QuotaScopeKind = "UNKNOWN_SHARED"
    independent_quota_proof: bool = False
    budget_cost_usd_limit: float | None = Field(default=None, gt=0.0)
    duration_ms_limit: int | None = Field(default=None, gt=0)
    memory_mb_limit: float | None = Field(default=None, gt=0.0)
    total_tokens_limit: int | None = Field(default=None, gt=0)
    quota_units_limit: int | None = Field(default=None, gt=0)
    warning_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)
    critical_ratio: float = Field(default=1.0, gt=0.0, le=1.0)
    requires_operator_approval_above_critical: bool = True
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    policy_note: str = Field(min_length=1, max_length=500)
    enabled: bool = True
    timestamp: datetime

    @field_validator("scope_ref", "approval_id", "policy_note")
    @classmethod
    def _validate_texts(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "policy_field")
        if field_name == "policy_note":
            return _scrub_non_blank(value, field_name)
        return ensure_not_secret(value, field_name).strip()

    @field_validator("provider_id", "connection_id", "model_id", "project_ref", "account_ref")
    @classmethod
    def _validate_slugs(cls, value: str | None, info: object) -> str | None:
        return _normalize_optional_slug(value, getattr(info, "field_name", "slug"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")

    @model_validator(mode="after")
    def _validate_policy(self) -> RegisterBudgetPolicyRequest:
        if self.critical_ratio <= self.warning_ratio:
            raise ValueError("critical_ratio must be strictly greater than warning_ratio.")
        has_any_limit = any(
            val is not None
            for val in (
                self.budget_cost_usd_limit,
                self.duration_ms_limit,
                self.memory_mb_limit,
                self.total_tokens_limit,
                self.quota_units_limit,
            )
        )
        if not has_any_limit:
            raise ValueError("At least one budget or quota threshold limit must be specified.")
        if self.scope_kind in ("CONNECTION", "QUOTA_BUCKET", "PROVIDER_MODEL") and not self.provider_id:
            raise ValueError(f"provider_id is required when scope_kind is {self.scope_kind!r}.")
        if self.quota_units_limit is not None and not self.provider_id:
            raise ValueError("provider_id is required when configuring quota_units_limit.")
        return self


class ResourceBudgetPolicy(BaseModel):
    """Persisted budget/quota policy bound to a task, project, connection, bucket, or model."""

    model_config = ConfigDict(extra="forbid")

    policy_id: ResourcePolicyId
    scope_kind: ResourceScopeKind
    scope_ref: str
    provider_id: str | None = None
    connection_id: str | None = None
    model_id: str | None = None
    project_ref: str | None = None
    account_ref: str | None = None
    quota_scope_kind: QuotaScopeKind
    quota_bucket_key: str | None = None
    independent_quota_proof: bool = False
    budget_cost_usd_limit: float | None = None
    duration_ms_limit: int | None = None
    memory_mb_limit: float | None = None
    total_tokens_limit: int | None = None
    quota_units_limit: int | None = None
    warning_ratio: float
    critical_ratio: float
    requires_operator_approval_above_critical: bool = True
    approval_id: str | None = None
    policy_note: str
    enabled: bool = True
    record_version: int = Field(default=1, ge=1)
    created_by: str
    created_at: datetime
    updated_at: datetime


class RecordResourceUsageRequest(BaseModel):
    """Validated append-only ledger submission with strict measured vs estimated vs unavailable separation."""

    model_config = ConfigDict(extra="forbid")

    ledger_entry_id: ResourceLedgerId = Field(default_factory=new_resource_ledger_id)
    task_id: TaskId
    run_id: RunId | None = None
    project_id: ProjectId | None = None
    agent_id: AgentId | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_ref: str | None = Field(default=None, min_length=1, max_length=128)
    account_ref: str | None = Field(default=None, min_length=1, max_length=128)
    quota_scope_kind: QuotaScopeKind = "UNKNOWN_SHARED"
    independent_quota_proof: bool = False
    duration_ms: int | None = Field(default=None, ge=0)
    provider_duration_ms: int | None = Field(default=None, ge=0)
    duration_source: DurationSource = "UNAVAILABLE"
    memory_mb: float | None = Field(default=None, ge=0.0)
    peak_memory_mb: float | None = Field(default=None, ge=0.0)
    memory_source: MemorySource = "UNAVAILABLE"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    token_usage_status: TokenUsageStatus = "UNAVAILABLE_FROM_PROVIDER"
    actual_cost_usd: float | None = Field(default=None, ge=0.0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    cost_source: CostSource = "UNAVAILABLE_FROM_PROVIDER"
    pricing_table_ref: str | None = Field(default=None, min_length=1, max_length=128)
    is_estimated: bool = False
    estimate_label: str | None = Field(default=None, min_length=1, max_length=256)
    quota_units_used: int | None = Field(default=None, ge=0)
    quota_limit_units: int | None = Field(default=None, ge=0)
    quota_remaining_units: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    note: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime

    @field_validator("provider_id", "connection_id", "model_id", "project_ref", "account_ref")
    @classmethod
    def _validate_slugs(cls, value: str | None, info: object) -> str | None:
        field_name = getattr(info, "field_name", "slug")
        normalized = _normalize_optional_slug(value, field_name)
        if field_name == "provider_id" and normalized is None:
            raise ValueError("provider_id must not be blank.")
        return normalized

    @field_validator("session_id", "idempotency_key", "pricing_table_ref")
    @classmethod
    def _validate_handles(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        cleaned = ensure_not_secret(value, getattr(info, "field_name", "handle")).strip()
        if not cleaned:
            raise ValueError(f"{getattr(info, 'field_name', 'handle')} must not be blank.")
        return cleaned

    @field_validator("estimate_label", "note")
    @classmethod
    def _validate_prose(cls, value: str | None, info: object) -> str | None:
        return _scrub_optional_text(value, getattr(info, "field_name", "text"))

    @field_validator("metadata")
    @classmethod
    def _validate_meta(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_safe_metadata(value, "metadata")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")

    @model_validator(mode="after")
    def _validate_truthfulness(self) -> RecordResourceUsageRequest:
        if self.duration_source == "UNAVAILABLE":
            if self.duration_ms is not None or self.provider_duration_ms is not None:
                raise ValueError(
                    "duration_ms and provider_duration_ms must stay None when duration_source is 'UNAVAILABLE'."
                )
        elif self.duration_ms is None and self.provider_duration_ms is None:
            raise ValueError(
                "At least one of duration_ms or provider_duration_ms must be set when duration_source is measured."
            )

        if self.memory_source == "UNAVAILABLE":
            if self.memory_mb is not None or self.peak_memory_mb is not None:
                raise ValueError(
                    "memory_mb and peak_memory_mb must stay None when memory_source is 'UNAVAILABLE'."
                )
        elif self.memory_mb is None and self.peak_memory_mb is None:
            raise ValueError(
                "At least one of memory_mb or peak_memory_mb must be set when memory_source is measured."
            )

        token_fields = (
            self.input_tokens,
            self.output_tokens,
            self.thinking_tokens,
            self.cached_tokens,
            self.total_tokens,
        )
        if self.token_usage_status == "UNAVAILABLE_FROM_PROVIDER":
            if any(v is not None for v in token_fields):
                raise ValueError(
                    "Token counters must stay None when token_usage_status is 'UNAVAILABLE_FROM_PROVIDER'."
                )
        elif all(v is None for v in token_fields):
            raise ValueError(
                "At least one token metric is required when token_usage_status is measured or estimated."
            )

        if self.cost_source == "UNAVAILABLE_FROM_PROVIDER":
            if self.actual_cost_usd is not None or self.estimated_cost_usd is not None:
                raise ValueError(
                    "actual_cost_usd and estimated_cost_usd must stay None when cost_source is 'UNAVAILABLE_FROM_PROVIDER'."
                )
        elif self.cost_source == "PROVIDER_BILLED":
            if self.actual_cost_usd is None or self.estimated_cost_usd is not None:
                raise ValueError(
                    "PROVIDER_BILLED requires actual_cost_usd and forbids setting estimated_cost_usd."
                )
        elif self.cost_source == "LOCAL_FREE_ZERO":
            if self.actual_cost_usd is None or self.actual_cost_usd != 0.0 or self.estimated_cost_usd is not None:
                raise ValueError(
                    "LOCAL_FREE_ZERO requires actual_cost_usd == 0.0 and estimated_cost_usd is None."
                )
        elif self.cost_source == "ESTIMATED_LOCAL_PRICING_TABLE":
            if self.actual_cost_usd is not None or self.estimated_cost_usd is None:
                raise ValueError(
                    "ESTIMATED_LOCAL_PRICING_TABLE must populate estimated_cost_usd and keep actual_cost_usd=None."
                )
            if not self.pricing_table_ref:
                raise ValueError("pricing_table_ref is required for ESTIMATED_LOCAL_PRICING_TABLE.")
            if not self.is_estimated or not self.estimate_label:
                raise ValueError("Estimated cost entries must set is_estimated=True and a clear estimate_label.")

        if self.token_usage_status == "ESTIMATED" and (not self.is_estimated or not self.estimate_label):
            raise ValueError("Estimated token usage must set is_estimated=True and a clear estimate_label.")

        if self.is_estimated and not self.estimate_label:
            raise ValueError("is_estimated=True requires a non-empty estimate_label.")

        return self


class ResourceLedgerEntry(BaseModel):
    """Persisted immutable resource ledger row attached to Task/Run and Provider/Connection/Model."""

    model_config = ConfigDict(extra="forbid")

    ledger_entry_id: ResourceLedgerId
    task_id: TaskId
    run_id: RunId | None = None
    project_id: ProjectId | None = None
    agent_id: AgentId | None = None
    session_id: str | None = None
    provider_id: str
    connection_id: str | None = None
    model_id: str | None = None
    project_ref: str | None = None
    account_ref: str | None = None
    quota_scope_kind: QuotaScopeKind
    quota_bucket_key: str
    quota_independence_proven: bool = False
    duration_ms: int | None = None
    provider_duration_ms: int | None = None
    duration_source: DurationSource
    memory_mb: float | None = None
    peak_memory_mb: float | None = None
    memory_source: MemorySource
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_status: TokenUsageStatus
    actual_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    cost_source: CostSource
    pricing_table_ref: str | None = None
    telemetry_status: RunTelemetryStatus
    is_estimated: bool = False
    estimate_label: str | None = None
    quota_units_used: int | None = None
    quota_limit_units: int | None = None
    quota_remaining_units: int | None = None
    idempotency_key: str | None = None
    request_fingerprint: str
    note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_by: str
    audit_event_id: EventId
    recorded_at: datetime


class ResourceThresholdAlert(BaseModel):
    """Deduplicated persistent budget or quota threshold alert with recovery & ACK/RESOLVE lifecycle."""

    model_config = ConfigDict(extra="forbid")

    alert_id: ResourceAlertId
    dedup_key: str
    policy_id: ResourcePolicyId
    alert_category: ResourceAlertCategory
    scope_kind: ResourceScopeKind
    scope_ref: str
    task_id: TaskId | None = None
    run_id: RunId | None = None
    provider_id: str | None = None
    connection_id: str | None = None
    model_id: str | None = None
    quota_bucket_key: str | None = None
    severity: ResourceAlertSeverity
    status: ResourceAlertStatus = "OPEN"
    observed_value: float = Field(ge=0.0)
    threshold_limit: float = Field(gt=0.0)
    utilization_ratio: float = Field(ge=0.0)
    is_estimated_basis: bool = False
    estimate_label: str | None = None
    summary: str
    recommended_action: str
    occurrence_count: int = Field(ge=1)
    notification_id: NotificationId | None = None
    action_inbox_item_id: str | None = None
    master_notified: bool = False
    requires_operator_approval: bool = False
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    acknowledgement_note: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    audit_event_id: EventId
    record_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class EvaluatedAlertOutcome(BaseModel):
    """Result of threshold evaluation for one triggered or updated alert."""

    model_config = ConfigDict(extra="forbid")

    alert: ResourceThresholdAlert
    newly_created: bool
    escalated_to_critical: bool = False
    notification_suppressed: bool = False


class RecordResourceUsageOutcome(BaseModel):
    """Complete result returned after recording a resource usage entry and evaluating thresholds."""

    model_config = ConfigDict(extra="forbid")

    entry: ResourceLedgerEntry
    idempotent_replay: bool = False
    triggered_alerts: list[EvaluatedAlertOutcome] = Field(default_factory=list)
    recovered_alert_ids: list[ResourceAlertId] = Field(default_factory=list)


class ResourceLedgerQuery(BaseModel):
    """Filter criteria for querying persisted resource ledger entries."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId | None = None
    run_id: RunId | None = None
    provider_id: str | None = None
    connection_id: str | None = None
    model_id: str | None = None
    quota_bucket_key: str | None = None


class TaskResourceUsageSummary(BaseModel):
    """Honest aggregate summary of a Task's persisted resource usage without zero-coercing None."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    entry_count: int = Field(ge=0)
    run_ids: list[RunId] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    quota_buckets: list[str] = Field(default_factory=list)
    measured_duration_ms_total: int | None = None
    peak_memory_mb_max: float | None = None
    measured_input_tokens_total: int | None = None
    measured_output_tokens_total: int | None = None
    measured_total_tokens_total: int | None = None
    actual_cost_usd_total: float | None = None
    estimated_cost_usd_total: float | None = None
    has_estimated_entries: bool = False
    estimate_labels: list[str] = Field(default_factory=list)
    has_unavailable_duration_entries: bool = False
    has_unavailable_memory_entries: bool = False
    has_unavailable_token_entries: bool = False
    has_unavailable_cost_entries: bool = False
    quota_units_used_total: int | None = None
    open_alert_count: int = Field(ge=0)
    critical_alert_count: int = Field(ge=0)


class BucketQuotaSummary(BaseModel):
    """Shared or verified-independent quota consumption summary for a quota bucket."""

    model_config = ConfigDict(extra="forbid")

    quota_bucket_key: str
    provider_id: str
    quota_scope_kind: QuotaScopeKind
    quota_independence_proven: bool
    quota_independence_status: QuotaIndependenceStatus
    connections: list[str] = Field(default_factory=list)
    task_ids: list[TaskId] = Field(default_factory=list)
    entry_count: int = Field(ge=0)
    quota_units_used_total: int | None = None
    latest_quota_limit_units: int | None = None
    latest_quota_remaining_units: int | None = None
    open_alert_count: int = Field(ge=0)


class ScopeMetricTotals(BaseModel):
    """Internal/domain aggregate metric snapshot across a policy scope for threshold checks."""

    model_config = ConfigDict(extra="forbid")

    total_cost_usd: float | None = None
    has_estimated_cost: bool = False
    estimate_labels: list[str] = Field(default_factory=list)
    total_duration_ms: int | None = None
    peak_memory_mb: float | None = None
    total_tokens: int | None = None
    total_quota_units_used: int | None = None


@runtime_checkable
class ResourceLedgerTransactionContext(Protocol):
    """Transaction-scoped persistence boundary executed inside one atomic BEGIN IMMEDIATE unit."""

    def get_task(self, task_id: str) -> Task | None: ...

    def get_run(self, run_id: str) -> Run | None: ...

    def is_approval_approved(self, approval_id: str) -> bool: ...

    def get_policy(self, scope_kind: ResourceScopeKind, scope_ref: str) -> ResourceBudgetPolicy | None: ...

    def upsert_policy(self, policy: ResourceBudgetPolicy) -> ResourceBudgetPolicy: ...

    def find_ledger_by_idempotency(
        self, task_id: str, run_id: str | None, idempotency_key: str
    ) -> ResourceLedgerEntry | None: ...

    def insert_ledger_entry(self, entry: ResourceLedgerEntry) -> ResourceLedgerEntry: ...

    def list_matching_enabled_policies(self, entry: ResourceLedgerEntry) -> list[ResourceBudgetPolicy]: ...

    def compute_scope_totals(self, policy: ResourceBudgetPolicy) -> ScopeMetricTotals: ...

    def find_active_alert(self, dedup_key: str) -> ResourceThresholdAlert | None: ...

    def list_open_alerts_for_policy(self, policy_id: str) -> list[ResourceThresholdAlert]: ...

    def insert_alert(self, alert: ResourceThresholdAlert) -> ResourceThresholdAlert: ...

    def update_alert_observation(
        self,
        *,
        alert_id: str,
        expected_record_version: int,
        severity: ResourceAlertSeverity,
        observed_value: float,
        threshold_limit: float,
        utilization_ratio: float,
        is_estimated_basis: bool,
        estimate_label: str | None,
        summary: str,
        recommended_action: str,
        occurrence_count: int,
        notification_id: str | None,
        action_inbox_item_id: str | None,
        master_notified: bool,
        requires_operator_approval: bool,
        audit_event_id: str,
        updated_at: datetime,
    ) -> ResourceThresholdAlert: ...

    def mark_alert_acknowledged(
        self,
        *,
        alert_id: str,
        expected_record_version: int,
        acknowledged_by: str,
        acknowledged_at: datetime,
        note: str,
        audit_event_id: str,
    ) -> ResourceThresholdAlert: ...

    def mark_alert_resolved(
        self,
        *,
        alert_id: str,
        expected_record_version: int,
        resolved_by: str,
        resolved_at: datetime,
        note: str,
        audit_event_id: str,
    ) -> ResourceThresholdAlert: ...

    def get_alert_for_update(self, alert_id: str) -> ResourceThresholdAlert | None: ...

    def save_notification(self, notification: NotificationRef) -> NotificationRef: ...

    def insert_action_inbox_item(
        self, item: ActionInboxItem, *, audit_event_id: str
    ) -> ActionInboxItem: ...

    def update_action_inbox_status(
        self, inbox_item_id: str, *, status: ActionInboxStatus, updated_at: datetime
    ) -> None: ...

    def append_event(self, event: Event) -> Event: ...


@runtime_checkable
class ResourceLedgerRepository(Protocol):
    """Repository interface injected into ``ResourcesDepartmentService`` without SQLite coupling."""

    def execute_transaction(
        self,
        processor: Callable[[ResourceLedgerTransactionContext], Any],
    ) -> Any: ...

    def list_policies(self, *, enabled_only: bool = False) -> list[ResourceBudgetPolicy]: ...

    def list_ledger_entries(self, query: ResourceLedgerQuery) -> list[ResourceLedgerEntry]: ...

    def list_alerts(
        self, *, scope_ref: str | None = None, include_resolved: bool = False
    ) -> list[ResourceThresholdAlert]: ...

    def get_task(self, task_id: str) -> Task | None: ...

    def get_bucket_summary(self, quota_bucket_key: str) -> BucketQuotaSummary | None: ...

    def summarize_task(self, task_id: str) -> TaskResourceUsageSummary | None: ...
