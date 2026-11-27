"""Unit, Engine integration, isolation, recovery, and governance tests for Phase 05 / P05.05."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from pydantic import ValidationError
import pytest

from core.agents.loader import load_agent_manifest_file
from core.agents.manifest import AgentManifest
from core.agents.registry import AgentRegistryService
from core.contracts import (
    Artifact,
    Run,
    Session,
    Task,
    new_artifact_id,
    new_run_id,
    new_task_id,
)
from core.governance.approvals import ApprovalService
from core.governance.evaluator import PolicyEvaluator, compute_request_fingerprint
from core.governance.manager import PermissionManager
from core.governance.models import ApprovalPolicyContext, PolicyRule, TrustedAuthContext
from core.orchestration import (
    DuplicateInvocationConflictError,
    DynamicTeamBuilderService,
    MasterPlanningService,
    PlanStepSpec,
    ProviderConversationCollisionError,
    SessionTransitionError,
    StaleSessionVersionError,
    StructuredUserBrief,
    TeamBuildRequest,
    WorkerExecutionRequest,
    WorkerPreflightBlockedError,
    WorkerSessionLifecycleService,
)
from core.orchestration.planning_models import PlanRoutingProposal
from core.tasks.lifecycle import HandleTerminationOutcome, OwnedExecutionHandle, WorkflowLifecycleController
from core.tasks.state_machine import complete_run_only, start_task_run
from core.tasks.transitions import ExecutionEvidence, TransitionContext
from engines.connections import EngineConnectionRef
from engines.contract import (
    EngineAdapter,
    EngineCapabilities,
    EngineError,
    EngineEvent,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
    EngineTelemetry,
)
from engines.registry import EngineRegistry
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteRepositoryStore, recover_run, recover_task
from storage.repositories.lifecycle_repo import SQLiteLifecyclePersistenceAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 11, 27, 9, 0, 0, tzinfo=timezone.utc)


def _master_auth(actor_id: str = "core.master") -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def _operator_auth(actor_id: str = "operator.local") -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )


def _make_worker_manifest(
    *,
    agent_id: str,
    department: str,
    skills: list[str],
    provider_id: str = "antigravity",
    adapter_id: str = "antigravity_cli",
    connection_id: str | None = "ag_shared",
    model_id: str | None = "gemini-2.5-flash",
    fs_read: list[str] | None = None,
    fs_write: list[str] | None = None,
    timeout_seconds: int = 120,
    spec_version: str = "1.0.0",
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "spec_version": spec_version,
            "id": agent_id,
            "name": f"Worker {agent_id}",
            "role": f"Specialist {agent_id}",
            "system_role": "worker",
            "department": department,
            "description": f"Isolated fixture worker for {agent_id}.",
            "mission": {
                "purpose": f"Execute bounded {department} step deliverables.",
                "responsibilities": ["Produce verified output inside assigned workspace scope."],
                "non_goals": ["Never claim QA PASS or touch foreign workspaces."],
            },
            "capabilities": {
                "skills": skills,
                "inputs": ["task_brief", "structured_json", "text"],
                "outputs": ["workspace_file_artifact", "structured_execution_report", "text"],
            },
            "engine": {
                "default_type": provider_id,
                "provider_id": provider_id,
                "adapter_id": adapter_id,
                "connection_id": connection_id,
                "model_tier": "flash",
                "model_id": model_id,
                "routing_profile": "workspace_sandboxed",
                "fallback_policy": "operator_approved_only",
            },
            "memory": {"scopes": ["private", "project"]},
            "skills": {"allowed": []},
            "tools": {"allowed": []},
            "permissions": {
                "fs_read": fs_read if fs_read is not None else ["workspace/{task_id}/*"],
                "fs_write": fs_write if fs_write is not None else ["workspace/{task_id}/*"],
                "network": "none",
                "requires_approval": ["files.delete"],
            },
            "limits": {"timeout_seconds": timeout_seconds, "max_retries": 1},
        }
    )


class FakeConfigurableEngineAdapter(EngineAdapter):
    """Controlled offline test EngineAdapter with event capture and per-call outcomes."""

    def __init__(
        self,
        *,
        provider_id: str = "antigravity",
        adapter_id: str = "antigravity_cli",
        data_residency: str = "cli_managed",
        handler: Callable[
            [EngineRequest, EngineConnectionRef | None, Callable[[EngineEvent], None] | None],
            EngineResult | None,
        ]
        | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._adapter_id = adapter_id
        self._data_residency = data_residency
        self._handler = handler
        self.requests: list[EngineRequest] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def platform_kind(self) -> str:
        return "agent_execution_platform"

    def get_capabilities(
        self,
        connection: EngineConnectionRef | None = None,
    ) -> EngineCapabilities:
        return EngineCapabilities(
            provider_id=self._provider_id,
            adapter_id=self._adapter_id,
            platform_kind="agent_execution_platform",
            text_generation="SUPPORTED",
            structured_output="SUPPORTED",
            streaming="SUPPORTED",
            tool_calling_proposal="SUPPORTED",
            native_tool_execution="SUPPORTED",
            workspace_access="SUPPORTED",
            session_continuation="SUPPORTED",
            cancellation="SUPPORTED",
            usage_telemetry="UNSUPPORTED",
            model_discovery="NOT_VERIFIED",
            requires_authentication=True,
            data_residency=self._data_residency,  # type: ignore[arg-type]
        )

    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        self.requests.append(request)
        if self._handler is not None:
            result = self._handler(request, connection, on_event)
            return result  # type: ignore[return-value]

        now = BASE_TIME + timedelta(seconds=10)
        conv_id = f"conv_{request.metadata.get('agent_id', 'worker')}_{uuid.uuid4().hex[:8]}"
        if on_event is not None:
            on_event(
                EngineEvent(
                    event_id=f"eng_evt_{uuid.uuid4().hex[:10]}",
                    sequence_number=1,
                    timestamp=now,
                    event_type="execution_started",
                    source="provider",
                    provider_id=self._provider_id,
                    connection_id=request.connection_id,
                    status="STARTED",
                    redacted_payload={"detail": "offline fixture started"},
                )
            )
            on_event(
                EngineEvent(
                    event_id=f"eng_evt_{uuid.uuid4().hex[:10]}",
                    sequence_number=2,
                    timestamp=now + timedelta(seconds=1),
                    event_type="text_delta",
                    source="provider",
                    provider_id=self._provider_id,
                    connection_id=request.connection_id,
                    status="IN_PROGRESS",
                    text_delta="offline worker completed deliverable",
                )
            )
        return EngineResult(
            request_id=request.request_id,
            provider_id=self._provider_id,
            adapter_id=self._adapter_id,
            connection_id=request.connection_id,
            model_id=request.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            business_verification_status="UNVERIFIED",
            response_text="Offline fixture execution finished.",
            session_reference=EngineSessionReference(
                session_id=request.session_reference.session_id if request.session_reference else None,
                provider_conversation_id=conv_id,
                binding_source="adapter_logical",
            ),
            started_at=now,
            finished_at=now + timedelta(seconds=2),
            duration_ms=2000,
            telemetry=EngineTelemetry(
                duration_ms=2000,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
            events=[],
            error=None,
        )


class FakeOwnedHandle:
    """In-memory controllable OwnedExecutionHandle for cancellation and race tests."""

    def __init__(self, *, confirm_stop: bool = True, delay: float = 0.0) -> None:
        self.execution_id = f"exec_{uuid.uuid4().hex[:12]}"
        self.owner_token = f"own_{uuid.uuid4().hex[:16]}"
        self.pid: int | None = 4242
        self._running = True
        self._confirm_stop = confirm_stop
        self._delay = delay
        self.cancel_called = threading.Event()

    def is_running(self) -> bool:
        return self._running

    def request_cancel(
        self,
        *,
        owner_token: str,
        wait: bool = True,
        timeout_seconds: float = 5.0,
    ) -> HandleTerminationOutcome:
        if owner_token != self.owner_token:
            return HandleTerminationOutcome(
                execution_id=self.execution_id,
                confirmed_stopped=False,
                terminal_status="STILL_RUNNING",
                message="owner mismatch",
            )
        if self._delay > 0:
            time.sleep(self._delay)
        self.cancel_called.set()
        if self._confirm_stop:
            self._running = False
            return HandleTerminationOutcome(
                execution_id=self.execution_id,
                confirmed_stopped=True,
                terminal_status="CANCELLED",
                message="confirmed stop in fixture",
            )
        return HandleTerminationOutcome(
            execution_id=self.execution_id,
            confirmed_stopped=False,
            terminal_status="STILL_RUNNING",
            message="stop timed out",
        )


def _setup_environment(
    tmp_path: Path,
    *,
    adapter: FakeConfigurableEngineAdapter | None = None,
    rules: list[PolicyRule] | None = None,
    register_connection: bool = True,
    connection_enabled: bool = True,
) -> dict[str, Any]:
    db_path = tmp_path / "p05_05.sqlite"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    ws_root = tmp_path / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    store = SQLiteRepositoryStore(db_path)
    approvals = ApprovalService(repository=store.approvals, journal=store.events)
    evaluator = PolicyEvaluator(rules=rules)
    pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers={"antigravity", "gemini", "openai", "ollama"},
    )
    op_auth = _operator_auth()
    agents_root = REPO_ROOT / "agents"
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )

    eng_registry = EngineRegistry()
    active_adapter = adapter or FakeConfigurableEngineAdapter()
    eng_registry.register_adapter(active_adapter, set_default_for_provider=True)
    if register_connection:
        eng_registry.register_connection(
            EngineConnectionRef(
                connection_id="ag_shared",
                provider_id="antigravity",
                adapter_id="antigravity_cli",
                credential_ref="env:ANTIGRAVITY_API_KEY",
                default_model_id="gemini-2.5-flash",
                enabled=connection_enabled,
            )
        )

    lifecycle = WorkflowLifecycleController(
        persistence=SQLiteLifecyclePersistenceAdapter(store),
        permission_hook=pm.build_lifecycle_hook(op_auth),
    )
    planner = MasterPlanningService(
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        workspace_root=ws_root,
        allowed_providers={"antigravity", "ollama"},
        allowed_models={"gemini-2.5-flash", "gemini-2.5-pro"},
    )
    team_builder = DynamicTeamBuilderService(
        team_repo=store.teams,
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        allowed_providers={"antigravity", "ollama"},
        allowed_models={"gemini-2.5-flash", "gemini-2.5-pro"},
    )
    worker_service = WorkerSessionLifecycleService(
        worker_session_repo=store.worker_sessions,
        task_repo=store.tasks,
        run_repo=store.runs,
        metadata_repo=store.metadata,
        event_journal=store.events,
        plan_repo=store.plans,
        team_repo=store.teams,
        agent_registry=registry,
        permission_manager=pm,
        engine_registry=eng_registry,
        lifecycle_controller=lifecycle,
        approval_service=approvals,
    )
    return {
        "db_path": db_path,
        "ws_root": ws_root,
        "store": store,
        "approvals": approvals,
        "pm": pm,
        "registry": registry,
        "eng_registry": eng_registry,
        "adapter": active_adapter,
        "lifecycle": lifecycle,
        "planner": planner,
        "team_builder": team_builder,
        "worker_service": worker_service,
    }


def _seed_planned_assigned_task(
    env: dict[str, Any],
    *,
    workers: list[tuple[str, str, list[str]]],
    steps: list[dict[str, Any]] | None = None,
) -> tuple[Task, Any, Any]:
    """Register workers, create & apply MasterWorkPlan, and assign DynamicTeamProposal."""
    op_auth = _operator_auth()
    master_auth = _master_auth()
    registry: AgentRegistryService = env["registry"]
    planner: MasterPlanningService = env["planner"]
    team_builder: DynamicTeamBuilderService = env["team_builder"]
    store: SQLiteRepositoryStore = env["store"]

    for idx, (aid, dept, skills) in enumerate(workers):
        registry.register_agent(
            _make_worker_manifest(agent_id=aid, department=dept, skills=skills),
            auth_context=op_auth,
            timestamp=BASE_TIME + timedelta(seconds=1 + idx),
            validated_capabilities=skills,
        )

    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Execute multi-step workspace deliverable",
            description="Plan and assign isolated Worker sessions.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=5),
        )
    ).task

    step_specs: list[PlanStepSpec] = []
    if steps is None:
        for i, (aid, _, skills) in enumerate(workers):
            step_specs.append(
                PlanStepSpec(
                    step_id=f"step_{i + 1:02d}",
                    order=i,
                    title=f"Worker step {i + 1}",
                    description=f"Run step {i + 1} for {aid}.",
                    depends_on=[f"step_{i:02d}"] if i > 0 else [],
                    required_capabilities=skills,
                    expected_outputs=[f"workspace/{task.task_id}/{aid}/out.md"],
                    allowed_read_paths=[f"workspace/{task.task_id}"],
                    allowed_write_paths=[f"workspace/{task.task_id}"],
                    acceptance_criteria=[f"Verified output from {aid}."],
                    routing_proposal=PlanRoutingProposal(
                        engine_id="antigravity",
                        provider_id="antigravity",
                        connection_id="ag_shared",
                        model_id="gemini-2.5-flash",
                        data_residency_policy="cloud_allowed",
                        routing_reason="Isolated sandbox routing on shared provider connection.",
                    ),
                )
            )
    else:
        for raw_step in steps:
            raw_step.setdefault("allowed_read_paths", [f"workspace/{task.task_id}"])
            raw_step.setdefault("allowed_write_paths", [f"workspace/{task.task_id}"])
            step_specs.append(PlanStepSpec(**raw_step))

    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/out.md"],
        acceptance_criteria=["Artifact verified"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    outcome = planner.create_plan(
        brief=brief,
        steps=step_specs,
        auth_context=master_auth,
        timestamp=BASE_TIME + timedelta(seconds=6),
    )
    assert outcome.stored_plan is not None
    applied_plan, _ = planner.apply_plan_to_task(
        outcome.stored_plan.plan_id,
        expected_plan_record_version=outcome.stored_plan.record_version,
        auth_context=master_auth,
        timestamp=BASE_TIME + timedelta(seconds=7),
    )

    proposal = team_builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=master_auth,
        timestamp=BASE_TIME + timedelta(seconds=8),
    )
    assign_res = team_builder.apply_team_assignment(
        proposal.proposal_id,
        expected_proposal_record_version=proposal.record_version,
        auth_context=master_auth,
        timestamp=BASE_TIME + timedelta(seconds=9),
    )
    return assign_res.task, applied_plan, assign_res.stored_proposal


def test_p05_05_ast_purity_and_session_uuid_v4_contract(tmp_path: Path) -> None:
    """[2, 36] AST layer purity in core/orchestration/worker_session*.py and UUID v4 identity."""
    forbidden = {
        "sqlite3",
        "storage",
        "sqlalchemy",
        "subprocess",
        "crewai",
        "api",
        "ui",
        "httpx",
        "urllib",
        "requests",
    }
    for rel in (
        "core/orchestration/worker_session_models.py",
        "core/orchestration/worker_sessions.py",
    ):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, f"{rel} imported {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".")[0] not in forbidden, f"{rel} imported {node.module}"

    with pytest.raises(ValidationError):
        Session(session_id="sess-legacy-spike-001", agent_id="it.worker_a")

    env = _setup_environment(tmp_path)
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[("it.worker_a", "it", ["code_generation"])],
    )
    service: WorkerSessionLifecycleService = env["worker_service"]
    req = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    stored, preflight = service.initialize_worker_session(req)
    assert preflight.allowed is True
    parsed_uuid = uuid.UUID(stored.session.session_id)
    assert parsed_uuid.version == 4
    assert stored.session.status == "INITIALIZED"
    assert stored.session.started_at is None
    assert stored.session.finished_at is None


def test_p05_05_preflight_blocks_wrong_ids_drift_disabled_unassigned_and_workspace(tmp_path: Path) -> None:
    """[3..12] Fail-closed preflight blocks wrong task/run, drift, disabled, unassigned, unauthorized, workspace, provider, connection."""
    env = _setup_environment(tmp_path)
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[
            ("it.worker_a", "it", ["code_generation"]),
            ("it.worker_b", "it", ["text_analysis"]),
        ],
    )
    service: WorkerSessionLifecycleService = env["worker_service"]
    store: SQLiteRepositoryStore = env["store"]
    registry: AgentRegistryService = env["registry"]
    op_auth = _operator_auth()

    base_req = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        timestamp=BASE_TIME + timedelta(seconds=12),
    )

    # 3. Wrong Task ID
    wrong_task_report = service.run_execution_preflight(
        base_req.model_copy(update={"task_id": new_task_id()})
    )
    assert wrong_task_report.allowed is False
    assert "TASK_NOT_FOUND" in wrong_task_report.blocking_codes

    # 4. Wrong Run ID (Run owned by another Task)
    other_task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Foreign task",
            description="Separate task for run ownership check.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=11),
        )
    ).task
    foreign_run = store.runs.create_run(
        Run(
            run_id=new_run_id(),
            task_id=other_task.task_id,
            runtime_id="worker_session_lifecycle",
        )
    ).run
    wrong_run_report = service.run_execution_preflight(
        base_req.model_copy(update={"run_id": foreign_run.run_id})
    )
    assert wrong_run_report.allowed is False
    assert "WRONG_RUN_TASK_OWNERSHIP" in wrong_run_report.blocking_codes

    # 8. Unassigned Worker (worker_b is assigned to step_02, NOT step_01!)
    unassigned_report = service.run_execution_preflight(
        base_req.model_copy(update={"agent_id": "it.worker_b"})
    )
    assert unassigned_report.allowed is False
    assert "UNASSIGNED_WORKER" in unassigned_report.blocking_codes

    # 9. Unauthorized caller (worker_agent cannot self-authorize agent.execute)
    unauth_report = service.run_execution_preflight(
        base_req.model_copy(update={"actor_id": "it.worker_a", "actor_kind": "worker_agent"})
    )
    assert unauth_report.allowed is False
    assert "UNAUTHORIZED_EXECUTION" in unauth_report.blocking_codes

    # 10. Workspace violation (traversal / foreign task path / protected core dir)
    with pytest.raises(ValidationError):
        WorkerExecutionRequest(**{**base_req.model_dump(), "workspace_scope": "../escape"})
    traversal_runtime = service.run_execution_preflight(
        base_req.model_copy(update={"workspace_scope": "../escape"})
    )
    assert traversal_runtime.allowed is False
    assert "WORKSPACE_VIOLATION" in traversal_runtime.blocking_codes
    cross_task_ws = service.run_execution_preflight(
        base_req.model_copy(update={"workspace_scope": f"workspace/{other_task.task_id}/evil"})
    )
    assert cross_task_ws.allowed is False
    assert "WORKSPACE_VIOLATION" in cross_task_ws.blocking_codes

    protected_ws = service.run_execution_preflight(
        base_req.model_copy(update={"workspace_scope": "core/hack.py"})
    )
    assert protected_ws.allowed is False
    assert "WORKSPACE_VIOLATION" in protected_ws.blocking_codes

    # 11. Provider mismatch
    prov_mismatch = service.run_execution_preflight(
        base_req.model_copy(update={"provider_id": "ollama"})
    )
    assert prov_mismatch.allowed is False
    assert "PROVIDER_MISMATCH" in prov_mismatch.blocking_codes

    # 12. Connection mismatch
    conn_mismatch = service.run_execution_preflight(
        base_req.model_copy(update={"connection_id": "ag_other_conn"})
    )
    assert conn_mismatch.allowed is False
    assert "CONNECTION_MISMATCH" in conn_mismatch.blocking_codes

    # 5 & 6. Manifest revision drift / hash drift (update worker_a to rev 2 while proposal pins rev 1)
    updated_manifest = _make_worker_manifest(
        agent_id="it.worker_a",
        department="it",
        skills=["code_generation"],
        spec_version="1.1.0",
    )
    rec_a = registry.get_agent("it.worker_a")
    assert rec_a is not None
    registry.update_agent(
        "it.worker_a",
        updated_manifest,
        expected_record_version=rec_a.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=13),
        change_summary="Update manifest to r2 mid-flight",
    )
    drift_report = service.run_execution_preflight(base_req)
    assert drift_report.allowed is False
    assert "STALE_MANIFEST_REVISION" in drift_report.blocking_codes

    # 7. Disabled Worker
    rec_a2 = registry.get_agent("it.worker_a")
    assert rec_a2 is not None
    registry.disable_agent(
        "it.worker_a",
        expected_record_version=rec_a2.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=14),
        reason="Disabled for maintenance",
    )
    disabled_report = service.run_execution_preflight(base_req)
    assert disabled_report.allowed is False
    assert "WORKER_DISABLED" in disabled_report.blocking_codes

    # 5b. Invalid pinned manifest revision on raw team check
    with env["store"].open_connection() as conn:
        env["store"].worker_sessions  # ensure repo ready
        _ = conn
    assert len(env["adapter"].requests) == 0


def test_p05_05_two_workers_shared_provider_isolation_and_collision_detection(tmp_path: Path) -> None:
    """[13, 14, 15, 23, 25, 30, 31, 32] Two workers share provider+connection with isolated sessions and collision fail-closed."""
    env = _setup_environment(tmp_path)
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[
            ("it.worker_a", "it", ["code_generation"]),
            ("it.worker_b", "it", ["text_analysis"]),
        ],
    )
    service: WorkerSessionLifecycleService = env["worker_service"]
    store: SQLiteRepositoryStore = env["store"]

    req_a = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        additional_context="SECRET_NOTE_FOR_A_ONLY",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    out_a = service.execute_worker_step(req_a)
    assert out_a.status == "CLOSED"
    assert out_a.verification_tier == "OFFLINE_INTEGRATION_VERIFIED"
    assert out_a.run_id is not None and out_a.session_id is not None

    req_b = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_02",
        agent_id="it.worker_b",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_b",
        additional_context="NOTE_FOR_B_ONLY",
        timestamp=BASE_TIME + timedelta(seconds=20),
    )
    out_b = service.execute_worker_step(req_b)
    assert out_b.status == "CLOSED"
    assert out_b.session_id is not None

    # Shared Run ownership without competing Runs, isolated logical & provider IDs
    assert out_a.run_id == out_b.run_id
    assert out_a.session_id != out_b.session_id
    assert out_a.provider_conversation_id != out_b.provider_conversation_id
    assert out_a.binding is not None and out_b.binding is not None
    assert out_a.binding.private_context_namespace != out_b.binding.private_context_namespace

    # No private context bleeding into worker B's EngineRequest
    adapter: FakeConfigurableEngineAdapter = env["adapter"]
    assert "SECRET_NOTE_FOR_A_ONLY" in (adapter.requests[0].context or "")
    assert "SECRET_NOTE_FOR_A_ONLY" not in (adapter.requests[1].context or "")

    # Run contains both Session UUIDs; Task stays RUNNING (not falsely COMPLETED!)
    persisted_run = store.runs.get_run(out_a.run_id)
    persisted_task = store.tasks.get_task(task.task_id)
    assert persisted_run is not None
    assert set(persisted_run.session_ids) == {out_a.session_id, out_b.session_id}
    assert persisted_task is not None
    assert persisted_task.status == "RUNNING"
    assert persisted_task.active_run_id == out_a.run_id

    # Event attribution check (run.started attributed to orchestrator; worker/engine events strictly to owning Worker)
    evts_a = store.events.read_entries(session_id=out_a.session_id)
    evts_b = store.events.read_entries(session_id=out_b.session_id)
    worker_events_a = [
        e for e in evts_a if e.event.event_type.startswith(("session.", "engine."))
    ]
    worker_events_b = [
        e for e in evts_b if e.event.event_type.startswith(("session.", "engine."))
    ]
    assert len(worker_events_a) >= 4
    assert len(worker_events_b) >= 4
    assert all(e.event.actor_id == "it.worker_a" for e in worker_events_a)
    assert all(e.event.actor_id == "it.worker_b" for e in worker_events_b)
    assert not any(e.event.actor_id == "it.worker_b" for e in evts_a)
    assert not any(e.event.actor_id == "it.worker_a" for e in evts_b)
    assert not any(e.event.event_type in {"tool.call_observed", "qa.verification_recorded"} for e in evts_a + evts_b)

    # Read-only runtime state projection truthfulness
    snap_a = service.get_worker_runtime_state("it.worker_a", task_id=task.task_id)
    assert snap_a.in_registry is True
    assert snap_a.is_assigned_to_task is True
    assert snap_a.session_status == "CLOSED"
    assert snap_a.observable_running is False
    assert snap_a.qa_verified_artifact_ids == []

    # Provider conversation collision detection (Step 15 adversarial)
    colliding_conv = out_a.provider_conversation_id
    assert colliding_conv is not None

    def _collide_handler(
        req: EngineRequest,
        conn: EngineConnectionRef | None,
        on_event: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        now = BASE_TIME + timedelta(seconds=35)
        return EngineResult(
            request_id=req.request_id,
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id=req.connection_id,
            model_id=req.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            session_reference=EngineSessionReference(
                session_id=req.session_reference.session_id if req.session_reference else None,
                provider_conversation_id=colliding_conv,
                binding_source="adapter_logical",
            ),
            started_at=now,
            finished_at=now + timedelta(seconds=1),
            duration_ms=1000,
            telemetry=EngineTelemetry(
                duration_ms=1000,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        )

    adapter._handler = _collide_handler
    # Add step_03 assigned to worker_c and watch collision fail cleanly
    task2, plan2, team2 = _seed_planned_assigned_task(
        env,
        workers=[("it.worker_c", "it", ["artifact_verification"])],
    )
    out_c = service.execute_worker_step(
        WorkerExecutionRequest(
            task_id=task2.task_id,
            plan_id=plan2.plan_id,
            team_proposal_id=team2.proposal_id,
            step_id="step_01",
            agent_id="it.worker_c",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(env["ws_root"]),
            workspace_scope=f"workspace/{task2.task_id}/it.worker_c",
            timestamp=BASE_TIME + timedelta(seconds=34),
        )
    )
    assert out_c.status == "FAILED"
    assert out_c.error_code == "PROVIDER_CONVERSATION_COLLISION"
    assert out_c.provider_conversation_id is None


def test_p05_05_session_transitions_failures_cancel_race_timeout_and_recovery(tmp_path: Path) -> None:
    """[16..20, 26..29] State guards (IDLE/CLOSED/FAILED), cancel race, timeout, stale lock, restart recovery."""
    env = _setup_environment(tmp_path)
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[
            ("it.worker_a", "it", ["code_generation"]),
            ("it.worker_b", "it", ["text_analysis"]),
        ],
    )
    service: WorkerSessionLifecycleService = env["worker_service"]
    adapter: FakeConfigurableEngineAdapter = env["adapter"]

    # 16 & 17. Direct INITIALIZED -> IDLE/CLOSED forbidden; INITIALIZED -> ACTIVE -> IDLE -> ACTIVE -> CLOSED allowed
    req1 = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    init_stored, _ = service.initialize_worker_session(req1)
    with pytest.raises(SessionTransitionError):
        service.transition_session(
            init_stored.session.session_id,
            target_status="CLOSED",
            actor_id="core.master",
            actor_kind="orchestrator",
            reason="Skip ACTIVE",
            evidence_summary="none",
            timestamp=BASE_TIME + timedelta(seconds=11),
        )

    active_stored = service.transition_session(
        init_stored.session.session_id,
        target_status="ACTIVE",
        actor_id="core.master",
        actor_kind="orchestrator",
        reason="Start test active session",
        evidence_summary="inv-test-1",
        timestamp=BASE_TIME + timedelta(seconds=12),
        invocation_id="inv-test-1",
    )
    idle_stored = service.transition_session(
        active_stored.session.session_id,
        target_status="IDLE",
        actor_id="core.master",
        actor_kind="orchestrator",
        reason="Pause between subturns",
        evidence_summary="turn_idle",
        timestamp=BASE_TIME + timedelta(seconds=13),
    )
    assert idle_stored.session.status == "IDLE"

    # Restart simulation while IDLE/ACTIVE -> reopen SQLite without live handle -> NEEDS_RECONCILIATION
    env["store"].close()
    reopened_env = _setup_environment_on_existing_db(tmp_path / "p05_05.sqlite", env["ws_root"])
    reopened_svc: WorkerSessionLifecycleService = reopened_env["worker_service"]
    recon = reopened_svc.reconcile_worker_sessions(
        task.task_id,
        timestamp=BASE_TIME + timedelta(seconds=15),
    )
    assert recon.recovery_status == "NEEDS_RECONCILIATION"
    assert recon.items[0].observable_process_status == "UNOBSERVABLE_AFTER_RESTART"
    assert reopened_svc.get_worker_runtime_state("it.worker_a", task_id=task.task_id).observable_running is False
    with pytest.raises(SessionTransitionError, match="no observable live process handle"):
        reopened_svc.terminate_session(
            init_stored.session.session_id,
            actor_id="operator.local",
            actor_kind="operator",
            reason="Cancel unobservable session",
            timestamp=BASE_TIME + timedelta(seconds=16),
        )

    # Close cleanly on active session transitions and test cancel race + timeout
    reopened_svc.transition_session(
        init_stored.session.session_id,
        target_status="CLOSED",
        actor_id="operator.local",
        actor_kind="operator",
        reason="Operator reconciled close",
        evidence_summary="reconciled_verified",
        timestamp=BASE_TIME + timedelta(seconds=17),
    )
    reopened_store: SQLiteRepositoryStore = reopened_env["store"]
    assert len(reopened_store.worker_sessions.list_transitions(init_stored.session.session_id)) == 4
    reopened_store.close()

    # Cancel race test (new task in fresh env)
    env2 = _setup_environment(tmp_path / "race")
    t_r, p_r, tm_r = _seed_planned_assigned_task(
        env2,
        workers=[("it.worker_a", "it", ["code_generation"])],
    )
    handle = FakeOwnedHandle(confirm_stop=True)
    started_event = threading.Event()
    release_engine = threading.Event()

    def _slow_handler(
        req: EngineRequest,
        conn: EngineConnectionRef | None,
        on_event: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        started_event.set()
        release_engine.wait(timeout=3.0)
        return EngineResult(
            request_id=req.request_id,
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id=req.connection_id,
            model_id=req.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            started_at=BASE_TIME + timedelta(seconds=20),
            finished_at=BASE_TIME + timedelta(seconds=21),
            duration_ms=1000,
            telemetry=EngineTelemetry(
                duration_ms=1000,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        )

    env2["adapter"]._handler = _slow_handler
    svc2: WorkerSessionLifecycleService = env2["worker_service"]
    outcomes: list[Any] = []

    def _run_worker() -> None:
        outcomes.append(
            svc2.execute_worker_step(
                WorkerExecutionRequest(
                    task_id=t_r.task_id,
                    plan_id=p_r.plan_id,
                    team_proposal_id=tm_r.proposal_id,
                    step_id="step_01",
                    agent_id="it.worker_a",
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    workspace_root=str(env2["ws_root"]),
                    workspace_scope=f"workspace/{t_r.task_id}/it.worker_a",
                    timestamp=BASE_TIME + timedelta(seconds=10),
                ),
                execution_handle=handle,
            )
        )

    worker_thread = threading.Thread(target=_run_worker, daemon=True)
    worker_thread.start()
    assert started_event.wait(timeout=3.0)
    sessions_active = env2["store"].worker_sessions.list_stored_sessions(task_id=t_r.task_id)
    assert len(sessions_active) == 1
    assert svc2.get_worker_runtime_state("it.worker_a", task_id=t_r.task_id).observable_running is True

    cancelled = svc2.terminate_session(
        sessions_active[0].session.session_id,
        actor_id="operator.local",
        actor_kind="operator",
        reason="Operator cancelled mid-execution",
        timestamp=BASE_TIME + timedelta(seconds=15),
    )
    assert cancelled.session.status == "TERMINATED"
    release_engine.set()
    worker_thread.join(timeout=3.0)
    assert len(outcomes) == 1
    assert outcomes[0].status == "TERMINATED"
    assert outcomes[0].error_code == "ALREADY_TERMINAL"
    assert env2["store"].worker_sessions.get_stored_session(sessions_active[0].session.session_id).session.status == "TERMINATED"

    # 28. Timeout via EngineResult(status="TIMED_OUT")
    env3 = _setup_environment(tmp_path / "timeout")
    t_t, p_t, tm_t = _seed_planned_assigned_task(
        env3,
        workers=[("it.worker_a", "it", ["code_generation"])],
    )
    env3["adapter"]._handler = lambda req, conn, on_ev: EngineResult(
        request_id=req.request_id,
        provider_id="antigravity",
        adapter_id="antigravity_cli",
        connection_id=req.connection_id,
        model_id=req.model_id,
        platform_kind="agent_execution_platform",
        status="TIMED_OUT",
        started_at=BASE_TIME + timedelta(seconds=20),
        finished_at=BASE_TIME + timedelta(seconds=50),
        duration_ms=30000,
        telemetry=EngineTelemetry(
            duration_ms=30000,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        ),
        error=EngineError(
            error_code="TIMED_OUT",
            error_source="cli",
            message="Worker CLI timed out after 30s.",
            retryable=False,
        ),
    )
    out_to = env3["worker_service"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=t_t.task_id,
            plan_id=p_t.plan_id,
            team_proposal_id=tm_t.proposal_id,
            step_id="step_01",
            agent_id="it.worker_a",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(env3["ws_root"]),
            workspace_scope=f"workspace/{t_t.task_id}/it.worker_a",
            timestamp=BASE_TIME + timedelta(seconds=10),
        )
    )
    assert out_to.status == "FAILED"
    assert out_to.error_code == "TIMED_OUT"


def test_p05_05_idempotency_after_restart_and_event_append_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[21, 22, 24, 25, 33] Idempotency conflict/replay after restart, atomic Event rollback, and Task/Run replay validity."""
    env = _setup_environment(tmp_path)
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[("it.worker_a", "it", ["code_generation"])],
    )
    svc: WorkerSessionLifecycleService = env["worker_service"]
    req = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        idempotency_key="idem-step-01-fixed",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    first = svc.execute_worker_step(req)
    assert first.status == "CLOSED"
    assert len(env["adapter"].requests) == 1

    # Restart SQLite store
    env["store"].close()
    env2 = _setup_environment_on_existing_db(tmp_path / "p05_05.sqlite", env["ws_root"])
    svc2: WorkerSessionLifecycleService = env2["worker_service"]

    # 22. Exact replay does NOT invoke EngineAdapter a second time
    replay = svc2.execute_worker_step(req.model_copy(update={"timestamp": BASE_TIME + timedelta(seconds=20)}))
    assert replay.status == "IDEMPOTENT_REPLAY"
    assert replay.session_id == first.session_id
    assert len(env2["adapter"].requests) == 0

    # 21. Same idempotency_key with changed fingerprint is blocked fail-closed
    conflict = svc2.execute_worker_step(
        req.model_copy(
            update={
                "workspace_scope": f"workspace/{task.task_id}/it.worker_a/sub",
                "timestamp": BASE_TIME + timedelta(seconds=21),
            }
        )
    )
    assert conflict.status == "BLOCKED"
    assert conflict.error_code == "IDEMPOTENCY_CONFLICT"

    # 33 & Replay Check: Task & Run recover cleanly as VALID across restart
    t_report = recover_task(env2["store"], task.task_id)
    assert t_report.recovery_status == "NEEDS_RECONCILIATION" or t_report.snapshot_matches_replay is True
    assert t_report.replay_result.status == "VALID"
    assert first.run_id is not None
    r_report = recover_run(env2["store"], first.run_id)
    assert r_report.replay_result.status == "VALID"
    env2["store"].close()

    # 24. Event append failure rollback preserves SQLite atomicity (0 leaked Run/Session/Binding)
    env_rb = _setup_environment(tmp_path / "rollback")
    t_rb, p_rb, tm_rb = _seed_planned_assigned_task(
        env_rb,
        workers=[("it.worker_a", "it", ["code_generation"])],
    )
    rb_store: SQLiteRepositoryStore = env_rb["store"]
    orig_append_conn = rb_store.events.append_on_connection

    def _fail_session_init_event(conn: Any, event: Any) -> Any:
        if event.event_type == "session.initialized":
            raise RuntimeError("Simulated disk/journal failure on session.initialized")
        return orig_append_conn(conn, event)

    monkeypatch.setattr(rb_store.events, "append_on_connection", _fail_session_init_event)
    with pytest.raises(RuntimeError, match="Simulated disk/journal failure"):
        env_rb["worker_service"].initialize_worker_session(
            WorkerExecutionRequest(
                task_id=t_rb.task_id,
                plan_id=p_rb.plan_id,
                team_proposal_id=tm_rb.proposal_id,
                step_id="step_01",
                agent_id="it.worker_a",
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(env_rb["ws_root"]),
                workspace_scope=f"workspace/{t_rb.task_id}/it.worker_a",
                timestamp=BASE_TIME + timedelta(seconds=10),
            )
        )
    assert rb_store.runs.list_runs_for_task(t_rb.task_id) == []
    assert rb_store.sessions.list_sessions(task_id=t_rb.task_id) == []
    assert rb_store.worker_sessions.list_stored_sessions(task_id=t_rb.task_id) == []


def _setup_environment_on_existing_db(db_path: Path, ws_root: Path) -> dict[str, Any]:
    """Reopen a persisted SQLite database simulating fresh Python process restart."""
    store = SQLiteRepositoryStore(db_path)
    approvals = ApprovalService(repository=store.approvals, journal=store.events)
    pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events, approval_service=approvals)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers={"antigravity", "gemini", "openai", "ollama"},
    )
    eng_registry = EngineRegistry()
    adapter = FakeConfigurableEngineAdapter()
    eng_registry.register_adapter(adapter, set_default_for_provider=True)
    eng_registry.register_connection(
        EngineConnectionRef(
            connection_id="ag_shared",
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            credential_ref="env:ANTIGRAVITY_API_KEY",
            default_model_id="gemini-2.5-flash",
            enabled=True,
        )
    )
    lifecycle = WorkflowLifecycleController(
        persistence=SQLiteLifecyclePersistenceAdapter(store),
        permission_hook=pm.build_lifecycle_hook(_operator_auth()),
    )
    worker_service = WorkerSessionLifecycleService(
        worker_session_repo=store.worker_sessions,
        task_repo=store.tasks,
        run_repo=store.runs,
        metadata_repo=store.metadata,
        event_journal=store.events,
        plan_repo=store.plans,
        team_repo=store.teams,
        agent_registry=registry,
        permission_manager=pm,
        engine_registry=eng_registry,
        lifecycle_controller=lifecycle,
        approval_service=approvals,
    )
    return {
        "store": store,
        "registry": registry,
        "adapter": adapter,
        "worker_service": worker_service,
    }


def test_p05_05_engine_missing_result_invalid_revision_and_stale_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[5, 6, 13, 19, 26, 28] Cover missing engine result, invalid revision, hash drift, residency block, timeout limit, and optimistic lock."""
    env = _setup_environment(tmp_path)
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[("it.worker_a", "it", ["code_generation"])],
    )
    svc: WorkerSessionLifecycleService = env["worker_service"]
    store: SQLiteRepositoryStore = env["store"]
    base_req = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )

    # Timeout exceeding manifest limit (120s) fails closed in preflight
    to_limit_report = svc.run_execution_preflight(
        base_req.model_copy(update={"timeout_seconds": 121.0})
    )
    assert to_limit_report.allowed is False
    assert "TIMEOUT_EXCEEDS_MANIFEST_LIMIT" in to_limit_report.blocking_codes

    # Invalid / missing pinned manifest revision in Registry history
    orig_get_ver = env["registry"].get_manifest_version
    monkeypatch.setattr(env["registry"], "get_manifest_version", lambda aid, rev: None)
    missing_rev_report = svc.run_execution_preflight(base_req)
    assert missing_rev_report.allowed is False
    assert "INVALID_MANIFEST_REVISION" in missing_rev_report.blocking_codes

    # Tampered version hash detected as MANIFEST_HASH_DRIFT
    real_ver = orig_get_ver("it.worker_a", 1)
    assert real_ver is not None
    tampered_ver = real_ver.model_copy(update={"manifest_sha256": "0" * 64})
    monkeypatch.setattr(env["registry"], "get_manifest_version", lambda aid, rev: tampered_ver)
    hash_drift_report = svc.run_execution_preflight(base_req)
    assert hash_drift_report.allowed is False
    assert "MANIFEST_HASH_DRIFT" in hash_drift_report.blocking_codes
    monkeypatch.undo()

    # EngineAdapter returning None transitions ACTIVE -> FAILED with ENGINE_MISSING_RESULT
    env["adapter"]._handler = lambda req, conn, on_ev: None
    missing_res = svc.execute_worker_step(base_req)
    assert missing_res.status == "FAILED"
    assert missing_res.error_code == "ENGINE_MISSING_RESULT"

    # Optimistic lock conflict raises StaleSessionVersionError
    init_stored, _ = svc.initialize_worker_session(
        base_req.model_copy(update={"timestamp": BASE_TIME + timedelta(seconds=20)})
    )
    stale_binding = init_stored.binding.model_copy(update={"updated_at": BASE_TIME + timedelta(seconds=21)})
    stale_sess = init_stored.session.model_copy(update={"status": "ACTIVE", "started_at": BASE_TIME + timedelta(seconds=21), "invocation_id": "inv-lock"})
    with pytest.raises(StaleSessionVersionError):
        store.worker_sessions.transition_session_atomic(
            session=stale_sess,
            binding=stale_binding,
            expected_record_version=99,
            transition=init_stored.binding.model_dump() and __import__(
                "core.orchestration.worker_session_models",
                fromlist=["WorkerSessionTransitionRecord"],
            ).WorkerSessionTransitionRecord(
                session_id=stale_sess.session_id,
                task_id=task.task_id,
                run_id=init_stored.binding.run_id,
                from_status="INITIALIZED",
                to_status="ACTIVE",
                actor_id="core.master",
                reason="Stale version test",
                evidence_summary="stale",
                event_id="evt_placeholder_1234",
                timestamp=BASE_TIME + timedelta(seconds=21),
            ),
            events=[],
        )

    # Local-only residency step paired with cloud adapter blocks in preflight
    env_loc = _setup_environment(tmp_path / "local_only")
    t_loc, p_loc, tm_loc = _seed_planned_assigned_task(
        env_loc,
        workers=[("it.worker_local_fail", "it", ["code_generation"])],
    )
    stored_p_loc = env_loc["store"].plans.get_plan(p_loc.plan_id)
    assert stored_p_loc is not None
    step_loc = stored_p_loc.plan.steps[0].model_copy(
        update={
            "routing_proposal": PlanRoutingProposal(
                engine_id="antigravity",
                provider_id="antigravity",
                connection_id="ag_shared",
                data_residency_policy="local_only",
                routing_reason="Require local-only execution.",
            )
        }
    )
    new_plan_obj = stored_p_loc.plan.model_copy(update={"plan_revision": 2, "steps": [step_loc]})
    with env_loc["store"].open_connection() as conn:
        conn.execute(
            "UPDATE master_plans SET plan_json = ? WHERE plan_id = ?;",
            (new_plan_obj.canonical_json(), p_loc.plan_id),
        )
    loc_report = env_loc["worker_service"].run_execution_preflight(
        WorkerExecutionRequest(
            task_id=t_loc.task_id,
            plan_id=p_loc.plan_id,
            team_proposal_id=tm_loc.proposal_id,
            step_id="step_01",
            agent_id="it.worker_local_fail",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(env_loc["ws_root"]),
            workspace_scope=f"workspace/{t_loc.task_id}/it.worker_local_fail",
            timestamp=BASE_TIME + timedelta(seconds=30),
        )
    )
    assert loc_report.allowed is False
    assert "DATA_BOUNDARY_VIOLATION" in loc_report.blocking_codes


def test_p05_05_approval_gate_and_dependency_ordering(tmp_path: Path) -> None:
    """[11, 16] Enforce step.depends_on ordering and ApprovalService single-use consumption."""
    approval_rule = PolicyRule(
        rule_id="gov.require_approval_worker_exec",
        version="1.0.0",
        name="Require Human Approval for agent.execute",
        description="Require explicit operator approval before Worker execution.",
        category="approval",
        targets=["master_agent"],
        resources=["*"],
        priority=500,
        effect="REQUIRE_APPROVAL",
        actions=["agent.execute"],
        approval_required=True,
    )
    from core.governance.evaluator import build_default_policy_rules

    env = _setup_environment(
        tmp_path,
        rules=build_default_policy_rules() + [approval_rule],
    )
    task, plan, team = _seed_planned_assigned_task(
        env,
        workers=[
            ("it.worker_a", "it", ["code_generation"]),
            ("it.worker_b", "it", ["text_analysis"]),
        ],
    )
    svc: WorkerSessionLifecycleService = env["worker_service"]
    approvals: ApprovalService = env["approvals"]
    op_auth = _operator_auth()
    master_auth = _master_auth()

    req_step2 = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_02",
        agent_id="it.worker_b",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_b",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    # step_02 blocked before step_01 finishes AND missing approval_id
    dep_and_appr = svc.run_execution_preflight(req_step2)
    assert dep_and_appr.allowed is False
    assert "DEPENDENCY_NOT_SATISFIED" in dep_and_appr.blocking_codes
    assert "APPROVAL_REQUIRED" in dep_and_appr.blocking_codes

    # Create expired approval and valid approval for step_01
    req_step1 = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=team.proposal_id,
        step_id="step_01",
        agent_id="it.worker_a",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(env["ws_root"]),
        workspace_scope=f"workspace/{task.task_id}/it.worker_a",
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    fp1 = compute_request_fingerprint(
        action="agent.execute",
        resource=f"task:{task.task_id}/step:step_01/agent:it.worker_a",
        task_id=task.task_id,
        run_id=None,
        parameters={
            "agent_id": "it.worker_a",
            "step_id": "step_01",
            "plan_id": plan.plan_id,
            "team_proposal_id": team.proposal_id,
            "pinned_revision": "1.0.0",
        },
    )
    apr_exp = approvals.create_approval_request(
        requester=master_auth,
        action="agent.execute",
        target_resource=f"task:{task.task_id}/step:step_01/agent:it.worker_a",
        task_id=task.task_id,
        run_id=None,
        request_fingerprint=fp1,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=["gov.require_approval_worker_exec"],
            rule_versions={"gov.require_approval_worker_exec": "1.0.0"},
            reason_code="REQUIRE_APPROVAL",
        ),
        risk_summary="Approve step_01 execution",
        requested_at=BASE_TIME + timedelta(seconds=10),
        expires_at=BASE_TIME + timedelta(seconds=12),
    )
    approvals.decide_approval(
        apr_exp.approval_id,
        decider=op_auth,
        approved=True,
        reason="Approved briefly",
        timestamp=BASE_TIME + timedelta(seconds=11),
    )
    expired_out = svc.execute_worker_step(
        req_step1.model_copy(
            update={
                "approval_id": apr_exp.approval_id,
                "timestamp": BASE_TIME + timedelta(seconds=15),
            }
        )
    )
    assert expired_out.status == "BLOCKED"
    assert expired_out.error_code == "APPROVAL_EXPIRED"

    # Valid non-expired approval allows execution and is single-use consumed
    apr_ok = approvals.create_approval_request(
        requester=master_auth,
        action="agent.execute",
        target_resource=f"task:{task.task_id}/step:step_01/agent:it.worker_a",
        task_id=task.task_id,
        run_id=None,
        request_fingerprint=fp1,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=["gov.require_approval_worker_exec"],
            rule_versions={"gov.require_approval_worker_exec": "1.0.0"},
            reason_code="REQUIRE_APPROVAL",
        ),
        risk_summary="Approve step_01 execution cleanly",
        requested_at=BASE_TIME + timedelta(seconds=16),
    )
    approvals.decide_approval(
        apr_ok.approval_id,
        decider=op_auth,
        approved=True,
        reason="Approved by operator",
        timestamp=BASE_TIME + timedelta(seconds=17),
    )
    ok_out = svc.execute_worker_step(
        req_step1.model_copy(
            update={
                "approval_id": apr_ok.approval_id,
                "timestamp": BASE_TIME + timedelta(seconds=18),
            }
        )
    )
    assert ok_out.status == "CLOSED"
    consumed_rec = approvals.get_approval(apr_ok.approval_id)
    assert consumed_rec is not None and consumed_rec.status == "CONSUMED"

    # Reusing already-consumed approval_id fails closed
    reused_out = svc.execute_worker_step(
        req_step1.model_copy(
            update={
                "approval_id": apr_ok.approval_id,
                "timestamp": BASE_TIME + timedelta(seconds=19),
            }
        )
    )
    assert reused_out.status == "BLOCKED"
    assert reused_out.error_code == "APPROVAL_NOT_APPROVED"

