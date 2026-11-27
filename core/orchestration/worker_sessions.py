"""Governed, isolated Worker Session Lifecycle and Engine execution coordinator for P05.05."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from fnmatch import fnmatch
import logging
from pathlib import Path, PurePosixPath
import threading
from typing import TYPE_CHECKING, Any, Literal
import uuid

from pydantic import ValidationError

from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    EventId,
    RunId,
    SessionId,
    ensure_not_secret,
    new_event_id,
    new_run_id,
    new_session_id,
    validate_aware_datetime,
)
from core.contracts.run import Run, RunErrorRef, RunTelemetryRef
from core.contracts.session import Session, SessionStatus
from core.contracts.task import Task
from core.events.journal import EventJournal
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import (
    PolicyDecision,
    PolicyRequest,
    TrustedAuthContext,
    actor_kind_to_event_source as _actor_kind_to_event_source,
    resolve_trusted_auth_context,
)
from core.governance.workspace import validate_workspace_path
from core.orchestration.planning_models import (
    PlanRepository,
    PlanStepSpec,
    StoredMasterPlan,
)
from core.orchestration.team_models import (
    PinnedAgentAssignment,
    StoredTeamProposal,
    TeamProposalRepository,
)
from core.orchestration.worker_session_models import (
    TERMINAL_SESSION_STATUSES,
    VALID_SESSION_TRANSITIONS,
    DuplicateInvocationConflictError,
    PreflightCheckResult,
    ProviderConversationCollisionError,
    SessionTransitionError,
    StaleSessionVersionError,
    StoredWorkerSession,
    WorkerExecutionOutcome,
    WorkerExecutionPreflightReport,
    WorkerExecutionRequest,
    WorkerPreflightBlockedError,
    WorkerRuntimeStateSnapshot,
    WorkerSessionBinding,
    WorkerSessionError,
    WorkerSessionNotFoundError,
    WorkerSessionRecoveryItem,
    WorkerSessionRecoveryReport,
    WorkerSessionRepository,
    WorkerSessionTransitionRecord,
)
from core.tasks.lifecycle import OwnedExecutionHandle, WorkflowLifecycleController
from core.tasks.state_machine import start_task_run, transition_run
from core.tasks.transitions import (
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    ActorTrustKind,
    ExecutionEvidence,
    RunTransitionRequest,
    TransitionContext,
)
from engines.antigravity.diagnostics import redact_secrets
from engines.connections import EngineConnectionRef
from engines.contract import (
    EngineEvent,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
)
from engines.registry import EngineRegistry

if TYPE_CHECKING:
    from core.agents.manifest import AgentManifest
    from core.agents.registry import AgentManifestVersion, AgentRegistryRecord, AgentRegistryService
    from core.governance.approvals import ApprovalService
    from core.orchestration.handoff_models import (
        HandoffContextPackage,
        WorkerHandoffRepository,
    )

logger = logging.getLogger(__name__)

_LOCAL_ONLY_PROVIDERS: frozenset[str] = frozenset({"ollama", "local_llm", "llama_cpp"})
_SENSITIVE_DENIED_PATHS: tuple[str, ...] = (".env", ".runtime", ".git", ".ssh", "secrets")


def _monotonic_ts(candidate: datetime | None, floor: datetime) -> datetime:
    """Ensure journal/session timestamps never regress earlier than causality bounds."""
    if candidate is None:
        return floor
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        return floor
    return candidate if candidate >= floor else floor


def _normalize_scope_posix(raw_scope: str) -> str:
    """Normalize a workspace-relative scope string to POSIX form without trailing slash."""
    cleaned = raw_scope.strip().replace("\\", "/")
    return PurePosixPath(cleaned).as_posix().rstrip("/")


def _scope_matches_patterns(
    scope_rel: str,
    patterns: Sequence[str],
    *,
    task_id: str,
) -> bool:
    """Return True if ``scope_rel`` matches at least one expanded manifest/step scope pattern."""
    if not patterns:
        return True
    normalized_scope = _normalize_scope_posix(scope_rel)
    probe_child = f"{normalized_scope}/__probe__.tmp"
    for raw_pattern in patterns:
        expanded = _normalize_scope_posix(raw_pattern.replace("{task_id}", task_id))
        if fnmatch(normalized_scope, expanded) or fnmatch(probe_child, expanded):
            return True
        if "*" not in expanded and (
            normalized_scope == expanded or normalized_scope.startswith(f"{expanded}/")
        ):
            return True
        if expanded.endswith("/*") and normalized_scope == expanded[:-2]:
            return True
    return False


def _truncate_bounded(text: str, limit: int = 500) -> str:
    """Trim whitespace and cap string length cleanly within Pydantic field limits."""
    cleaned = text.strip()
    if not cleaned:
        return "unspecified"
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def _sum_optional_ints(left: int | None, right: int | None) -> int | None:
    """Sum two optional non-negative counters while preserving None when both are absent."""
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _sum_optional_floats(left: float | None, right: float | None) -> float | None:
    """Sum two optional cost metrics while preserving None when both are absent."""
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)


def _merge_run_telemetry(existing: RunTelemetryRef | None, step: RunTelemetryRef) -> RunTelemetryRef:
    """Accumulate per-step Worker telemetry onto the shared Run without overwriting prior steps."""
    if existing is None:
        return step

    if existing.telemetry_status == "VERIFIED_MEASURED" and step.telemetry_status == "VERIFIED_MEASURED":
        merged_status = "VERIFIED_MEASURED"
        merged_source = (
            existing.telemetry_source
            if existing.telemetry_source == step.telemetry_source
            else "adapter_measured"
        )
    elif (
        existing.telemetry_status == "ESTIMATED_PENDING_AUDIT"
        or step.telemetry_status == "ESTIMATED_PENDING_AUDIT"
    ):
        merged_status = "ESTIMATED_PENDING_AUDIT"
        merged_source = "adapter_measured"
    else:
        merged_status = "UNAVAILABLE_FROM_PROVIDER"
        merged_source = (
            "unavailable"
            if existing.telemetry_source == "unavailable" and step.telemetry_source == "unavailable"
            else "adapter_measured"
        )

    return RunTelemetryRef(
        total_duration_ms=_sum_optional_ints(existing.total_duration_ms, step.total_duration_ms),
        provider_duration_ms=_sum_optional_ints(existing.provider_duration_ms, step.provider_duration_ms),
        engine_call_count=(existing.engine_call_count or 0) + (step.engine_call_count or 1),
        input_tokens=_sum_optional_ints(existing.input_tokens, step.input_tokens),
        output_tokens=_sum_optional_ints(existing.output_tokens, step.output_tokens),
        thinking_tokens=_sum_optional_ints(existing.thinking_tokens, step.thinking_tokens),
        cached_tokens=_sum_optional_ints(existing.cached_tokens, step.cached_tokens),
        total_tokens=_sum_optional_ints(existing.total_tokens, step.total_tokens),
        estimated_cost_usd=_sum_optional_floats(existing.estimated_cost_usd, step.estimated_cost_usd),
        actual_cost_usd=_sum_optional_floats(existing.actual_cost_usd, step.actual_cost_usd),
        telemetry_status=merged_status,
        telemetry_source=merged_source,
    )


def _map_engine_telemetry(result: EngineResult) -> RunTelemetryRef:
    """Project factual EngineResult telemetry without fabricating missing token metrics."""
    tel = result.telemetry
    return RunTelemetryRef(
        total_duration_ms=tel.duration_ms,
        provider_duration_ms=(
            int(round(tel.provider_duration_seconds * 1000))
            if tel.provider_duration_seconds is not None
            else None
        ),
        engine_call_count=1,
        input_tokens=tel.input_tokens,
        output_tokens=tel.output_tokens,
        thinking_tokens=tel.thinking_tokens,
        cached_tokens=tel.cached_tokens,
        total_tokens=tel.total_tokens,
        estimated_cost_usd=tel.estimated_cost_usd,
        actual_cost_usd=tel.actual_cost_usd,
        telemetry_status=tel.telemetry_status,
        telemetry_source=tel.telemetry_source,
    )


class WorkerSessionLifecycleService:
    """Isolated, SQLite-backed Worker Session Lifecycle & Engine execution service."""

    def __init__(
        self,
        *,
        worker_session_repo: WorkerSessionRepository,
        task_repo: Any,
        run_repo: Any,
        metadata_repo: Any,
        event_journal: EventJournal,
        plan_repo: PlanRepository,
        team_repo: TeamProposalRepository,
        agent_registry: AgentRegistryService,
        permission_manager: PermissionManager,
        engine_registry: EngineRegistry,
        lifecycle_controller: WorkflowLifecycleController | None = None,
        approval_service: ApprovalService | None = None,
        handoff_repo: WorkerHandoffRepository | None = None,
        connection_resolver: Callable[[str], EngineConnectionRef | None] | None = None,
        tool_runtime: Any | None = None,
        artifact_vault: Any | None = None,
    ) -> None:
        """Inject explicit persistence, Registry, Governance, Engine, and Lifecycle dependencies."""
        self._worker_sessions = worker_session_repo
        self._tasks = task_repo
        self._runs = run_repo
        self._metadata = metadata_repo
        self._events = event_journal
        self._plans = plan_repo
        self._teams = team_repo
        self._registry = agent_registry
        self._permissions = permission_manager
        self._engines = engine_registry
        self._lifecycle = lifecycle_controller
        self._approvals = approval_service
        self._handoffs = handoff_repo
        self._connection_resolver = connection_resolver
        self._tool_runtime = tool_runtime
        self._artifact_vault = artifact_vault
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._task_locks: dict[str, threading.RLock] = {}
        self._live_handles: dict[str, OwnedExecutionHandle] = {}

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """Return a per-session lock serializing execution completion against cancellation."""
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _get_task_lock(self, task_id: str) -> threading.RLock:
        """Return a per-task lock serializing shared Task/Run state updates without blocking unrelated tasks."""
        with self._lock:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = threading.RLock()
                self._task_locks[task_id] = lock
            return lock

    def _resolve_target_run(
        self,
        task: Task,
        requested_run_id: str | None,
    ) -> tuple[Run | None, int | None, str | None, str | None]:
        """Resolve an existing non-terminal Run to share across Worker sessions on one Task."""
        if requested_run_id is not None:
            stored = self._runs.get_stored_run(requested_run_id)
            if stored is None:
                return None, None, "RUN_NOT_FOUND", f"Run '{requested_run_id}' does not exist."
            if stored.run.task_id != task.task_id:
                return (
                    None,
                    None,
                    "WRONG_RUN_TASK_OWNERSHIP",
                    f"Run '{requested_run_id}' belongs to Task '{stored.run.task_id}', not '{task.task_id}'.",
                )
            if stored.run.status in TERMINAL_RUN_STATUSES:
                return (
                    None,
                    None,
                    "STALE_RUN_STATE",
                    f"Run '{requested_run_id}' is already terminal ({stored.run.status}).",
                )
            return stored.run, stored.version, None, None

        if task.active_run_id is not None:
            stored = self._runs.get_stored_run(task.active_run_id)
            if stored is None or stored.run.task_id != task.task_id:
                return None, None, "STALE_RUN_STATE", f"Task active_run_id '{task.active_run_id}' is invalid."
            if stored.run.status in TERMINAL_RUN_STATUSES:
                return (
                    None,
                    None,
                    "STALE_RUN_STATE",
                    f"Task active Run '{task.active_run_id}' is already terminal ({stored.run.status}).",
                )
            return stored.run, stored.version, None, None

        for existing_run in reversed(self._runs.list_runs_for_task(task.task_id)):
            if existing_run.status in {"PENDING", "RUNNING"}:
                stored = self._runs.get_stored_run(existing_run.run_id)
                if stored is not None:
                    return stored.run, stored.version, None, None

        return None, None, None, None

    def _resolve_connection(
        self,
        provider_id: str,
        connection_id: str | None,
    ) -> tuple[EngineConnectionRef | None, str | None, str | None]:
        """Resolve connection metadata from EngineRegistry or injected resolver without fallback."""
        if connection_id is None:
            return None, None, None
        conn: EngineConnectionRef | None = self._engines.connections.get(connection_id)
        if conn is None and self._connection_resolver is not None:
            conn = self._connection_resolver(connection_id)
        if conn is None:
            return (
                None,
                "CONNECTION_MISMATCH",
                f"Connection '{connection_id}' is not registered in EngineRegistry.",
            )
        if not conn.enabled:
            return (
                conn,
                "CONNECTION_DISABLED",
                f"Connection '{connection_id}' is disabled.",
            )
        if conn.provider_id != provider_id:
            return (
                conn,
                "CONNECTION_MISMATCH",
                f"Connection '{connection_id}' belongs to provider '{conn.provider_id}', not '{provider_id}'.",
            )
        return conn, None, None

    def run_execution_preflight(
        self,
        request: WorkerExecutionRequest,
    ) -> WorkerExecutionPreflightReport:
        """Run all 18 fail-closed execution preflight checks before touching Engine adapters."""
        validate_aware_datetime(request.timestamp, "request.timestamp")
        checks: list[PreflightCheckResult] = []
        blocking_codes: list[str] = []

        def _record(name: str, passed: bool, code: str, message: str) -> None:
            checks.append(PreflightCheckResult(check_name=name, passed=passed, code=code, message=message))
            if not passed and code not in blocking_codes:
                blocking_codes.append(code)

        # 1. Task exists
        stored_task = self._tasks.get_stored_task(request.task_id)
        if stored_task is None:
            _record("task_exists", False, "TASK_NOT_FOUND", f"Task '{request.task_id}' does not exist.")
            return WorkerExecutionPreflightReport(
                allowed=False,
                task_id=request.task_id,
                run_id=request.run_id,
                plan_id=request.plan_id,
                team_proposal_id=request.team_proposal_id,
                step_id=request.step_id,
                agent_id=request.agent_id,
                checks=checks,
                blocking_codes=blocking_codes,
            )
        task = stored_task.task
        _record("task_exists", True, "OK", f"Task '{task.task_id}' exists (status={task.status}).")

        # 2. Task assigned cleanly & Run ownership valid
        run_obj, _, run_err_code, run_err_msg = self._resolve_target_run(task, request.run_id)
        task_eligible = task.status in {"ASSIGNED", "RUNNING"} and request.agent_id in task.assigned_agent_ids
        if run_err_code is not None:
            _record("task_and_run_assigned", False, run_err_code, run_err_msg or "Run ownership failed.")
        elif not task_eligible:
            _record(
                "task_and_run_assigned",
                False,
                "TASK_NOT_ASSIGNED",
                f"Task '{task.task_id}' (status={task.status}) has not assigned '{request.agent_id}'.",
            )
        else:
            _record(
                "task_and_run_assigned",
                True,
                "OK",
                f"Task '{task.task_id}' and Run '{run_obj.run_id if run_obj else 'pending_create'}' eligible.",
            )

        # 3. MasterWorkPlan APPLIED & step present
        stored_plan: StoredMasterPlan | None = self._plans.get_plan(request.plan_id)
        plan_step: PlanStepSpec | None = None
        if stored_plan is None:
            _record("plan_applied", False, "PLAN_NOT_FOUND", f"MasterWorkPlan '{request.plan_id}' not found.")
        elif stored_plan.task_id != task.task_id:
            _record(
                "plan_applied",
                False,
                "PLAN_TASK_MISMATCH",
                f"Plan '{stored_plan.plan_id}' belongs to Task '{stored_plan.task_id}', not '{task.task_id}'.",
            )
        elif stored_plan.status != "APPLIED":
            _record(
                "plan_applied",
                False,
                "PLAN_NOT_APPLIED",
                f"Plan '{stored_plan.plan_id}' status is '{stored_plan.status}', expected 'APPLIED'.",
            )
        else:
            plan_step = next((s for s in stored_plan.plan.steps if s.step_id == request.step_id), None)
            if plan_step is None:
                _record(
                    "plan_applied",
                    False,
                    "STEP_NOT_FOUND",
                    f"Step '{request.step_id}' does not exist in Plan '{stored_plan.plan_id}'.",
                )
            else:
                _record(
                    "plan_applied",
                    True,
                    "OK",
                    f"Plan '{stored_plan.plan_id}' rev {stored_plan.active_revision} APPLIED with step '{plan_step.step_id}'.",
                )

        # 4. TeamProposal ASSIGNED & aligned with Task/Plan
        stored_team: StoredTeamProposal | None = self._teams.get_proposal(request.team_proposal_id)
        pinned_member: PinnedAgentAssignment | None = None
        if stored_team is None:
            _record(
                "team_proposal_assigned",
                False,
                "TEAM_PROPOSAL_NOT_FOUND",
                f"TeamProposal '{request.team_proposal_id}' not found.",
            )
        elif stored_team.task_id != task.task_id:
            _record(
                "team_proposal_assigned",
                False,
                "TEAM_TASK_MISMATCH",
                f"TeamProposal '{stored_team.proposal_id}' belongs to Task '{stored_team.task_id}'.",
            )
        elif stored_plan is not None and (
            stored_team.plan_id != stored_plan.plan_id
            or stored_team.plan_revision != stored_plan.active_revision
        ):
            _record(
                "team_proposal_assigned",
                False,
                "TEAM_PLAN_MISMATCH",
                "TeamProposal plan_id/plan_revision does not match active APPLIED Plan.",
            )
        elif stored_team.status != "ASSIGNED":
            _record(
                "team_proposal_assigned",
                False,
                "TEAM_NOT_ASSIGNED",
                f"TeamProposal '{stored_team.proposal_id}' status is '{stored_team.status}', expected 'ASSIGNED'.",
            )
        else:
            pinned_member = next(
                (m for m in stored_team.proposal.pinned_members if m.agent_id == request.agent_id),
                None,
            )
            _record(
                "team_proposal_assigned",
                True,
                "OK",
                f"TeamProposal '{stored_team.proposal_id}' is ASSIGNED.",
            )

        # 5-9. Registry existence, ACTIVE status, non-stale proposal, pinned revision & SHA-256
        live_record: AgentRegistryRecord | None = self._registry.get_agent(request.agent_id)
        if live_record is None:
            _record(
                "worker_registered",
                False,
                "WORKER_NOT_REGISTERED",
                f"Worker '{request.agent_id}' does not exist in Agent Registry.",
            )
            _record("worker_active", False, "WORKER_NOT_REGISTERED", "Cannot verify ACTIVE status for missing Worker.")
            _record("team_not_stale", False, "WORKER_NOT_REGISTERED", "Worker missing from Registry.")
            _record("pinned_revision_exists", False, "WORKER_NOT_REGISTERED", "Worker missing from Registry.")
            _record("pinned_sha256_match", False, "WORKER_NOT_REGISTERED", "Worker missing from Registry.")
            pinned_version: AgentManifestVersion | None = None
        else:
            _record("worker_registered", True, "OK", f"Worker '{request.agent_id}' exists in Registry.")
            if live_record.registration_status != "ACTIVE":
                _record(
                    "worker_active",
                    False,
                    "WORKER_DISABLED",
                    f"Worker '{request.agent_id}' has Registry status '{live_record.registration_status}', not 'ACTIVE'.",
                )
            else:
                _record("worker_active", True, "OK", f"Worker '{request.agent_id}' is ACTIVE in Registry.")

            if pinned_member is None:
                _record(
                    "team_not_stale",
                    False,
                    "UNASSIGNED_WORKER",
                    f"Worker '{request.agent_id}' is not in TeamProposal '{request.team_proposal_id}'.",
                )
                _record("pinned_revision_exists", False, "UNASSIGNED_WORKER", "No pinned revision available.")
                _record("pinned_sha256_match", False, "UNASSIGNED_WORKER", "No pinned hash available.")
                pinned_version = None
            else:
                staleness_issues: list[str] = []
                if stored_team is not None and stored_team.status == "STALE":
                    staleness_issues.append("TeamProposal marked STALE.")
                if live_record.active_revision != pinned_member.manifest_revision:
                    staleness_issues.append(
                        f"Live revision {live_record.active_revision} != pinned {pinned_member.manifest_revision}."
                    )
                if live_record.manifest_sha256 != pinned_member.manifest_sha256:
                    staleness_issues.append("Live manifest SHA-256 drifted from pinned digest.")
                if staleness_issues:
                    _record(
                        "team_not_stale",
                        False,
                        "STALE_MANIFEST_REVISION",
                        "; ".join(staleness_issues),
                    )
                else:
                    _record(
                        "team_not_stale",
                        True,
                        "OK",
                        f"Pinned revision r{pinned_member.manifest_revision} matches live Registry.",
                    )

                pinned_version = self._registry.get_manifest_version(
                    request.agent_id,
                    pinned_member.manifest_revision,
                )
                if pinned_version is None:
                    _record(
                        "pinned_revision_exists",
                        False,
                        "INVALID_MANIFEST_REVISION",
                        f"Manifest revision r{pinned_member.manifest_revision} not found for '{request.agent_id}'.",
                    )
                    _record("pinned_sha256_match", False, "INVALID_MANIFEST_REVISION", "Missing pinned revision.")
                else:
                    _record(
                        "pinned_revision_exists",
                        True,
                        "OK",
                        f"Pinned Manifest revision r{pinned_version.manifest_revision} loaded.",
                    )
                    computed_hash = pinned_version.manifest.canonical_sha256()
                    if (
                        pinned_version.manifest_sha256 != pinned_member.manifest_sha256
                        or computed_hash != pinned_member.manifest_sha256
                    ):
                        _record(
                            "pinned_sha256_match",
                            False,
                            "MANIFEST_HASH_DRIFT",
                            "Pinned Manifest SHA-256 does not match immutable version record digest.",
                        )
                    else:
                        _record(
                            "pinned_sha256_match",
                            True,
                            "OK",
                            "Pinned Manifest SHA-256 verified against immutable version history.",
                        )

        # 10. Worker explicitly assigned to target step
        if stored_team is not None:
            step_entry = next(
                (s for s in stored_team.proposal.step_assignments if s.step_id == request.step_id),
                None,
            )
            if (
                step_entry is None
                or step_entry.status != "ASSIGNED_IN_PROPOSAL"
                or step_entry.assigned_agent_id != request.agent_id
            ):
                _record(
                    "worker_assigned_to_step",
                    False,
                    "UNASSIGNED_WORKER",
                    f"Worker '{request.agent_id}' is not assigned to step '{request.step_id}'.",
                )
            elif pinned_member is not None and step_entry.pinned_manifest_revision != pinned_member.manifest_revision:
                _record(
                    "worker_assigned_to_step",
                    False,
                    "MANIFEST_REVISION_MISMATCH",
                    f"Step assignment revision mismatch on '{request.step_id}'.",
                )
            else:
                _record(
                    "worker_assigned_to_step",
                    True,
                    "OK",
                    f"Worker '{request.agent_id}' owns step '{request.step_id}'.",
                )
        else:
            _record("worker_assigned_to_step", False, "TEAM_PROPOSAL_NOT_FOUND", "No TeamProposal loaded.")

        # 11. Step upstream dependencies (Checklist / Session / Handoff)
        dep_issues: list[str] = []
        if plan_step is not None and plan_step.depends_on:
            checklist_by_id = {
                item.checklist_item_id: item
                for item in self._metadata.list_checklist_items(task.task_id)
            }
            existing_sessions = self._worker_sessions.list_stored_sessions(
                task_id=task.task_id,
                run_id=run_obj.run_id if run_obj else None,
            )
            for dep_step_id in plan_step.depends_on:
                dep_plan_step = (
                    next((s for s in stored_plan.plan.steps if s.step_id == dep_step_id), None)
                    if stored_plan is not None
                    else None
                )
                chk_done = (
                    dep_plan_step is not None
                    and dep_plan_step.checklist_item_id is not None
                    and dep_plan_step.checklist_item_id in checklist_by_id
                    and checklist_by_id[dep_plan_step.checklist_item_id].status == "COMPLETED"
                    and checklist_by_id[dep_plan_step.checklist_item_id].evidence_reference is not None
                )
                sess_done = any(
                    s.binding.step_id == dep_step_id and s.session.status == "CLOSED"
                    for s in existing_sessions
                )
                if not (chk_done or sess_done):
                    dep_issues.append(f"Upstream step '{dep_step_id}' has no COMPLETED checklist or CLOSED session.")
                if request.require_acknowledged_handoffs and self._handoffs is not None:
                    handoffs = self._handoffs.list_handoffs(
                        task_id=task.task_id,
                        run_id=run_obj.run_id if run_obj else None,
                        source_step_id=dep_step_id,
                        target_step_id=plan_step.step_id,
                    )
                    accepted = [
                        h
                        for h in handoffs
                        if h.handoff.to_agent_id == request.agent_id
                        and h.handoff.status == "ACKNOWLEDGED"
                    ]
                    if not accepted:
                        dep_issues.append(
                            f"Missing ACKNOWLEDGED handoff from '{dep_step_id}' to '{plan_step.step_id}'."
                        )
        if dep_issues:
            _record("step_dependencies_met", False, "DEPENDENCY_NOT_SATISFIED", "; ".join(dep_issues))
        else:
            _record("step_dependencies_met", True, "OK", "All upstream step dependencies satisfied.")

        # 12 & 13. Provider, Connection, Engine Adapter & Capability / Data Residency check
        pinned_manifest: AgentManifest | None = pinned_version.manifest if pinned_version is not None else None
        resolved_provider: str | None = (
            request.provider_id
            or (pinned_member.provider_id if pinned_member else None)
            or (pinned_manifest.engine.provider_id if pinned_manifest else None)
            or (pinned_manifest.engine.default_type if pinned_manifest else None)
        )
        resolved_connection: str | None = (
            request.connection_id
            or (pinned_member.connection_id if pinned_member else None)
            or (pinned_manifest.engine.connection_id if pinned_manifest else None)
        )
        resolved_model: str | None = (
            request.model_id
            or (pinned_member.model_id if pinned_member else None)
            or (pinned_manifest.engine.model_id if pinned_manifest else None)
        )

        provider_code: str | None = None
        provider_msg: str | None = None
        if resolved_provider is None:
            provider_code = "PROVIDER_MISMATCH"
            provider_msg = "No provider_id could be resolved from request or pinned manifest."
        elif request.provider_id is not None:
            if pinned_member and pinned_member.provider_id and request.provider_id != pinned_member.provider_id:
                provider_code = "PROVIDER_MISMATCH"
                provider_msg = (
                    f"Requested provider '{request.provider_id}' mismatches pinned provider "
                    f"'{pinned_member.provider_id}'."
                )
            elif pinned_manifest and pinned_manifest.engine.provider_id and request.provider_id != pinned_manifest.engine.provider_id:
                provider_code = "PROVIDER_MISMATCH"
                provider_msg = (
                    f"Requested provider '{request.provider_id}' mismatches manifest provider "
                    f"'{pinned_manifest.engine.provider_id}'."
                )

        if provider_code is None and request.connection_id is not None:
            if pinned_member and pinned_member.connection_id and request.connection_id != pinned_member.connection_id:
                provider_code = "CONNECTION_MISMATCH"
                provider_msg = (
                    f"Requested connection '{request.connection_id}' mismatches pinned connection "
                    f"'{pinned_member.connection_id}'."
                )
            elif (
                pinned_manifest
                and pinned_manifest.engine.connection_id
                and request.connection_id != pinned_manifest.engine.connection_id
            ):
                provider_code = "CONNECTION_MISMATCH"
                provider_msg = (
                    f"Requested connection '{request.connection_id}' mismatches manifest connection "
                    f"'{pinned_manifest.engine.connection_id}'."
                )

        conn_ref: EngineConnectionRef | None = None
        if provider_code is None and resolved_provider is not None and resolved_connection is not None:
            conn_ref, provider_code, provider_msg = self._resolve_connection(
                resolved_provider,
                resolved_connection,
            )

        if provider_code is not None:
            _record("provider_connection_valid", False, provider_code, provider_msg or "Provider/Connection rejected.")
            _record("engine_adapter_valid", False, provider_code, "Skipped adapter validation due to routing mismatch.")
        else:
            _record(
                "provider_connection_valid",
                True,
                "OK",
                f"Provider '{resolved_provider}' connection '{resolved_connection}' verified.",
            )
            data_locality: Literal["default", "local_only"] = "default"
            if (
                plan_step is not None
                and plan_step.routing_proposal is not None
                and plan_step.routing_proposal.data_residency_policy == "local_only"
            ):
                data_locality = "local_only"
            elif (
                stored_plan is not None
                and stored_plan.plan.routing_proposal.data_residency_policy == "local_only"
            ):
                data_locality = "local_only"

            probe_req = EngineRequest(
                provider_id=resolved_provider,
                adapter_id=pinned_manifest.engine.adapter_id if pinned_manifest else None,
                connection_id=conn_ref.connection_id if conn_ref and self._engines.connections.get(conn_ref.connection_id) else None,
                model_id=resolved_model,
                prompt=f"preflight probe for {request.step_id}",
                workspace_path=str(Path(request.workspace_root) / request.workspace_scope),
                execution_mode=request.execution_mode,
                data_locality=data_locality,
            )
            adapter, _, resolve_err = self._engines.resolve(probe_req)
            if resolve_err is not None or adapter is None:
                _record(
                    "engine_adapter_valid",
                    False,
                    resolve_err.error_code if resolve_err else "ENGINE_ADAPTER_MISSING",
                    resolve_err.message if resolve_err else f"No EngineAdapter for '{resolved_provider}'.",
                )
            else:
                cap_err = adapter.validate_request_capabilities(
                    probe_req,
                    adapter.get_capabilities(conn_ref),
                    connection_id=resolved_connection,
                )
                if cap_err is not None:
                    _record("engine_adapter_valid", False, cap_err.error_code, cap_err.message)
                else:
                    _record(
                        "engine_adapter_valid",
                        True,
                        "OK",
                        f"Adapter '{adapter.adapter_id}' verified for provider '{resolved_provider}'.",
                    )

        # 14. Workspace scope isolation & manifest fs_read/fs_write boundary check
        ws_action = "files.write" if request.execution_mode == "accept-edits" else "files.read"
        ws_check = validate_workspace_path(
            request.workspace_root,
            request.workspace_scope,
            action=ws_action,
        )
        if not ws_check.allowed:
            _record("workspace_scope_valid", False, "WORKSPACE_VIOLATION", ws_check.message)
        elif pinned_manifest is not None:
            allowed_patterns = (
                pinned_manifest.permissions.fs_write
                if request.execution_mode == "accept-edits"
                else (pinned_manifest.permissions.fs_read or pinned_manifest.permissions.fs_write)
            )
            if request.execution_mode == "accept-edits" and not pinned_manifest.permissions.fs_write:
                _record(
                    "workspace_scope_valid",
                    False,
                    "WORKSPACE_VIOLATION",
                    f"Worker '{request.agent_id}' manifest grants no permissions.fs_write for accept-edits.",
                )
            elif allowed_patterns and not _scope_matches_patterns(
                request.workspace_scope,
                allowed_patterns,
                task_id=task.task_id,
            ):
                _record(
                    "workspace_scope_valid",
                    False,
                    "WORKSPACE_VIOLATION",
                    (
                        f"Workspace scope '{request.workspace_scope}' is outside manifest "
                        f"{'fs_write' if request.execution_mode == 'accept-edits' else 'fs_read'} bounds."
                    ),
                )
            elif (
                request.execution_mode == "accept-edits"
                and plan_step is not None
                and plan_step.allowed_write_paths
                and not _scope_matches_patterns(
                    request.workspace_scope,
                    plan_step.allowed_write_paths,
                    task_id=task.task_id,
                )
            ):
                _record(
                    "workspace_scope_valid",
                    False,
                    "WORKSPACE_VIOLATION",
                    f"Workspace scope '{request.workspace_scope}' is outside step.allowed_write_paths.",
                )
            else:
                _record(
                    "workspace_scope_valid",
                    True,
                    "OK",
                    f"Workspace scope '{request.workspace_scope}' canonicalized to '{ws_check.canonical_path}'.",
                )
        else:
            _record("workspace_scope_valid", True, "OK", f"Workspace scope '{request.workspace_scope}' validated.")

        # 15 & 16. Governance PermissionManager check & Approval check (read-only evaluation)
        auth_ctx = resolve_trusted_auth_context(
            auth_context=request.auth_context,
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
        )
        policy_req = self._build_execute_policy_request(
            request=request,
            auth_context=auth_ctx,
            run_id=run_obj.run_id if run_obj else None,
            workspace_path=ws_check.canonical_path,
            pinned_manifest=pinned_manifest,
        )
        decision = self._permissions.evaluator.evaluate(policy_req)
        approval_required = decision.decision == "APPROVAL_REQUIRED" or (
            plan_step is not None and plan_step.requires_human_approval
        )
        if decision.decision in {"DENY", "ERROR"}:
            _record("governance_allowed", False, "UNAUTHORIZED_EXECUTION", decision.message)
        else:
            _record("governance_allowed", True, "OK", f"Governance decision '{decision.decision}'.")

        if approval_required:
            if self._approvals is None or request.approval_id is None:
                _record(
                    "approval_satisfied",
                    False,
                    "APPROVAL_REQUIRED",
                    "Execution requires an approved human approval_id.",
                )
            else:
                appr = self._approvals.get_approval(request.approval_id)
                if appr is None:
                    _record("approval_satisfied", False, "APPROVAL_NOT_FOUND", f"Approval '{request.approval_id}' not found.")
                elif appr.status != "APPROVED":
                    _record(
                        "approval_satisfied",
                        False,
                        "APPROVAL_NOT_APPROVED",
                        f"Approval '{request.approval_id}' has status '{appr.status}', expected 'APPROVED'.",
                    )
                elif appr.expires_at is not None and request.timestamp >= appr.expires_at:
                    _record("approval_satisfied", False, "APPROVAL_EXPIRED", f"Approval '{request.approval_id}' expired.")
                elif appr.task_id is not None and appr.task_id != task.task_id:
                    _record("approval_satisfied", False, "APPROVAL_TASK_MISMATCH", "Approval bound to different Task.")
                else:
                    _record("approval_satisfied", True, "OK", f"Approval '{appr.approval_id}' valid.")
        else:
            _record("approval_satisfied", True, "OK", "No extra human approval required.")

        # 17. Cancellation / timeout / manifest execution limit guards
        cancel_blocked = False
        if self._lifecycle is not None and run_obj is not None:
            if not self._lifecycle.check_checkpoint_can_continue(task.task_id, run_obj.run_id):
                cancel_blocked = True
        if cancel_blocked:
            _record(
                "no_cancel_or_timeout",
                False,
                "CANCEL_REQUESTED",
                f"Run '{run_obj.run_id if run_obj else 'none'}' is cancelled or terminal.",
            )
        elif pinned_manifest is not None and request.timeout_seconds > float(pinned_manifest.limits.timeout_seconds):
            _record(
                "no_cancel_or_timeout",
                False,
                "TIMEOUT_EXCEEDS_MANIFEST_LIMIT",
                (
                    f"Requested timeout {request.timeout_seconds}s exceeds manifest limit "
                    f"{pinned_manifest.limits.timeout_seconds}s."
                ),
            )
        else:
            _record("no_cancel_or_timeout", True, "OK", "No active cancel/timeout barrier.")

        # 18. Idempotency check
        replay_session_id: str | None = None
        if request.idempotency_key is not None:
            existing_idem = self._worker_sessions.find_by_idempotency(
                request.task_id,
                request.idempotency_key,
            )
            if existing_idem is not None:
                expected_fp = request.compute_request_fingerprint()
                if existing_idem.binding.request_fingerprint != expected_fp:
                    _record(
                        "idempotency_clear",
                        False,
                        "IDEMPOTENCY_CONFLICT",
                        f"Idempotency key '{request.idempotency_key}' was reused with different parameters.",
                    )
                else:
                    replay_session_id = existing_idem.session.session_id
                    _record(
                        "idempotency_clear",
                        True,
                        "IDEMPOTENT_REPLAY",
                        f"Matched existing idempotent session '{replay_session_id}'.",
                    )
            else:
                _record("idempotency_clear", True, "OK", "Idempotency key available.")
        else:
            _record("idempotency_clear", True, "OK", "No idempotency key supplied.")

        return WorkerExecutionPreflightReport(
            allowed=len(blocking_codes) == 0,
            task_id=task.task_id,
            run_id=run_obj.run_id if run_obj else request.run_id,
            plan_id=request.plan_id,
            plan_revision=stored_plan.active_revision if stored_plan else None,
            team_proposal_id=request.team_proposal_id,
            step_id=request.step_id,
            agent_id=request.agent_id,
            pinned_manifest_revision=pinned_member.manifest_revision if pinned_member else None,
            pinned_manifest_sha256=pinned_member.manifest_sha256 if pinned_member else None,
            resolved_provider_id=resolved_provider,
            resolved_connection_id=resolved_connection,
            resolved_model_id=resolved_model,
            resolved_workspace_path=ws_check.canonical_path,
            checks=checks,
            blocking_codes=blocking_codes,
            approval_id=request.approval_id,
            idempotent_replay_session_id=replay_session_id,
        )

    @staticmethod
    def _build_execute_policy_request(
        *,
        request: WorkerExecutionRequest,
        auth_context: TrustedAuthContext,
        run_id: str | None,
        workspace_path: str | None,
        pinned_manifest: AgentManifest | None,
    ) -> PolicyRequest:
        """Build Governance ``PolicyRequest`` for ``agent.execute``."""
        return PolicyRequest(
            auth_context=auth_context,
            action="agent.execute",
            resource=f"task:{request.task_id}/step:{request.step_id}/agent:{request.agent_id}",
            task_id=request.task_id,
            run_id=run_id,
            timestamp=request.timestamp,
            workspace_root=request.workspace_root,
            target_path=workspace_path or request.workspace_scope,
            approval_id=request.approval_id,
            correlation_id=request.correlation_id,
            parameters={
                "agent_id": request.agent_id,
                "step_id": request.step_id,
                "plan_id": request.plan_id,
                "team_proposal_id": request.team_proposal_id,
                "pinned_revision": getattr(pinned_manifest, "spec_version", None),
            },
        )

    def initialize_worker_session(
        self,
        request: WorkerExecutionRequest,
    ) -> tuple[StoredWorkerSession, WorkerExecutionPreflightReport]:
        """Validate preflight + Governance and persist an isolated INITIALIZED Worker Session."""
        preflight = self.run_execution_preflight(request)
        if not preflight.allowed:
            primary_code = preflight.blocking_codes[0] if preflight.blocking_codes else "PREFLIGHT_BLOCKED"
            fail_msgs = [c.message for c in preflight.checks if not c.passed]
            raise WorkerPreflightBlockedError(
                primary_code,
                "; ".join(fail_msgs) or "Worker execution preflight blocked.",
                report=preflight,
            )

        if preflight.idempotent_replay_session_id is not None:
            existing = self._worker_sessions.get_stored_session(preflight.idempotent_replay_session_id)
            if existing is not None:
                return existing, preflight

        stored_task = self._tasks.get_stored_task(request.task_id)
        assert stored_task is not None
        stored_plan = self._plans.get_plan(request.plan_id)
        assert stored_plan is not None
        plan_step = next(s for s in stored_plan.plan.steps if s.step_id == request.step_id)
        pinned_version = self._registry.get_manifest_version(
            request.agent_id,
            preflight.pinned_manifest_revision or 1,
        )
        assert pinned_version is not None

        auth_ctx = resolve_trusted_auth_context(
            auth_context=request.auth_context,
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
        )
        existing_run, existing_run_ver, _, _ = self._resolve_target_run(stored_task.task, request.run_id)
        policy_req = self._build_execute_policy_request(
            request=request,
            auth_context=auth_ctx,
            run_id=existing_run.run_id if existing_run else None,
            workspace_path=preflight.resolved_workspace_path,
            pinned_manifest=pinned_version.manifest,
        )

        def _materialize_session() -> StoredWorkerSession:
            with self._get_task_lock(request.task_id):
                if request.idempotency_key is not None:
                    existing_idem = self._worker_sessions.find_by_idempotency(
                        request.task_id,
                        request.idempotency_key,
                    )
                    if existing_idem is not None:
                        if existing_idem.binding.request_fingerprint != request.compute_request_fingerprint():
                            raise DuplicateInvocationConflictError(
                                "IDEMPOTENCY_CONFLICT",
                                f"Idempotency key '{request.idempotency_key}' conflicts with session '{existing_idem.session.session_id}'.",
                            )
                        return existing_idem

                for attempt in range(3):
                    latest_task = self._tasks.get_stored_task(request.task_id)
                    assert latest_task is not None
                    current_run, current_run_ver, run_err_code, run_err_msg = self._resolve_target_run(
                        latest_task.task,
                        request.run_id,
                    )
                    if run_err_code is not None:
                        raise WorkerPreflightBlockedError(
                            run_err_code,
                            run_err_msg or "Target Run is unavailable.",
                            report=preflight,
                        )

                    correlation_id = request.correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
                    session_id = new_session_id()
                    resolved_provider = preflight.resolved_provider_id or "antigravity"
                    resolved_connection = preflight.resolved_connection_id

                    if current_run is None:
                        run_id = new_run_id()
                        new_run_obj: Run | None = Run(
                            run_id=run_id,
                            task_id=latest_task.task.task_id,
                            runtime_id="worker_session_lifecycle",
                            correlation_id=correlation_id,
                            status="PENDING",
                            session_ids=[session_id],
                            provider_id=resolved_provider,
                            connection_id=resolved_connection,
                            model_id=preflight.resolved_model_id,
                        )
                        updated_run_obj: Run | None = None
                        run_ver: int | None = None
                    else:
                        run_id = current_run.run_id
                        next_session_ids = list(current_run.session_ids)
                        if session_id not in next_session_ids:
                            next_session_ids.append(session_id)
                        new_run_obj = None
                        updated_run_obj = current_run.model_copy(update={"session_ids": next_session_ids})
                        run_ver = current_run_ver

                    session_obj = Session(
                        session_id=session_id,
                        agent_id=request.agent_id,
                        task_id=latest_task.task.task_id,
                        run_id=run_id,
                        provider_id=resolved_provider,
                        connection_id=resolved_connection,
                        workspace_reference=request.workspace_scope,
                        status="INITIALIZED",
                        started_at=None,
                        finished_at=None,
                    )
                    binding_obj = WorkerSessionBinding(
                        session_id=session_id,
                        task_id=latest_task.task.task_id,
                        run_id=run_id,
                        agent_id=request.agent_id,
                        plan_id=stored_plan.plan_id,
                        plan_revision=stored_plan.active_revision,
                        team_proposal_id=request.team_proposal_id,
                        step_id=request.step_id,
                        checklist_item_id=plan_step.checklist_item_id,
                        pinned_manifest_revision=pinned_version.manifest_revision,
                        pinned_manifest_sha256=pinned_version.manifest_sha256,
                        provider_id=resolved_provider,
                        connection_id=resolved_connection,
                        model_id=preflight.resolved_model_id,
                        workspace_scope=request.workspace_scope,
                        private_context_namespace=f"ctx/{latest_task.task.task_id}/{run_id}/{request.agent_id}/{session_id}",
                        idempotency_key=request.idempotency_key,
                        request_fingerprint=request.compute_request_fingerprint() if request.idempotency_key else None,
                        correlation_id=correlation_id,
                        reconciliation_status="NONE",
                        created_at=request.timestamp,
                        updated_at=request.timestamp,
                        last_audit_event_id=None,
                    )
                    init_event = Event(
                        event_id=new_event_id(),
                        timestamp=request.timestamp,
                        event_type="session.initialized",
                        source=_actor_kind_to_event_source(request.actor_kind),
                        task_id=latest_task.task.task_id,
                        run_id=run_id,
                        session_id=session_id,
                        actor_id=request.agent_id,
                        provider_id=resolved_provider,
                        connection_id=resolved_connection,
                        model_id=preflight.resolved_model_id,
                        correlation_id=correlation_id,
                        redacted_payload=sanitize_event_payload(
                            {
                                "previous_status": None,
                                "new_status": "INITIALIZED",
                                "step_id": request.step_id,
                                "plan_id": stored_plan.plan_id,
                                "plan_revision": stored_plan.active_revision,
                                "team_proposal_id": request.team_proposal_id,
                                "pinned_manifest_revision": pinned_version.manifest_revision,
                                "pinned_manifest_sha256": pinned_version.manifest_sha256,
                                "workspace_scope": request.workspace_scope,
                                "private_context_namespace": binding_obj.private_context_namespace,
                            }
                        ),
                    )
                    binding_obj = binding_obj.model_copy(update={"last_audit_event_id": init_event.event_id})
                    transition_rec = WorkerSessionTransitionRecord(
                        session_id=session_id,
                        task_id=latest_task.task.task_id,
                        run_id=run_id,
                        from_status=None,
                        to_status="INITIALIZED",
                        actor_id=request.actor_id,
                        reason=f"Initialized isolated Worker session for step '{request.step_id}'.",
                        evidence_summary=f"preflight=PASS team={request.team_proposal_id} rev=r{pinned_version.manifest_revision}",
                        event_id=init_event.event_id,
                        timestamp=request.timestamp,
                    )
                    try:
                        return self._worker_sessions.create_session_atomic(
                            session=session_obj,
                            binding=binding_obj,
                            transition=transition_rec,
                            events=[init_event],
                            new_run=new_run_obj,
                            updated_run=updated_run_obj,
                            expected_run_version=run_ver,
                        )
                    except Exception as exc:
                        if getattr(exc, "error_code", None) == "STALE_STATE" and attempt < 2:
                            continue
                        raise
                raise WorkerSessionError("STALE_RUN_STATE", "Concurrent Run updates exhausted retries during session init.")

        try:
            if request.approval_id is not None or self._permissions.evaluator.evaluate(policy_req).decision == "APPROVAL_REQUIRED":
                _, created = self._permissions.dispatch_protected_action(policy_req, _materialize_session)
                return created, preflight
            decision = self._permissions.evaluate_and_audit(policy_req)
            if decision.decision != "ALLOW":
                raise WorkerPreflightBlockedError("UNAUTHORIZED_EXECUTION", decision.message, report=preflight)
            return _materialize_session(), preflight
        except PermissionDeniedError as exc:
            raise WorkerPreflightBlockedError("UNAUTHORIZED_EXECUTION", str(exc), report=preflight) from exc
        except GovernanceAuditError as exc:
            raise WorkerSessionError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc

    def transition_session(
        self,
        session_id: str,
        *,
        target_status: SessionStatus,
        actor_id: AgentId,
        actor_kind: ActorTrustKind,
        reason: str,
        evidence_summary: str,
        timestamp: datetime,
        invocation_id: str | None = None,
        provider_conversation_id: str | None = None,
        execution_id: str | None = None,
        extra_events: Sequence[Event] = (),
        updated_run: Run | None = None,
        expected_run_version: int | None = None,
        updated_task: Task | None = None,
        expected_task_version: int | None = None,
    ) -> StoredWorkerSession:
        """Apply a guarded Session state transition with audit Event and optimistic lock."""
        validate_aware_datetime(timestamp, "timestamp")
        ensure_not_secret(reason, "reason")
        ensure_not_secret(evidence_summary, "evidence_summary")
        safe_reason = _truncate_bounded(reason, 500)
        safe_evidence = _truncate_bounded(evidence_summary, 500)

        stored = self._worker_sessions.get_stored_session(session_id)
        if stored is None:
            raise WorkerSessionNotFoundError(
                "SESSION_NOT_FOUND",
                f"Worker session '{session_id}' does not exist.",
            )

        current_session = stored.session
        current_binding = stored.binding
        allowed_targets = VALID_SESSION_TRANSITIONS[current_session.status]
        if target_status not in allowed_targets:
            raise SessionTransitionError(
                "INVALID_SESSION_TRANSITION",
                f"Session '{session_id}' cannot transition {current_session.status} -> {target_status}.",
            )

        effective_ts = _monotonic_ts(timestamp, current_session.started_at or current_binding.created_at)
        next_started_at = current_session.started_at
        next_finished_at = current_session.finished_at
        if target_status == "ACTIVE":
            if next_started_at is None:
                next_started_at = effective_ts
            next_finished_at = None
        elif target_status in TERMINAL_SESSION_STATUSES:
            if next_started_at is None:
                next_started_at = effective_ts
            next_finished_at = _monotonic_ts(effective_ts, next_started_at)

        next_invocation = invocation_id or current_session.invocation_id
        if target_status == "ACTIVE" and next_invocation is None:
            raise SessionTransitionError(
                "MISSING_INVOCATION_EVIDENCE",
                f"Transitioning session '{session_id}' to ACTIVE requires explicit invocation_id.",
            )

        next_conv = provider_conversation_id or current_session.provider_conversation_id
        if next_conv is not None and current_session.provider_id is not None:
            conflict = self._worker_sessions.find_provider_conversation_conflict(
                current_session.provider_id,
                next_conv,
                session_id=session_id,
                task_id=current_binding.task_id,
                agent_id=current_binding.agent_id,
            )
            if conflict is not None:
                raise ProviderConversationCollisionError(
                    "PROVIDER_CONVERSATION_COLLISION",
                    (
                        f"Provider conversation '{next_conv}' on '{current_session.provider_id}' "
                        f"already belongs to session '{conflict.session_id}'."
                    ),
                )

        if target_status == "ACTIVE" and current_session.status == "INITIALIZED":
            event_type = "session.started"
        elif target_status == "CLOSED":
            event_type = "session.closed"
        else:
            event_type = "session.status_changed"

        next_session = current_session.model_copy(
            update={
                "status": target_status,
                "invocation_id": next_invocation,
                "provider_conversation_id": next_conv,
                "started_at": next_started_at,
                "finished_at": next_finished_at,
            }
        )
        Session.model_validate_json(next_session.model_dump_json())

        audit_event = Event(
            event_id=new_event_id(),
            timestamp=effective_ts,
            event_type=event_type,  # type: ignore[arg-type]
            source=_actor_kind_to_event_source(actor_kind),
            task_id=current_binding.task_id,
            run_id=current_binding.run_id,
            session_id=session_id,
            actor_id=current_binding.agent_id,
            provider_id=current_binding.provider_id,
            connection_id=current_binding.connection_id,
            model_id=current_binding.model_id,
            correlation_id=current_binding.correlation_id,
            redacted_payload=sanitize_event_payload(
                {
                    "previous_status": current_session.status,
                    "new_status": target_status,
                    "actor_id": actor_id,
                    "invocation_id": next_invocation,
                    "execution_id": execution_id or current_binding.execution_id,
                    "step_id": current_binding.step_id,
                    "provider_conversation_id": next_conv,
                    "reason": safe_reason,
                    "evidence_summary": safe_evidence,
                }
            ),
        )
        next_binding = current_binding.model_copy(
            update={
                "execution_id": execution_id or current_binding.execution_id,
                "last_audit_event_id": audit_event.event_id,
                "updated_at": effective_ts,
            }
        )
        transition_rec = WorkerSessionTransitionRecord(
            session_id=session_id,
            task_id=current_binding.task_id,
            run_id=current_binding.run_id,
            from_status=current_session.status,
            to_status=target_status,
            actor_id=actor_id,
            reason=safe_reason,
            evidence_summary=safe_evidence,
            event_id=audit_event.event_id,
            timestamp=effective_ts,
        )

        combined_events = list(extra_events) + [audit_event]
        return self._worker_sessions.transition_session_atomic(
            session=next_session,
            binding=next_binding,
            expected_record_version=stored.record_version,
            transition=transition_rec,
            events=combined_events,
            updated_run=updated_run,
            expected_run_version=expected_run_version,
            updated_task=updated_task,
            expected_task_version=expected_task_version,
        )

    def _build_worker_engine_request(
        self,
        *,
        request: WorkerExecutionRequest,
        stored: StoredWorkerSession,
        manifest: AgentManifest,
        step: PlanStepSpec,
        data_locality: Literal["default", "local_only"],
        handoff_packages: Sequence[HandoffContextPackage] = (),
    ) -> EngineRequest:
        """Build an isolated ``EngineRequest`` using only pinned Manifest, Step, and explicit Handoffs."""
        pinned_summary = (
            f"Agent: {manifest.id} ({manifest.role})\n"
            f"Mission: {manifest.mission.purpose}\n"
            f"Responsibilities: {'; '.join(manifest.mission.responsibilities)}\n"
            f"Non-goals: {'; '.join(manifest.mission.non_goals) or 'None'}\n"
            f"Task: {request.task_id} | Run: {stored.binding.run_id} | Session: {stored.session.session_id}\n"
            f"Step: {step.step_id}: {step.title}\n"
            f"Objective: {step.description}\n"
            f"Expected Outputs: {', '.join(step.expected_outputs)}\n"
            f"Acceptance Criteria: {'; '.join(step.acceptance_criteria)}\n"
            f"Workspace Scope: {request.workspace_scope}"
        )
        context_parts: list[str] = [
            f"PRIVATE_NAMESPACE: {stored.binding.private_context_namespace}",
        ]
        if request.additional_context:
            context_parts.append(f"OPERATOR_NOTE:\n{redact_secrets(request.additional_context)}")
        if handoff_packages:
            for idx, pkg in enumerate(handoff_packages, start=1):
                context_parts.append(
                    f"[UNTRUSTED_UPSTREAM_DATA handoff_index={idx} step={pkg.target_step_id}]\n"
                    f"SourceSummary: {redact_secrets(pkg.source_summary)}\n"
                    f"Artifacts: {', '.join(a.artifact_id for a in pkg.artifact_references) or 'none'}\n"
                    f"Notice: {pkg.untrusted_data_notice}\n"
                    f"[/UNTRUSTED_UPSTREAM_DATA]"
                )
        combined_context = "\n\n".join(context_parts)

        conn_id_for_req = (
            stored.binding.connection_id
            if stored.binding.connection_id
            and self._engines.connections.get(stored.binding.connection_id) is not None
            else None
        )
        return EngineRequest(
            provider_id=stored.binding.provider_id,
            adapter_id=manifest.engine.adapter_id,
            connection_id=conn_id_for_req,
            model_id=stored.binding.model_id,
            prompt=pinned_summary,
            context=combined_context,
            workspace_path=str(Path(request.workspace_root) / request.workspace_scope),
            timeout_seconds=max(1, min(600, int(round(request.timeout_seconds)))),
            execution_mode=request.execution_mode,
            data_locality=data_locality,
            session_reference=EngineSessionReference(
                session_id=stored.session.session_id,
                provider_conversation_id=None,
                binding_source="none",
            ),
            denied_paths=list(_SENSITIVE_DENIED_PATHS),
            deny_shell_commands=False,
            metadata={
                "task_id": stored.binding.task_id,
                "run_id": stored.binding.run_id,
                "session_id": stored.session.session_id,
                "agent_id": stored.binding.agent_id,
                "step_id": stored.binding.step_id,
                "pinned_manifest_revision": stored.binding.pinned_manifest_revision,
                "private_context_namespace": stored.binding.private_context_namespace,
                "correlation_id": stored.binding.correlation_id,
            },
        )

    def _validate_handoffs_if_requested(
        self,
        request: WorkerExecutionRequest,
    ) -> list[HandoffContextPackage]:
        """Load and verify explicit Handoff packages before passing upstream data to Worker B."""
        if not request.handoff_ids:
            return []
        if self._handoffs is None:
            raise WorkerPreflightBlockedError(
                "HANDOFF_REPO_NOT_CONFIGURED",
                "handoff_ids supplied but WorkerHandoffRepository is not configured.",
            )
        packages: list[HandoffContextPackage] = []
        for hid in request.handoff_ids:
            stored_hoff = self._handoffs.get_handoff(hid)
            if stored_hoff is None:
                raise WorkerPreflightBlockedError(
                    "HANDOFF_NOT_FOUND",
                    f"Handoff '{hid}' does not exist.",
                )
            rec = stored_hoff.handoff
            if rec.task_id != request.task_id or rec.target_step_id != request.step_id or rec.to_agent_id != request.agent_id:
                raise WorkerPreflightBlockedError(
                    "HANDOFF_OWNERSHIP_MISMATCH",
                    f"Handoff '{hid}' does not target (task='{request.task_id}', step='{request.step_id}', agent='{request.agent_id}').",
                )
            if rec.status not in {"DELIVERED", "ACKNOWLEDGED"}:
                raise WorkerPreflightBlockedError(
                    "HANDOFF_NOT_DELIVERED",
                    f"Handoff '{hid}' has status '{rec.status}', expected DELIVERED or ACKNOWLEDGED.",
                )
            packages.append(rec.context_package)
        return packages

    def execute_worker_step(
        self,
        request: WorkerExecutionRequest,
        *,
        execution_handle: OwnedExecutionHandle | None = None,
        handoff_packages: Sequence[HandoffContextPackage] = (),
        raise_on_block: bool = False,
    ) -> WorkerExecutionOutcome:
        """Execute one isolated, manifest-pinned Worker step through EngineRegistry and persist facts."""
        preflight = self.run_execution_preflight(request)
        if preflight.idempotent_replay_session_id is not None and preflight.allowed:
            replayed = self._worker_sessions.get_stored_session(preflight.idempotent_replay_session_id)
            if replayed is not None:
                return WorkerExecutionOutcome(
                    status="IDEMPOTENT_REPLAY",
                    verification_tier=request.verification_tier,
                    task_id=replayed.binding.task_id,
                    run_id=replayed.binding.run_id,
                    session_id=replayed.session.session_id,
                    invocation_id=replayed.session.invocation_id,
                    provider_conversation_id=replayed.session.provider_conversation_id,
                    session=replayed.session,
                    binding=replayed.binding,
                    preflight=preflight,
                    engine_result=None,
                    recorded_event_ids=[],
                )

        if not preflight.allowed:
            code = preflight.blocking_codes[0] if preflight.blocking_codes else "PREFLIGHT_BLOCKED"
            msg = "; ".join(c.message for c in preflight.checks if not c.passed) or "Worker preflight blocked."
            if raise_on_block:
                raise WorkerPreflightBlockedError(code, msg, report=preflight)
            return WorkerExecutionOutcome(
                status="BLOCKED",
                verification_tier=request.verification_tier,
                task_id=request.task_id,
                run_id=preflight.run_id,
                preflight=preflight,
                error_code=code,
                error_message=msg,
            )

        try:
            loaded_handoff_pkgs = list(handoff_packages) + self._validate_handoffs_if_requested(request)
        except WorkerPreflightBlockedError as exc:
            if raise_on_block:
                raise
            return WorkerExecutionOutcome(
                status="BLOCKED",
                verification_tier=request.verification_tier,
                task_id=request.task_id,
                run_id=preflight.run_id,
                preflight=preflight,
                error_code=exc.error_code,
                error_message=exc.message,
            )

        try:
            stored, preflight = self.initialize_worker_session(request)
        except WorkerPreflightBlockedError as exc:
            if raise_on_block:
                raise
            return WorkerExecutionOutcome(
                status="BLOCKED",
                verification_tier=request.verification_tier,
                task_id=request.task_id,
                run_id=preflight.run_id,
                preflight=preflight,
                error_code=exc.error_code,
                error_message=exc.message,
            )

        session_id = stored.session.session_id
        session_lock = self._get_session_lock(session_id)
        recorded_event_ids: list[EventId] = (
            [stored.binding.last_audit_event_id] if stored.binding.last_audit_event_id else []
        )

        stored_plan = self._plans.get_plan(request.plan_id)
        assert stored_plan is not None
        plan_step = next(s for s in stored_plan.plan.steps if s.step_id == request.step_id)
        pinned_version = self._registry.get_manifest_version(
            request.agent_id,
            stored.binding.pinned_manifest_revision,
        )
        assert pinned_version is not None

        data_locality: Literal["default", "local_only"] = (
            "local_only"
            if (
                (plan_step.routing_proposal and plan_step.routing_proposal.data_residency_policy == "local_only")
                or stored_plan.plan.routing_proposal.data_residency_policy == "local_only"
            )
            else "default"
        )
        engine_request = self._build_worker_engine_request(
            request=request,
            stored=stored,
            manifest=pinned_version.manifest,
            step=plan_step,
            data_locality=data_locality,
            handoff_packages=loaded_handoff_pkgs,
        )

        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        extra_start_events: list[Event] = []

        # Register observable live execution handle if supplied
        if execution_handle is not None:
            with self._lock:
                self._live_handles[session_id] = execution_handle
            if self._lifecycle is not None:
                self._lifecycle.register_owned_execution(
                    stored.binding.task_id,
                    stored.binding.run_id,
                    execution_handle,
                    timestamp=request.timestamp,
                )

        # Serialize Run/Task RUNNING transitions across concurrent workers sharing a Task/Run
        with self._get_task_lock(request.task_id):
            for attempt in range(3):
                stored_task = self._tasks.get_stored_task(request.task_id)
                stored_run = self._runs.get_stored_run(stored.binding.run_id)
                assert stored_task is not None and stored_run is not None

                updated_run: Run | None = None
                expected_run_ver: int | None = None
                updated_task: Task | None = None
                expected_task_ver: int | None = None
                attempt_start_events: list[Event] = []

                if stored_run.run.status == "PENDING" and stored_task.task.status == "ASSIGNED":
                    pair_ctx = TransitionContext(
                        actor_id=request.actor_id,
                        actor_kind=request.actor_kind,
                        timestamp=request.timestamp,
                        reason=f"Start Run '{stored_run.run.run_id}' for Worker '{request.agent_id}' step '{request.step_id}'.",
                        execution_evidence=ExecutionEvidence(
                            source="core_runtime",
                            run_id=stored_run.run.run_id,
                            invocation_id=invocation_id,
                            reason=f"Worker session '{session_id}' starting Engine execution.",
                        ),
                        correlation_id=stored.binding.correlation_id,
                    )
                    pair_res = start_task_run(stored_task.task, stored_run.run, pair_ctx)
                    if not pair_res.accepted or pair_res.run is None or pair_res.task is None:
                        raise WorkerSessionError(
                            pair_res.error.error_code if pair_res.error else "RUN_START_FAILED",
                            pair_res.error.message if pair_res.error else "Failed to transition Task/Run to RUNNING.",
                        )
                    updated_run = pair_res.run
                    expected_run_ver = stored_run.version
                    updated_task = pair_res.task
                    expected_task_ver = stored_task.version
                    attempt_start_events.extend(pair_res.events)
                elif stored_run.run.status == "PENDING":
                    run_ctx = TransitionContext(
                        actor_id=request.actor_id,
                        actor_kind=request.actor_kind,
                        timestamp=request.timestamp,
                        reason=f"Start Run '{stored_run.run.run_id}' for Worker '{request.agent_id}'.",
                        execution_evidence=ExecutionEvidence(
                            source="core_runtime",
                            run_id=stored_run.run.run_id,
                            invocation_id=invocation_id,
                            reason=f"Worker session '{session_id}' starting Engine execution.",
                        ),
                        correlation_id=stored.binding.correlation_id,
                    )
                    run_res = transition_run(
                        RunTransitionRequest(run=stored_run.run, target_status="RUNNING", context=run_ctx)
                    )
                    if not run_res.accepted or run_res.run is None:
                        raise WorkerSessionError(
                            run_res.error.error_code if run_res.error else "RUN_START_FAILED",
                            run_res.error.message if run_res.error else "Failed to transition Run to RUNNING.",
                        )
                    updated_run = run_res.run
                    expected_run_ver = stored_run.version
                    if run_res.event is not None:
                        attempt_start_events.append(run_res.event)

                try:
                    stored = self.transition_session(
                        session_id,
                        target_status="ACTIVE",
                        actor_id=request.actor_id,
                        actor_kind=request.actor_kind,
                        reason=f"Launching EngineAdapter for step '{request.step_id}'.",
                        evidence_summary=f"invocation={invocation_id} provider={stored.binding.provider_id}",
                        timestamp=request.timestamp,
                        invocation_id=invocation_id,
                        execution_id=execution_handle.execution_id if execution_handle else None,
                        extra_events=attempt_start_events,
                        updated_run=updated_run,
                        expected_run_version=expected_run_ver,
                        updated_task=updated_task,
                        expected_task_version=expected_task_ver,
                    )
                    extra_start_events = attempt_start_events
                    break
                except Exception as exc:
                    if getattr(exc, "error_code", None) == "STALE_STATE" and attempt < 2:
                        continue
                    raise
        for ev in extra_start_events:
            recorded_event_ids.append(ev.event_id)
        if stored.binding.last_audit_event_id:
            recorded_event_ids.append(stored.binding.last_audit_event_id)

        last_event_ts = stored.session.started_at or request.timestamp

        def _on_engine_event(eng_ev: EngineEvent) -> None:
            nonlocal last_event_ts
            latest_session = self._worker_sessions.get_stored_session(session_id)
            if latest_session is None or latest_session.session.status in TERMINAL_SESSION_STATUSES:
                logger.warning("Ignoring late EngineEvent for terminal/missing session %s", session_id)
                return
            ev_ts = _monotonic_ts(eng_ev.timestamp, last_event_ts)
            last_event_ts = ev_ts
            payload: dict[str, Any] = {
                "engine_event_type": eng_ev.event_type,
                "engine_status": eng_ev.status,
                "sequence_number": eng_ev.sequence_number,
                "invocation_id": invocation_id,
                "step_id": request.step_id,
                "text_delta": eng_ev.text_delta,
                "tool_name": eng_ev.tool_name,
                "tool_call_id": eng_ev.tool_call_id,
                **eng_ev.redacted_payload,
            }
            if ev_ts != eng_ev.timestamp:
                payload["adapter_timestamp"] = eng_ev.timestamp.isoformat()
            journal_ev = Event(
                event_id=new_event_id(),
                timestamp=ev_ts,
                event_type="engine.event_forwarded",
                source="provider" if eng_ev.source == "provider" else "engine_adapter",
                task_id=stored.binding.task_id,
                run_id=stored.binding.run_id,
                session_id=session_id,
                actor_id=stored.binding.agent_id,
                provider_id=stored.binding.provider_id,
                connection_id=stored.binding.connection_id,
                model_id=stored.binding.model_id,
                correlation_id=stored.binding.correlation_id,
                redacted_payload=sanitize_event_payload(payload),
            )
            appended = self._worker_sessions.append_events_atomic([journal_ev])
            recorded_event_ids.extend(appended)

        engine_result: EngineResult | None = None
        exec_error_code: str | None = None
        exec_error_msg: str | None = None
        try:
            raw_result = self._engines.execute(engine_request, on_event=_on_engine_event)
            if raw_result is None or not isinstance(raw_result, EngineResult):
                exec_error_code = "ENGINE_MISSING_RESULT"
                exec_error_msg = "EngineAdapter returned empty or invalid EngineResult."
            else:
                engine_result = raw_result
        except (ValidationError, ValueError, RuntimeError, OSError, TypeError) as exc:
            exec_error_code = "ENGINE_EXECUTION_ERROR"
            exec_error_msg = _truncate_bounded(
                redact_secrets(f"Engine execution raised {type(exc).__name__}: {exc}"),
                500,
            )
        finally:
            if execution_handle is not None:
                with self._lock:
                    self._live_handles.pop(session_id, None)

        # Serialize terminal state persistence against concurrent cancellation requests
        with session_lock:
            current_stored = self._worker_sessions.get_stored_session(session_id)
            assert current_stored is not None
            if current_stored.session.status in TERMINAL_SESSION_STATUSES:
                # Session already reached terminal state (e.g. concurrent cancel confirmed); never overwrite!
                late_ts = _monotonic_ts(datetime.now(timezone.utc), last_event_ts)
                late_ev = Event(
                    event_id=new_event_id(),
                    timestamp=late_ts,
                    event_type="engine.event_forwarded",
                    source="core_runtime",
                    task_id=current_stored.binding.task_id,
                    run_id=current_stored.binding.run_id,
                    session_id=session_id,
                    actor_id=current_stored.binding.agent_id,
                    provider_id=current_stored.binding.provider_id,
                    connection_id=current_stored.binding.connection_id,
                    model_id=current_stored.binding.model_id,
                    correlation_id=current_stored.binding.correlation_id,
                    redacted_payload=sanitize_event_payload(
                        {
                            "late_callback_ignored": True,
                            "terminal_session_status": current_stored.session.status,
                            "engine_status": engine_result.status if engine_result else exec_error_code,
                        }
                    ),
                )
                recorded_event_ids.extend(self._worker_sessions.append_events_atomic([late_ev]))
                return WorkerExecutionOutcome(
                    status=current_stored.session.status,  # type: ignore[arg-type]
                    verification_tier=request.verification_tier,
                    task_id=current_stored.binding.task_id,
                    run_id=current_stored.binding.run_id,
                    session_id=session_id,
                    invocation_id=current_stored.session.invocation_id,
                    provider_conversation_id=current_stored.session.provider_conversation_id,
                    session=current_stored.session,
                    binding=current_stored.binding,
                    preflight=preflight,
                    engine_result=engine_result,
                    recorded_event_ids=recorded_event_ids,
                    error_code="ALREADY_TERMINAL",
                    error_message=f"Session already terminated ({current_stored.session.status}) before engine return.",
                )

            if engine_result is None:
                finish_ts = _monotonic_ts(datetime.now(timezone.utc), last_event_ts)
                failed_stored = self.transition_session(
                    session_id,
                    target_status="FAILED",
                    actor_id=request.actor_id,
                    actor_kind="core_runtime",
                    reason=exec_error_msg or "Engine returned no result.",
                    evidence_summary=f"error_code={exec_error_code}",
                    timestamp=finish_ts,
                )
                if failed_stored.binding.last_audit_event_id:
                    recorded_event_ids.append(failed_stored.binding.last_audit_event_id)
                return WorkerExecutionOutcome(
                    status="FAILED",
                    verification_tier=request.verification_tier,
                    task_id=failed_stored.binding.task_id,
                    run_id=failed_stored.binding.run_id,
                    session_id=session_id,
                    invocation_id=failed_stored.session.invocation_id,
                    session=failed_stored.session,
                    binding=failed_stored.binding,
                    preflight=preflight,
                    engine_result=None,
                    recorded_event_ids=recorded_event_ids,
                    error_code=exec_error_code,
                    error_message=exec_error_msg,
                )

            observed_conv_id = (
                engine_result.session_reference.provider_conversation_id
                if engine_result.session_reference is not None
                else None
            )
            finish_ts = _monotonic_ts(engine_result.finished_at, last_event_ts)

            # Check provider conversation collision before saving SUCCEEDED/CLOSED state
            if observed_conv_id is not None:
                conflict = self._worker_sessions.find_provider_conversation_conflict(
                    current_stored.binding.provider_id,
                    observed_conv_id,
                    session_id=session_id,
                    task_id=current_stored.binding.task_id,
                    agent_id=current_stored.binding.agent_id,
                )
                if conflict is not None:
                    collision_msg = (
                        f"Provider conversation '{observed_conv_id}' collides with session "
                        f"'{conflict.session_id}' (agent='{conflict.agent_id}', task='{conflict.task_id}')."
                    )
                    failed_stored = self.transition_session(
                        session_id,
                        target_status="FAILED",
                        actor_id=request.actor_id,
                        actor_kind="core_runtime",
                        reason=collision_msg,
                        evidence_summary="error_code=PROVIDER_CONVERSATION_COLLISION",
                        timestamp=finish_ts,
                        provider_conversation_id=None,
                    )
                    if failed_stored.binding.last_audit_event_id:
                        recorded_event_ids.append(failed_stored.binding.last_audit_event_id)
                    return WorkerExecutionOutcome(
                        status="FAILED",
                        verification_tier=request.verification_tier,
                        task_id=failed_stored.binding.task_id,
                        run_id=failed_stored.binding.run_id,
                        session_id=session_id,
                        invocation_id=failed_stored.session.invocation_id,
                        provider_conversation_id=None,
                        session=failed_stored.session,
                        binding=failed_stored.binding,
                        preflight=preflight,
                        engine_result=engine_result,
                        recorded_event_ids=recorded_event_ids,
                        error_code="PROVIDER_CONVERSATION_COLLISION",
                        error_message=collision_msg,
                    )

            if engine_result.status == "SUCCEEDED":
                target_session_status: SessionStatus = "CLOSED"
                end_reason = f"Worker step '{request.step_id}' Engine execution completed cleanly."
                end_err_code: str | None = None
                end_err_msg: str | None = None
            elif engine_result.status == "CANCELLED":
                target_session_status = "TERMINATED"
                end_reason = _truncate_bounded(
                    redact_secrets(engine_result.error.message if engine_result.error else "Execution cancelled."),
                    500,
                )
                end_err_code = engine_result.error.error_code if engine_result.error else "CANCELLED"
                end_err_msg = end_reason
            else:
                target_session_status = "FAILED"
                end_err_code = (
                    engine_result.error.error_code if engine_result.error else f"ENGINE_{engine_result.status}"
                )
                raw_end_reason = (
                    engine_result.error.message
                    if engine_result.error
                    else f"Engine finished with non-success status '{engine_result.status}'."
                )
                end_reason = _truncate_bounded(redact_secrets(raw_end_reason), 500)
                end_err_msg = end_reason

            # Accumulate per-step telemetry onto shared Run under task lock to prevent concurrent StaleStateError
            step_telemetry = _map_engine_telemetry(engine_result)
            with self._get_task_lock(current_stored.binding.task_id):
                final_stored: StoredWorkerSession | None = None
                for attempt in range(3):
                    latest_run = self._runs.get_stored_run(current_stored.binding.run_id)
                    run_update: Run | None = None
                    run_ver_update: int | None = None
                    if latest_run is not None and latest_run.run.status not in TERMINAL_RUN_STATUSES:
                        run_update = latest_run.run.model_copy(
                            update={"telemetry": _merge_run_telemetry(latest_run.run.telemetry, step_telemetry)}
                        )
                        run_ver_update = latest_run.version

                    try:
                        final_stored = self.transition_session(
                            session_id,
                            target_status=target_session_status,
                            actor_id=request.actor_id,
                            actor_kind="core_runtime",
                            reason=end_reason,
                            evidence_summary=(
                                f"engine_status={engine_result.status} adapter={engine_result.adapter_id} "
                                f"telemetry={engine_result.telemetry.telemetry_status}"
                            ),
                            timestamp=finish_ts,
                            provider_conversation_id=observed_conv_id,
                            updated_run=run_update,
                            expected_run_version=run_ver_update,
                        )
                        break
                    except Exception as exc:
                        if getattr(exc, "error_code", None) == "STALE_STATE" and attempt < 2:
                            continue
                        raise
                assert final_stored is not None
            if final_stored.binding.last_audit_event_id:
                recorded_event_ids.append(final_stored.binding.last_audit_event_id)

            return WorkerExecutionOutcome(
                status=target_session_status,  # type: ignore[arg-type]
                verification_tier=request.verification_tier,
                task_id=final_stored.binding.task_id,
                run_id=final_stored.binding.run_id,
                session_id=session_id,
                invocation_id=final_stored.session.invocation_id,
                provider_conversation_id=final_stored.session.provider_conversation_id,
                session=final_stored.session,
                binding=final_stored.binding,
                preflight=preflight,
                engine_result=engine_result,
                recorded_event_ids=recorded_event_ids,
                error_code=end_err_code,
                error_message=end_err_msg,
            )

    def terminate_session(
        self,
        session_id: str,
        *,
        actor_id: AgentId,
        actor_kind: ActorTrustKind,
        auth_context: TrustedAuthContext | None = None,
        reason: str,
        timestamp: datetime,
        owner_token: str | None = None,
        wait: bool = True,
        wait_timeout: float = 5.0,
    ) -> StoredWorkerSession:
        """Terminate an active/idle/initialized Session cleanly only with governance & stop proof."""
        validate_aware_datetime(timestamp, "timestamp")
        ensure_not_secret(reason, "reason")

        stored = self._worker_sessions.get_stored_session(session_id)
        if stored is None:
            raise WorkerSessionNotFoundError(
                "SESSION_NOT_FOUND",
                f"Worker session '{session_id}' does not exist.",
            )

        auth_ctx = resolve_trusted_auth_context(
            auth_context=auth_context,
            actor_id=actor_id,
            actor_kind=actor_kind,
        )
        policy_req = PolicyRequest(
            auth_context=auth_ctx,
            action="runtime.cancel",
            resource=f"task:{stored.binding.task_id}/run:{stored.binding.run_id}/session:{session_id}",
            task_id=stored.binding.task_id,
            run_id=stored.binding.run_id,
            timestamp=timestamp,
            correlation_id=stored.binding.correlation_id,
            parameters={"session_id": session_id, "reason": reason},
        )
        try:
            decision = self._permissions.evaluate_and_audit(policy_req)
            if decision.decision != "ALLOW":
                raise WorkerPreflightBlockedError("UNAUTHORIZED_CANCELLATION", decision.message)
        except PermissionDeniedError as exc:
            raise WorkerPreflightBlockedError("UNAUTHORIZED_CANCELLATION", str(exc)) from exc

        session_lock = self._get_session_lock(session_id)
        with session_lock:
            current = self._worker_sessions.get_stored_session(session_id)
            assert current is not None
            if current.session.status in TERMINAL_SESSION_STATUSES:
                raise SessionTransitionError(
                    "ALREADY_TERMINAL",
                    f"Session '{session_id}' is already terminal ({current.session.status}).",
                )

            with self._lock:
                handle = self._live_handles.get(session_id)

            if current.session.status == "INITIALIZED" and handle is None:
                return self.transition_session(
                    session_id,
                    target_status="TERMINATED",
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    reason=reason,
                    evidence_summary="checkpoint_cancelled_before_engine_start",
                    timestamp=timestamp,
                )

            if handle is None:
                # Process handle unobservable in this process; record reconciliation instead of faking TERMINATED
                recon_ts = _monotonic_ts(timestamp, current.binding.updated_at)
                recon_ev = Event(
                    event_id=new_event_id(),
                    timestamp=recon_ts,
                    event_type="workflow.reconciliation_recorded",
                    source=_actor_kind_to_event_source(actor_kind),
                    task_id=current.binding.task_id,
                    run_id=current.binding.run_id,
                    session_id=session_id,
                    actor_id=current.binding.agent_id,
                    correlation_id=current.binding.correlation_id,
                    redacted_payload=sanitize_event_payload(
                        {
                            "reason": reason,
                            "reconciliation_status": "NEEDS_RECONCILIATION",
                            "observable_process_status": "UNOBSERVABLE_AFTER_RESTART",
                        }
                    ),
                )
                self._worker_sessions.record_reconciliation_atomic(
                    session_id,
                    expected_record_version=current.record_version,
                    reconciliation_status="NEEDS_RECONCILIATION",
                    reconciliation_reason="Cannot terminate ACTIVE/IDLE session without observable execution handle.",
                    timestamp=recon_ts,
                    event=recon_ev,
                )
                raise SessionTransitionError(
                    "PROCESS_NOT_OBSERVABLE",
                    f"Session '{session_id}' has no observable live process handle; marked NEEDS_RECONCILIATION.",
                )

            if owner_token is not None and owner_token != handle.owner_token:
                raise SessionTransitionError(
                    "OWNERSHIP_REJECTED",
                    "Caller owner_token does not match active execution handle owner.",
                )

            outcome = handle.request_cancel(
                owner_token=handle.owner_token,
                wait=wait,
                timeout_seconds=wait_timeout,
            )
            if not outcome.confirmed_stopped:
                raise SessionTransitionError(
                    "CANCELLATION_NOT_CONFIRMED",
                    outcome.message or f"Execution handle for session '{session_id}' did not confirm stop.",
                )

            return self.transition_session(
                session_id,
                target_status="TERMINATED",
                actor_id=actor_id,
                actor_kind=actor_kind,
                reason=reason,
                evidence_summary=f"handle_stop_confirmed owner={handle.execution_id}",
                timestamp=timestamp,
            )

    def reconcile_worker_sessions(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        actor_id: AgentId = "core.master",
        timestamp: datetime,
    ) -> WorkerSessionRecoveryReport:
        """Inspect persisted Worker Sessions after restart and detect unobservable/drifted states."""
        validate_aware_datetime(timestamp, "timestamp")
        sessions = self._worker_sessions.list_stored_sessions(task_id=task_id, run_id=run_id)
        items: list[WorkerSessionRecoveryItem] = []
        inconsistencies: list[str] = []
        stale_warnings: list[str] = []
        needs_recon = False

        for stored in sessions:
            s = stored.session
            b = stored.binding
            live_agent = self._registry.get_agent(b.agent_id)
            worker_disabled = live_agent is None or live_agent.registration_status != "ACTIVE"
            manifest_drift = (
                live_agent is None
                or live_agent.active_revision != b.pinned_manifest_revision
                or live_agent.manifest_sha256 != b.pinned_manifest_sha256
            )
            if worker_disabled:
                stale_warnings.append(
                    f"Session '{s.session_id}' Worker '{b.agent_id}' is currently not ACTIVE in Registry."
                )
            elif manifest_drift and live_agent is not None:
                stale_warnings.append(
                    f"Session '{s.session_id}' pinned r{b.pinned_manifest_revision} differs from live r{live_agent.active_revision}."
                )

            with self._lock:
                live_handle = self._live_handles.get(s.session_id)

            if s.status in TERMINAL_SESSION_STATUSES:
                obs_status = "TERMINAL"
                item_recon = False
                item_reason = f"Session reached terminal state '{s.status}'."
            elif live_handle is not None and live_handle.is_running():
                obs_status = "LIVE_OBSERVABLE"
                item_recon = False
                item_reason = "Live owned execution handle is actively running (not auto-killed or auto-upgraded)."
            elif s.status in {"ACTIVE", "IDLE"}:
                obs_status = "UNOBSERVABLE_AFTER_RESTART"
                item_recon = True
                needs_recon = True
                item_reason = (
                    f"Session persisted as '{s.status}' before restart with no observable process handle; "
                    "manual reconciliation required without auto-retry."
                )
                if b.reconciliation_status != "NEEDS_RECONCILIATION":
                    recon_ts = _monotonic_ts(timestamp, b.updated_at)
                    recon_ev = Event(
                        event_id=new_event_id(),
                        timestamp=recon_ts,
                        event_type="workflow.reconciliation_recorded",
                        source="core_runtime",
                        task_id=b.task_id,
                        run_id=b.run_id,
                        session_id=s.session_id,
                        actor_id=actor_id,
                        correlation_id=b.correlation_id,
                        redacted_payload=sanitize_event_payload(
                            {
                                "reconciliation_status": "NEEDS_RECONCILIATION",
                                "last_persisted_status": s.status,
                                "observable_process_status": obs_status,
                                "worker_disabled": worker_disabled,
                                "manifest_drift_detected": manifest_drift,
                            }
                        ),
                    )
                    stored = self._worker_sessions.record_reconciliation_atomic(
                        s.session_id,
                        expected_record_version=stored.record_version,
                        reconciliation_status="NEEDS_RECONCILIATION",
                        reconciliation_reason=item_reason,
                        timestamp=recon_ts,
                        event=recon_ev,
                    )
            else:
                obs_status = "INITIALIZED_NOT_STARTED"
                item_recon = worker_disabled or manifest_drift
                if item_recon:
                    needs_recon = True
                    item_reason = (
                        "Session INITIALIZED before Worker disable/manifest drift; blocked from new start without re-pinning."
                    )
                    if b.reconciliation_status != "NEEDS_RECONCILIATION":
                        recon_ts = _monotonic_ts(timestamp, b.updated_at)
                        recon_ev = Event(
                            event_id=new_event_id(),
                            timestamp=recon_ts,
                            event_type="workflow.reconciliation_recorded",
                            source="core_runtime",
                            task_id=b.task_id,
                            run_id=b.run_id,
                            session_id=s.session_id,
                            actor_id=actor_id,
                            correlation_id=b.correlation_id,
                            redacted_payload=sanitize_event_payload(
                                {
                                    "reconciliation_status": "NEEDS_RECONCILIATION",
                                    "last_persisted_status": s.status,
                                    "observable_process_status": obs_status,
                                    "worker_disabled": worker_disabled,
                                    "manifest_drift_detected": manifest_drift,
                                }
                            ),
                        )
                        self._worker_sessions.record_reconciliation_atomic(
                            s.session_id,
                            expected_record_version=stored.record_version,
                            reconciliation_status="NEEDS_RECONCILIATION",
                            reconciliation_reason=item_reason,
                            timestamp=recon_ts,
                            event=recon_ev,
                        )
                else:
                    item_reason = "Session initialized and awaiting governed launch."

            items.append(
                WorkerSessionRecoveryItem(
                    session_id=s.session_id,
                    task_id=b.task_id,
                    run_id=b.run_id,
                    agent_id=b.agent_id,
                    step_id=b.step_id,
                    last_persisted_status=s.status,
                    observable_process_status=obs_status,  # type: ignore[arg-type]
                    reconciliation_required=item_recon,
                    manifest_drift_detected=manifest_drift,
                    worker_disabled=worker_disabled,
                    reason=item_reason,
                )
            )

        refreshed = self._worker_sessions.list_stored_sessions(task_id=task_id, run_id=run_id)
        return WorkerSessionRecoveryReport(
            task_id=task_id,
            run_id=run_id,
            recovery_status="NEEDS_RECONCILIATION" if needs_recon else "VERIFIED_CLEAN",
            sessions=refreshed,
            items=items,
            inconsistencies=inconsistencies,
            stale_manifest_warnings=stale_warnings,
        )

    def get_worker_runtime_state(
        self,
        agent_id: str,
        *,
        task_id: str | None = None,
        team_proposal_id: str | None = None,
    ) -> WorkerRuntimeStateSnapshot:
        """Return a read-only projection strictly separating Registry, Assignment, Session, and QA truth."""
        reg = self._registry.get_agent(agent_id)
        assigned_steps: list[str] = []
        is_assigned = False

        if task_id is not None:
            stored_team = (
                self._teams.get_proposal(team_proposal_id)
                if team_proposal_id is not None
                else self._teams.get_active_proposal_for_task(task_id)
            )
            if stored_team is not None and stored_team.status == "ASSIGNED":
                assigned_steps = [
                    sa.step_id
                    for sa in stored_team.proposal.step_assignments
                    if sa.assigned_agent_id == agent_id and sa.status == "ASSIGNED_IN_PROPOSAL"
                ]
                is_assigned = len(assigned_steps) > 0
            else:
                task_obj = self._tasks.get_task(task_id)
                if task_obj is not None and agent_id in task_obj.assigned_agent_ids:
                    is_assigned = True

        sessions = self._worker_sessions.list_stored_sessions(task_id=task_id, agent_id=agent_id)
        latest = sessions[-1] if sessions else None
        run_status = None
        if latest is not None:
            run_rec = self._runs.get_run(latest.binding.run_id)
            if run_rec is not None:
                run_status = run_rec.status

        with self._lock:
            observable_running = bool(
                latest is not None
                and latest.session.status == "ACTIVE"
                and latest.session.session_id in self._live_handles
                and self._live_handles[latest.session.session_id].is_running()
            )

        artifacts = self._metadata.list_artifacts(task_id=task_id) if task_id is not None else []
        worker_artifacts = [art for art in artifacts if art.owner_agent_id == agent_id]

        return WorkerRuntimeStateSnapshot(
            agent_id=agent_id,
            task_id=task_id,
            in_registry=reg is not None,
            registry_status=reg.registration_status if reg is not None else None,
            is_assigned_to_task=is_assigned,
            assigned_step_ids=assigned_steps,
            session_id=latest.session.session_id if latest is not None else None,
            session_status=latest.session.status if latest is not None else None,
            session_initialized=latest is not None,
            session_active=bool(latest is not None and latest.session.status == "ACTIVE"),
            observable_running=observable_running,
            run_id=latest.binding.run_id if latest is not None else None,
            run_status=run_status,
            output_artifact_ids=[art.artifact_id for art in worker_artifacts],
            qa_verified_artifact_ids=[
                art.artifact_id
                for art in worker_artifacts
                if art.verification_status == "VERIFIED_PASS"
            ],
            reconciliation_required=bool(
                latest is not None and latest.binding.reconciliation_status == "NEEDS_RECONCILIATION"
            ),
            reconciliation_reason=latest.binding.reconciliation_reason if latest is not None else None,
        )

    def invoke_session_tool(
        self,
        session_id: str,
        *,
        tool_id: str,
        parameters: dict[str, Any],
        workspace_root: str,
        timestamp: datetime,
        approval_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        """Invoke a Phase 07 tool strictly within a pinned ACTIVE Worker session."""
        if self._tool_runtime is None:
            raise WorkerSessionError(
                "TOOL_RUNTIME_UNCONFIGURED",
                "WorkerSessionLifecycleService requires tool_runtime to invoke tools.",
            )
        validate_aware_datetime(timestamp, "timestamp")
        stored = self._worker_sessions.get_stored_session(session_id)
        if stored is None:
            raise WorkerSessionNotFoundError("SESSION_NOT_FOUND", f"Worker session '{session_id}' not found.")
        if stored.session.status != "ACTIVE":
            raise SessionTransitionError(
                "SESSION_NOT_ACTIVE",
                f"Worker session '{session_id}' is '{stored.session.status}', must be ACTIVE to invoke tools.",
            )
        binding = stored.binding
        pinned = self._registry.get_manifest_version(binding.agent_id, binding.pinned_manifest_revision)
        if pinned is None:
            raise WorkerSessionError(
                "MANIFEST_REVISION_MISSING",
                f"Pinned manifest '{binding.agent_id}@r{binding.pinned_manifest_revision}' not found.",
            )
        auth_ctx = TrustedAuthContext(
            actor_id=binding.agent_id,
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=list(pinned.manifest.tools.allowed),
            assigned_task_ids=[binding.task_id],
        )
        from core.tools.models import ToolInvocationRequest

        req = ToolInvocationRequest(
            auth_context=auth_ctx,
            tool_id=tool_id,
            parameters=parameters,
            task_id=binding.task_id,
            run_id=binding.run_id,
            session_id=binding.session_id,
            workspace_root=workspace_root,
            approval_id=approval_id,
            correlation_id=binding.correlation_id,
            timestamp=timestamp,
        )
        return self._tool_runtime.invoke(req, is_cancelled=is_cancelled)

    def register_session_artifact(
        self,
        session_id: str,
        *,
        logical_key: str,
        relative_path: str,
        timestamp: datetime,
        source_path: Path | str | None = None,
        content_bytes: bytes | None = None,
        content_type: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Register an artifact in Phase 08 Vault anchored to an ACTIVE/CLOSED Worker session."""
        if self._artifact_vault is None:
            raise WorkerSessionError(
                "ARTIFACT_VAULT_UNCONFIGURED",
                "WorkerSessionLifecycleService requires artifact_vault to register artifacts.",
            )
        validate_aware_datetime(timestamp, "timestamp")
        stored = self._worker_sessions.get_stored_session(session_id)
        if stored is None:
            raise WorkerSessionNotFoundError("SESSION_NOT_FOUND", f"Worker session '{session_id}' not found.")
        if stored.session.status not in {"ACTIVE", "CLOSED"}:
            raise SessionTransitionError(
                "INVALID_SESSION_STATE",
                f"Worker session '{session_id}' is '{stored.session.status}', must be ACTIVE or CLOSED.",
            )
        binding = stored.binding
        auth_ctx = TrustedAuthContext(
            actor_id=binding.agent_id,
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=[binding.task_id],
        )
        return self._artifact_vault.register_artifact(
            auth_context=auth_ctx,
            task_id=binding.task_id,
            run_id=binding.run_id,
            owner_agent_id=binding.agent_id,
            logical_key=logical_key,
            relative_path=relative_path,
            timestamp=timestamp,
            source_path=source_path,
            content_bytes=content_bytes,
            content_type=content_type,
            tags=tags,
            metadata=metadata,
        )
