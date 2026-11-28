"""Phase 09 / P09.02 Resources Department: truthful CPU/RAM/time/token tracking, quota scopes, and post-run inventory."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from datetime import datetime
import os
import time
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    RunId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
)
from core.contracts.references import NotificationRef, NotificationSeverity
from core.contracts.run import RunTelemetryRef, RunTelemetryStatus
from core.departments import resources_ledger as _ledger_ops
from core.departments._common import (
    DepartmentBehaviorError,
    ToolInvokerPort,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
    invoke_tool_checked,
)
from core.departments.resources_models import (
    BucketQuotaSummary,
    RecordResourceUsageOutcome,
    RecordResourceUsageRequest,
    RegisterBudgetPolicyRequest,
    ResourceBudgetPolicy,
    ResourceLedgerEntry,
    ResourceLedgerQuery,
    ResourceLedgerRepository,
    ResourceThresholdAlert,
    TaskResourceUsageSummary,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source
from core.tools.models import ToolUsageSummary
from engines.connections import EngineConnectionRef

QuotaIsolationStatus = Literal[
    "QUOTA_SHARED_BY_PROJECT",
    "QUOTA_INDEPENDENCE_UNPROVEN",
    "QUOTA_INDEPENDENT_VERIFIED",
]


class ResourcesValidationError(DepartmentBehaviorError):
    """Raised when quota scopes, budget requests, or connection metadata fail validation."""


class HostProcessSample(BaseModel):
    """Measured OS process CPU and RAM usage without synthetic fabrication."""

    model_config = ConfigDict(extra="forbid")

    cpu_seconds: float | None = Field(default=None, ge=0.0)
    rss_memory_bytes: int | None = Field(default=None, ge=0)
    source: Literal["os_measured", "UNAVAILABLE"] = "os_measured"
    sampled_at: datetime

    @field_validator("sampled_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "sampled_at")


@runtime_checkable
class HostMetricsSamplerPort(Protocol):
    """Read-only port measuring real host CPU and RSS memory."""

    def sample(self, timestamp: datetime) -> HostProcessSample:
        """Capture current process CPU time and RSS memory if available."""


class _ProcessMemoryCounters(ctypes.Structure):
    """Win32 PROCESS_MEMORY_COUNTERS structure for current process RSS read."""

    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _read_current_process_rss_bytes() -> int | None:
    """Return current process resident memory bytes via OS API or None if unavailable."""
    if os.name == "nt":
        try:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.GetCurrentProcess()
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except (OSError, AttributeError):
            return None
        return None
    try:
        import resource

        max_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if max_rss <= 0:
            return None
        # macOS returns bytes; Linux returns kilobytes.
        if os.uname().sysname.lower() == "darwin":
            return max_rss
        return max_rss * 1024
    except (ImportError, OSError, AttributeError):
        return None


class DefaultHostMetricsSampler:
    """Stdlib/OS host resource sampler measuring current process CPU and RSS memory."""

    def sample(self, timestamp: datetime) -> HostProcessSample:
        """Measure process CPU seconds and OS RSS bytes fail-safely without fabrication."""
        validate_aware_datetime(timestamp, "timestamp")
        cpu_val = max(0.0, float(time.process_time()))
        rss_val = _read_current_process_rss_bytes()
        return HostProcessSample(
            cpu_seconds=cpu_val,
            rss_memory_bytes=rss_val,
            source="os_measured",
            sampled_at=timestamp,
        )


class QuotaScopeRecord(BaseModel):
    """Budget and provider quota state keyed by Provider, Project Scope, Connection, and Model."""

    model_config = ConfigDict(extra="forbid")

    provider_id: SnakeId
    connection_id: SnakeId
    project_ref: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    budget_limit_usd: float | None = Field(default=None, ge=0.0)
    spent_cost_usd: float | None = Field(default=None, ge=0.0)
    remaining_quota_units: int | None = Field(default=None, ge=0)
    quota_limit_units: int | None = Field(default=None, ge=0)
    warning_threshold_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    telemetry_status: RunTelemetryStatus = "UNAVAILABLE_FROM_PROVIDER"
    independent_quota_proof: bool = False
    observed_at: datetime

    @field_validator("project_ref", "model_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "quota_field"))

    @field_validator("observed_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "observed_at")

    @model_validator(mode="after")
    def _validate_truthfulness(self) -> QuotaScopeRecord:
        """Prevent converting missing provider metrics into VERIFIED_MEASURED."""
        if (
            self.telemetry_status == "VERIFIED_MEASURED"
            and self.remaining_quota_units is None
            and self.spent_cost_usd is None
        ):
            raise ValueError(
                "telemetry_status='VERIFIED_MEASURED' requires at least one measured cost or quota value."
            )
        return self


class QuotaIsolationAssessment(BaseModel):
    """Result comparing two connections to forbid blind multi-key quota evasion."""

    model_config = ConfigDict(extra="forbid")

    connection_id_a: SnakeId
    connection_id_b: SnakeId
    provider_id_a: SnakeId
    provider_id_b: SnakeId
    shared_project_ref: str | None = None
    isolation_status: QuotaIsolationStatus
    assumed_independent: bool
    key_rotation_allowed: bool
    rationale: str


class AllocationRecommendation(BaseModel):
    """Advisory connection/model allocation guidance respecting budget and quota policies."""

    model_config = ConfigDict(extra="forbid")

    recommended_connection_id: SnakeId | None = None
    recommended_model_id: str | None = None
    status: Literal[
        "WITHIN_BUDGET",
        "WARNING_NEAR_LIMIT",
        "BUDGET_OR_QUOTA_EXCEEDED",
        "UNAVAILABLE_FROM_PROVIDER",
    ]
    requires_operator_approval: bool = False
    estimated_remaining_budget_usd: float | None = None
    notification_severity: NotificationSeverity | None = None
    reason: str


class PostRunResourceInventory(BaseModel):
    """Verified post-run resource ledger entry separating measured vs provider-unavailable metrics."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId
    provider_id: SnakeId
    connection_id: SnakeId
    project_ref: str | None = None
    model_id: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    cpu_seconds_delta: float | None = Field(default=None, ge=0.0)
    rss_memory_bytes: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    engine_cost_usd: float | None = Field(default=None, ge=0.0)
    tool_calls_count: int = Field(ge=0)
    tool_duration_ms: int = Field(ge=0)
    tool_estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    telemetry_status: RunTelemetryStatus
    audit_event_id: str
    notification_id: str | None = None
    excel_artifact_path: str | None = None


class ResourcesDepartmentService:
    """Department service tracking host metrics, provider/project/connection quotas, and post-run inventories."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        sampler: HostMetricsSamplerPort | None = None,
        metadata_repo: Any | None = None,
        tool_invoker: ToolInvokerPort | None = None,
        ledger_repository: ResourceLedgerRepository | None = None,
    ) -> None:
        """Bind Resources service to Governance, EventJournal, optional notification store, ToolRuntime, and Ledger."""
        self._pm = permission_manager
        self._journal = journal
        self._sampler: HostMetricsSamplerPort = sampler or DefaultHostMetricsSampler()
        self._metadata = metadata_repo
        self._tools = tool_invoker
        self._ledger = ledger_repository
        self._scopes: dict[tuple[str, str, str | None, str | None], QuotaScopeRecord] = {}
        self._inventories: list[PostRunResourceInventory] = []

    def measure_host_snapshot(self, *, timestamp: datetime) -> HostProcessSample:
        """Sample current process CPU seconds and RSS memory cleanly."""
        return self._sampler.sample(timestamp)

    def record_quota_scope(
        self,
        scope: QuotaScopeRecord,
        *,
        auth_context: TrustedAuthContext,
        task_id: str | None = None,
    ) -> QuotaScopeRecord:
        """Record or update verified/unavailable quota scope keyed by (provider, conn, project, model)."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="connection.read",
            resource=f"resources:quota:{scope.provider_id}:{scope.connection_id}",
            task_id=task_id,
            timestamp=scope.observed_at,
            correlation_id=f"res-scope-{scope.connection_id}",
        )
        key = (scope.provider_id, scope.connection_id, scope.project_ref, scope.model_id)
        self._scopes[key] = scope
        append_department_event(
            self._journal,
            event_type="notification.created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=task_id,
            actor_id=event_actor_id(auth_context),
            provider_id=scope.provider_id,
            connection_id=scope.connection_id,
            model_id=scope.model_id,
            timestamp=scope.observed_at,
            correlation_id=f"res-scope-{scope.connection_id}",
            payload={
                "department": "resources",
                "project_ref": scope.project_ref,
                "telemetry_status": scope.telemetry_status,
                "budget_limit_usd": scope.budget_limit_usd,
                "spent_cost_usd": scope.spent_cost_usd,
                "remaining_quota_units": scope.remaining_quota_units,
            },
        )
        return scope

    def get_quota_scope(
        self,
        *,
        provider_id: str,
        connection_id: str,
        project_ref: str | None = None,
        model_id: str | None = None,
    ) -> QuotaScopeRecord | None:
        """Lookup tracked scope for exact (provider_id, connection_id, project_ref, model_id)."""
        return self._scopes.get((provider_id, connection_id, project_ref, model_id))

    @staticmethod
    def evaluate_connection_quota_isolation(
        conn_a: EngineConnectionRef,
        conn_b: EngineConnectionRef,
        *,
        independent_quota_proof: bool = False,
    ) -> QuotaIsolationAssessment:
        """Never assume two connections have separate quotas unless provider billing explicitly proves it."""
        if conn_a.provider_id == conn_b.provider_id and (
            conn_a.project_ref is not None
            and conn_b.project_ref is not None
            and conn_a.project_ref == conn_b.project_ref
        ):
            return QuotaIsolationAssessment(
                connection_id_a=conn_a.connection_id,
                connection_id_b=conn_b.connection_id,
                provider_id_a=conn_a.provider_id,
                provider_id_b=conn_b.provider_id,
                shared_project_ref=conn_a.project_ref,
                isolation_status="QUOTA_SHARED_BY_PROJECT",
                assumed_independent=False,
                key_rotation_allowed=False,
                rationale=(
                    f"Connections share provider '{conn_a.provider_id}' and project_ref "
                    f"'{conn_a.project_ref}'; rotating keys does not create separate quota."
                ),
            )
        if independent_quota_proof and conn_a.quota_scope_note == "connection_scoped" and conn_b.quota_scope_note == "connection_scoped":
            return QuotaIsolationAssessment(
                connection_id_a=conn_a.connection_id,
                connection_id_b=conn_b.connection_id,
                provider_id_a=conn_a.provider_id,
                provider_id_b=conn_b.provider_id,
                isolation_status="QUOTA_INDEPENDENT_VERIFIED",
                assumed_independent=True,
                key_rotation_allowed=False,
                rationale="Independent quota isolation explicitly verified via provider billing scope.",
            )
        return QuotaIsolationAssessment(
            connection_id_a=conn_a.connection_id,
            connection_id_b=conn_b.connection_id,
            provider_id_a=conn_a.provider_id,
            provider_id_b=conn_b.provider_id,
            isolation_status="QUOTA_INDEPENDENCE_UNPROVEN",
            assumed_independent=False,
            key_rotation_allowed=False,
            rationale=(
                "Two connections/keys are never assumed to carry independent quotas "
                "without explicit verified provider billing proof."
            ),
        )

    def recommend_allocation(
        self,
        *,
        connections: list[EngineConnectionRef],
        preferred_model_id: str | None = None,
        estimated_cost_usd: float | None = None,
        budget_limit_usd: float | None = None,
    ) -> AllocationRecommendation:
        """Select enabled priority connection and flag budget/quota warnings transparently."""
        enabled = sorted(
            (c for c in connections if c.enabled),
            key=lambda c: (c.priority, c.connection_id),
        )
        if not enabled:
            return AllocationRecommendation(
                status="BUDGET_OR_QUOTA_EXCEEDED",
                requires_operator_approval=True,
                notification_severity="ERROR",
                reason="No enabled EngineConnectionRef is available for allocation.",
            )

        selected = enabled[0]
        target_model = preferred_model_id or selected.default_model_id
        scope = self.get_quota_scope(
            provider_id=selected.provider_id,
            connection_id=selected.connection_id,
            project_ref=selected.project_ref,
            model_id=target_model,
        )
        effective_budget = (
            scope.budget_limit_usd if scope is not None and scope.budget_limit_usd is not None else budget_limit_usd
        )
        spent = scope.spent_cost_usd if scope is not None else None
        projected = (
            (spent or 0.0) + estimated_cost_usd
            if (spent is not None or estimated_cost_usd is not None)
            else None
        )

        if scope is not None and scope.remaining_quota_units == 0:
            return AllocationRecommendation(
                recommended_connection_id=selected.connection_id,
                recommended_model_id=target_model,
                status="BUDGET_OR_QUOTA_EXCEEDED",
                requires_operator_approval=True,
                estimated_remaining_budget_usd=None,
                notification_severity="CRITICAL",
                reason=f"Connection '{selected.connection_id}' reports 0 remaining quota units.",
            )

        if effective_budget is not None and projected is not None:
            remaining = effective_budget - projected
            ratio = projected / effective_budget if effective_budget > 0 else 1.0
            if projected > effective_budget:
                return AllocationRecommendation(
                    recommended_connection_id=selected.connection_id,
                    recommended_model_id=target_model,
                    status="BUDGET_OR_QUOTA_EXCEEDED",
                    requires_operator_approval=True,
                    estimated_remaining_budget_usd=remaining,
                    notification_severity="WARNING",
                    reason=(
                        f"Projected spend ${projected:.4f} exceeds budget ${effective_budget:.4f}; "
                        "operator approval required before costly fallback."
                    ),
                )
            warn_ratio = scope.warning_threshold_ratio if scope is not None else 0.8
            if ratio >= warn_ratio:
                return AllocationRecommendation(
                    recommended_connection_id=selected.connection_id,
                    recommended_model_id=target_model,
                    status="WARNING_NEAR_LIMIT",
                    requires_operator_approval=False,
                    estimated_remaining_budget_usd=remaining,
                    notification_severity="WARNING",
                    reason=f"Budget utilization reached {ratio * 100:.1f}% (>= {warn_ratio * 100:.0f}%).",
                )
            return AllocationRecommendation(
                recommended_connection_id=selected.connection_id,
                recommended_model_id=target_model,
                status="WITHIN_BUDGET",
                requires_operator_approval=False,
                estimated_remaining_budget_usd=remaining,
                notification_severity="INFO",
                reason="Selected enabled connection stays within configured budget bounds.",
            )

        return AllocationRecommendation(
            recommended_connection_id=selected.connection_id,
            recommended_model_id=target_model,
            status="UNAVAILABLE_FROM_PROVIDER",
            requires_operator_approval=False,
            estimated_remaining_budget_usd=None,
            notification_severity="INFO",
            reason="Provider quota/cost telemetry not yet observed; keeping null values without fabrication.",
        )

    def compile_post_run_inventory(
        self,
        *,
        auth_context: TrustedAuthContext,
        task_id: str,
        run_id: str,
        provider_id: str,
        connection_id: str,
        project_ref: str | None = None,
        model_id: str | None = None,
        run_telemetry: RunTelemetryRef | None = None,
        tool_usage: ToolUsageSummary | None = None,
        start_host_sample: HostProcessSample | None = None,
        budget_limit_usd: float | None = None,
        export_excel_relpath: str | None = None,
        workspace_root: str | None = None,
        timestamp: datetime,
    ) -> PostRunResourceInventory:
        """Compile truthful post-run inventory preserving None + UNAVAILABLE_FROM_PROVIDER when unmeasured."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"resources:inventory:{task_id}/{run_id}",
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            correlation_id=f"res-inv-{run_id}",
        )
        end_sample = self.measure_host_snapshot(timestamp=timestamp)
        cpu_delta: float | None = None
        if (
            start_host_sample is not None
            and start_host_sample.cpu_seconds is not None
            and end_sample.cpu_seconds is not None
        ):
            cpu_delta = max(0.0, end_sample.cpu_seconds - start_host_sample.cpu_seconds)

        tel = run_telemetry or RunTelemetryRef()
        engine_cost = tel.actual_cost_usd if tel.actual_cost_usd is not None else tel.estimated_cost_usd
        tool_calls = tool_usage.total_calls if tool_usage is not None else 0
        tool_ms = tool_usage.total_duration_ms if tool_usage is not None else 0
        tool_cost = tool_usage.known_cost_usd if tool_usage is not None else None

        exceeded_budget = (
            budget_limit_usd is not None
            and engine_cost is not None
            and engine_cost > budget_limit_usd
        )
        near_budget = (
            not exceeded_budget
            and budget_limit_usd is not None
            and budget_limit_usd > 0
            and engine_cost is not None
            and (engine_cost / budget_limit_usd) >= 0.8
        )

        excel_path_written: str | None = None
        if export_excel_relpath is not None:
            if workspace_root is None:
                raise ResourcesValidationError(
                    "WORKSPACE_REQUIRED",
                    "workspace_root is required when export_excel_relpath is requested.",
                )
            res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.office.write_excel",
                parameters={
                    "path": export_excel_relpath,
                    "overwrite": True,
                    "sheets": {
                        "Inventory": [
                            [
                                "task_id",
                                "run_id",
                                "provider_id",
                                "connection_id",
                                "project_ref",
                                "model_id",
                                "input_tokens",
                                "output_tokens",
                                "engine_cost_usd",
                                "tool_calls_count",
                                "tool_duration_ms",
                                "telemetry_status",
                            ],
                            [
                                task_id,
                                run_id,
                                provider_id,
                                connection_id,
                                project_ref,
                                model_id,
                                tel.input_tokens,
                                tel.output_tokens,
                                engine_cost,
                                tool_calls,
                                tool_ms,
                                tel.telemetry_status,
                            ],
                        ]
                    },
                },
                task_id=task_id,
                run_id=run_id,
                workspace_root=workspace_root,
                target_path=export_excel_relpath,
                timestamp=timestamp,
                correlation_id=f"res-xlsx-{run_id}",
            )
            excel_path_written = str(res.data.get("path", export_excel_relpath))

        ev = append_department_event(
            self._journal,
            event_type="notification.created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=task_id,
            run_id=run_id,
            actor_id=event_actor_id(auth_context),
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=model_id,
            timestamp=timestamp,
            correlation_id=f"res-inv-{run_id}",
            payload={
                "department": "resources",
                "project_ref": project_ref,
                "telemetry_status": tel.telemetry_status,
                "input_tokens": tel.input_tokens,
                "output_tokens": tel.output_tokens,
                "total_tokens": tel.total_tokens,
                "engine_cost_usd": engine_cost,
                "tool_calls_count": tool_calls,
                "tool_duration_ms": tool_ms,
                "cpu_seconds_delta": cpu_delta,
                "rss_memory_bytes": end_sample.rss_memory_bytes,
            },
        )
        notif_id: str | None = None
        if (exceeded_budget or near_budget) and self._metadata is not None:
            notif = NotificationRef(
                task_id=task_id,
                run_id=run_id,
                event_id=ev.event_id,
                severity="ERROR" if exceeded_budget else "WARNING",
                notification_type="RESOURCE_WARNING",
                created_at=timestamp,
            )
            saved_notif = self._metadata.save_notification(notif)
            notif_id = saved_notif.notification_id

        inventory = PostRunResourceInventory(
            task_id=task_id,
            run_id=run_id,
            provider_id=provider_id,
            connection_id=connection_id,
            project_ref=project_ref,
            model_id=model_id,
            duration_ms=tel.total_duration_ms,
            cpu_seconds_delta=cpu_delta,
            rss_memory_bytes=end_sample.rss_memory_bytes,
            input_tokens=tel.input_tokens,
            output_tokens=tel.output_tokens,
            total_tokens=tel.total_tokens,
            engine_cost_usd=engine_cost,
            tool_calls_count=tool_calls,
            tool_duration_ms=tool_ms,
            tool_estimated_cost_usd=tool_cost,
            telemetry_status=tel.telemetry_status,
            audit_event_id=ev.event_id,
            notification_id=notif_id,
            excel_artifact_path=excel_path_written,
        )
        self._inventories.append(inventory)
        return inventory

    def list_inventories(self, *, task_id: str | None = None) -> list[PostRunResourceInventory]:
        """List recorded post-run inventories optionally filtered by ``task_id``."""
        if task_id is None:
            return list(self._inventories)
        return [item for item in self._inventories if item.task_id == task_id]

    def register_budget_policy(
        self,
        request: RegisterBudgetPolicyRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ResourceBudgetPolicy:
        """Register or update a durable budget/quota threshold policy (Operator or Master only)."""
        return _ledger_ops.register_budget_policy(
            self._pm,
            self._ledger,
            request,
            auth_context=auth_context,
        )

    def record_usage(
        self,
        request: RecordResourceUsageRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> RecordResourceUsageOutcome:
        """Persist immutable usage ledger telemetry and evaluate budget/quota threshold alerts."""
        return _ledger_ops.record_usage(
            self._pm,
            self._ledger,
            request,
            auth_context=auth_context,
        )

    def get_task_resource_summary(
        self,
        task_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> TaskResourceUsageSummary:
        """Return honest Task resource totals without converting unavailable metrics to zero."""
        return _ledger_ops.get_task_resource_summary(
            self._pm,
            self._ledger,
            task_id,
            auth_context=auth_context,
            timestamp=timestamp,
        )

    def get_bucket_quota_summary(
        self,
        quota_bucket_key: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> BucketQuotaSummary | None:
        """Return shared/verified quota consumption metrics for a quota bucket."""
        return _ledger_ops.get_bucket_quota_summary(
            self._pm,
            self._ledger,
            quota_bucket_key,
            auth_context=auth_context,
            timestamp=timestamp,
        )

    def list_resource_ledger(
        self,
        query: ResourceLedgerQuery | None = None,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> list[ResourceLedgerEntry]:
        """List durable resource ledger rows filtered by query."""
        return _ledger_ops.list_resource_ledger(
            self._pm,
            self._ledger,
            query,
            auth_context=auth_context,
            timestamp=timestamp,
        )

    def list_resource_alerts(
        self,
        *,
        scope_ref: str | None = None,
        include_resolved: bool = False,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> list[ResourceThresholdAlert]:
        """List persisted resource budget and quota threshold alerts."""
        return _ledger_ops.list_resource_alerts(
            self._pm,
            self._ledger,
            scope_ref=scope_ref,
            include_resolved=include_resolved,
            auth_context=auth_context,
            timestamp=timestamp,
        )

    def acknowledge_resource_alert(
        self,
        alert_id: str,
        *,
        expected_record_version: int,
        note: str,
        timestamp: datetime,
        auth_context: TrustedAuthContext,
    ) -> ResourceThresholdAlert:
        """Acknowledge an active resource threshold alert cleanly."""
        return _ledger_ops.acknowledge_resource_alert(
            self._pm,
            self._ledger,
            alert_id,
            expected_record_version=expected_record_version,
            note=note,
            timestamp=timestamp,
            auth_context=auth_context,
        )

    def resolve_resource_alert(
        self,
        alert_id: str,
        *,
        expected_record_version: int,
        note: str,
        timestamp: datetime,
        auth_context: TrustedAuthContext,
    ) -> ResourceThresholdAlert:
        """Resolve an active resource threshold alert cleanly."""
        return _ledger_ops.resolve_resource_alert(
            self._pm,
            self._ledger,
            alert_id,
            expected_record_version=expected_record_version,
            note=note,
            timestamp=timestamp,
            auth_context=auth_context,
        )
