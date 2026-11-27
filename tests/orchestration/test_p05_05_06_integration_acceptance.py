"""Section VII End-to-End Integration Acceptance Suite for P05.05 + P05.06."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any, Callable
import uuid

import pytest

from core.agents.loader import load_agent_manifest_file
from core.agents.manifest import AgentManifest
from core.agents.registry import AgentRegistryService
from core.contracts import Artifact, Task, new_artifact_id, new_evidence_id, new_task_id
from core.governance.approvals import ApprovalService
from core.governance.evaluator import PolicyEvaluator
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext
from core.orchestration import (
    DynamicTeamBuilderService,
    HandoffAcknowledgeRequest,
    HandoffArtifactError,
    HandoffCreateRequest,
    HandoffDeliveryRequest,
    HandoffDependencyError,
    MasterPlanningService,
    PlanStepSpec,
    StructuredUserBrief,
    TeamBuildRequest,
    WorkerExecutionRequest,
    WorkerHandoffService,
    WorkerSessionLifecycleService,
)
from core.orchestration.planning_models import PlanRoutingProposal
from core.tasks.lifecycle import HandleTerminationOutcome, WorkflowLifecycleController
from engines.connections import EngineConnectionRef
from engines.contract import (
    EngineAdapter,
    EngineCapabilities,
    EngineEvent,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
    EngineTelemetry,
)
from engines.registry import EngineRegistry
from engines.verification import verify_engine_artifact
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteRepositoryStore, recover_run, recover_task
from storage.repositories.lifecycle_repo import SQLiteLifecyclePersistenceAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 11, 27, 11, 0, 0, tzinfo=timezone.utc)


def _master_auth() -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id="core.master",
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def _operator_auth() -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id="operator.local",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )


def _worker_manifest(
    *,
    agent_id: str,
    department: str,
    skills: list[str],
    fs_read: list[str] | None = None,
    fs_write: list[str] | None = None,
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "spec_version": "1.0.0",
            "id": agent_id,
            "name": f"Worker {agent_id}",
            "role": f"Specialist {agent_id}",
            "system_role": "worker",
            "department": department,
            "description": f"End-to-end acceptance worker {agent_id}.",
            "mission": {
                "purpose": f"Execute bounded {department} work safely.",
                "responsibilities": ["Deliver outputs matching task requirements."],
                "non_goals": ["Never bypass Governance or fabricate QA PASS."],
            },
            "capabilities": {
                "skills": skills,
                "inputs": ["task_brief", "text", "structured_json"],
                "outputs": ["workspace_file_artifact", "structured_execution_report", "text"],
            },
            "engine": {
                "default_type": "antigravity",
                "provider_id": "antigravity",
                "adapter_id": "antigravity_cli",
                "connection_id": "ag_shared",
                "model_tier": "flash",
                "model_id": "gemini-2.5-flash",
                "routing_profile": "workspace_sandboxed",
                "fallback_policy": "operator_approved_only",
            },
            "memory": {"scopes": ["private", "project"]},
            "skills": {"allowed": []},
            "tools": {"allowed": []},
            "permissions": {
                "fs_read": fs_read or ["workspace/{task_id}/*"],
                "fs_write": fs_write or ["workspace/{task_id}/*"],
                "network": "none",
                "requires_approval": ["files.delete"],
            },
            "limits": {"timeout_seconds": 120, "max_retries": 1},
        }
    )


class AcceptanceFakeAdapter(EngineAdapter):
    """Controlled offline EngineAdapter capturing calls without touching network or CLI."""

    def __init__(self, ws_root: Path) -> None:
        self._ws_root = ws_root
        self.calls: list[EngineRequest] = []
        self.custom_handler: Callable[
            [EngineRequest, EngineConnectionRef | None, Callable[[EngineEvent], None] | None],
            EngineResult | None,
        ] | None = None

    @property
    def provider_id(self) -> str:
        return "antigravity"

    @property
    def adapter_id(self) -> str:
        return "antigravity_cli"

    @property
    def platform_kind(self) -> str:
        return "agent_execution_platform"

    def get_capabilities(self, connection: EngineConnectionRef | None = None) -> EngineCapabilities:
        return EngineCapabilities(
            provider_id="antigravity",
            adapter_id="antigravity_cli",
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
            data_residency="cli_managed",
        )

    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        self.calls.append(request)
        if self.custom_handler is not None:
            return self.custom_handler(request, connection, on_event)  # type: ignore[return-value]

        now = BASE_TIME + timedelta(seconds=20)
        if on_event is not None:
            on_event(
                EngineEvent(
                    event_id=f"eng_{uuid.uuid4().hex[:10]}",
                    sequence_number=1,
                    timestamp=now,
                    event_type="execution_started",
                    source="provider",
                    provider_id="antigravity",
                    connection_id=request.connection_id,
                    status="STARTED",
                )
            )
        agent_id = str(request.metadata.get("agent_id", "worker"))
        step_id = str(request.metadata.get("step_id", "step"))
        task_id = str(request.metadata.get("task_id", "task"))
        out_path = self._ws_root / "workspace" / task_id / f"{agent_id}_{step_id}.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"acceptance output for {agent_id} {step_id}\n", encoding="utf-8")

        return EngineResult(
            request_id=request.request_id,
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id=request.connection_id,
            model_id=request.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            business_verification_status="UNVERIFIED",
            response_text=f"Finished {step_id} offline fixture",
            session_reference=EngineSessionReference(
                session_id=request.session_reference.session_id if request.session_reference else None,
                provider_conversation_id=f"prov_{agent_id}_{uuid.uuid4().hex[:8]}",
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


def _open_stack(db_path: Path, ws_root: Path, adapter: AcceptanceFakeAdapter | None = None) -> dict[str, Any]:
    store = SQLiteRepositoryStore(db_path)
    approvals = ApprovalService(repository=store.approvals, journal=store.events)
    pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events, approval_service=approvals)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers={"antigravity", "ollama"},
    )
    eng_reg = EngineRegistry()
    active_adapter = adapter or AcceptanceFakeAdapter(ws_root)
    eng_reg.register_adapter(active_adapter, set_default_for_provider=True)
    eng_reg.register_connection(
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
    planner = MasterPlanningService(
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        workspace_root=ws_root,
        allowed_providers={"antigravity", "ollama"},
        allowed_models={"gemini-2.5-flash"},
    )
    team_builder = DynamicTeamBuilderService(
        team_repo=store.teams,
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        allowed_providers={"antigravity", "ollama"},
        allowed_models={"gemini-2.5-flash"},
    )
    session_svc = WorkerSessionLifecycleService(
        worker_session_repo=store.worker_sessions,
        task_repo=store.tasks,
        run_repo=store.runs,
        metadata_repo=store.metadata,
        event_journal=store.events,
        plan_repo=store.plans,
        team_repo=store.teams,
        agent_registry=registry,
        permission_manager=pm,
        engine_registry=eng_reg,
        lifecycle_controller=lifecycle,
        approval_service=approvals,
        handoff_repo=store.handoffs,
    )
    handoff_svc = WorkerHandoffService(
        handoff_repo=store.handoffs,
        worker_session_repo=store.worker_sessions,
        task_repo=store.tasks,
        run_repo=store.runs,
        metadata_repo=store.metadata,
        plan_repo=store.plans,
        team_repo=store.teams,
        agent_registry=registry,
        permission_manager=pm,
        approval_service=approvals,
    )
    return {
        "store": store,
        "registry": registry,
        "adapter": active_adapter,
        "planner": planner,
        "team_builder": team_builder,
        "session_svc": session_svc,
        "handoff_svc": handoff_svc,
    }


def _bootstrap_db(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "acceptance.sqlite"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)
    ws_root = tmp_path / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    return db_path, ws_root


def test_scenario_a_single_worker_executes_step_with_sqlite_restart(tmp_path: Path) -> None:
    """Scenario A: Single production Worker executes 1 step via fake EngineAdapter & survives restart."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    stack = _open_stack(db_path, ws_root)
    store: SQLiteRepositoryStore = stack["store"]
    registry: AgentRegistryService = stack["registry"]
    op_auth = _operator_auth()
    agents_root = REPO_ROOT / "agents"
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    # Use a production-like worker registered cleanly via _worker_manifest or it_artifact_worker
    it_manifest = load_agent_manifest_file(
        agents_root / "roles" / "it_artifact_worker" / "agent.yaml",
        allowed_root=agents_root,
    )
    # Match connection_id "ag_shared" in offline acceptance stack
    eng_cfg = it_manifest.engine.model_copy(
        update={
            "provider_id": "antigravity",
            "adapter_id": "antigravity_cli",
            "connection_id": "ag_shared",
            "model_id": "gemini-2.5-flash",
        }
    )
    it_manifest = it_manifest.model_copy(
        update={
            "engine": eng_cfg,
            "permissions": it_manifest.permissions.model_copy(
                update={"fs_write": ["workspace/{task_id}/*"], "fs_read": ["workspace/{task_id}/*"]}
            ),
        }
    )
    registry.register_agent(
        it_manifest,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
        validated_capabilities=list(it_manifest.capabilities.skills),
    )

    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Scenario A deliverable",
            description="Single worker offline execution acceptance.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=2),
        )
    ).task
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/out.txt"],
        acceptance_criteria=["Output created in task workspace"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    step = PlanStepSpec(
        step_id="step_01",
        order=0,
        title="Produce single deliverable",
        description="Execute step_01 via offline fake EngineAdapter.",
        required_capabilities=[it_manifest.capabilities.skills[0]],
        expected_outputs=[f"workspace/{task.task_id}/out.txt"],
        allowed_read_paths=[f"workspace/{task.task_id}"],
        allowed_write_paths=[f"workspace/{task.task_id}"],
        acceptance_criteria=["Output present on disk"],
    )
    plan_res = stack["planner"].create_plan(
        brief=brief,
        steps=[step],
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=3),
    )
    plan, _ = stack["planner"].apply_plan_to_task(
        plan_res.stored_plan.plan_id,
        expected_plan_record_version=plan_res.stored_plan.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=4),
    )
    prop = stack["team_builder"].build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=5),
    )
    assigned = stack["team_builder"].apply_team_assignment(
        prop.proposal_id,
        expected_proposal_record_version=prop.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=6),
    )

    outcome = stack["session_svc"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_01",
            agent_id=it_manifest.id,
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            timestamp=BASE_TIME + timedelta(seconds=7),
        )
    )
    assert outcome.status == "CLOSED"
    assert outcome.verification_tier == "OFFLINE_INTEGRATION_VERIFIED"
    store.close()

    # Reopen SQLite and verify zero data loss & consistent replay
    reopened_stack = _open_stack(db_path, ws_root)
    reopened: SQLiteRepositoryStore = reopened_stack["store"]
    try:
        sess = reopened.worker_sessions.get_stored_session(outcome.session_id)
        assert sess is not None
        assert sess.session.status == "CLOSED"
        assert sess.binding.pinned_manifest_revision == 1
        t_rec = recover_task(reopened, task.task_id)
        assert t_rec.replay_result.status == "VALID"
        r_rec = recover_run(reopened, outcome.run_id)
        assert r_rec.replay_result.status == "VALID"
        assert reopened.tasks.get_task(task.task_id).status == "RUNNING"
    finally:
        reopened.close()


def test_scenario_b_two_workers_shared_provider_isolated_sessions_restart(tmp_path: Path) -> None:
    """Scenario B: Two Workers on same Task share Provider/Connection with isolated logical & provider contexts."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    stack = _open_stack(db_path, ws_root)
    registry: AgentRegistryService = stack["registry"]
    op_auth = _operator_auth()
    agents_root = REPO_ROOT / "agents"
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    registry.register_agent(
        _worker_manifest(agent_id="it.worker_alpha", department="it", skills=["code_generation"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
        validated_capabilities=["code_generation"],
    )
    registry.register_agent(
        _worker_manifest(agent_id="service.worker_beta", department="service", skills=["report_writing"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
        validated_capabilities=["report_writing"],
    )

    store: SQLiteRepositoryStore = stack["store"]
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Scenario B shared provider isolation",
            description="Two workers execute independent steps using shared provider.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=3),
        )
    ).task
    steps = [
        PlanStepSpec(
            step_id="step_01",
            order=0,
            title="Alpha step",
            description="Alpha executes bounded coding work.",
            required_capabilities=["code_generation"],
            expected_outputs=[f"workspace/{task.task_id}/alpha.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Alpha output created"],
        ),
        PlanStepSpec(
            step_id="step_02",
            order=1,
            title="Beta step",
            description="Beta executes separate service reporting work.",
            required_capabilities=["report_writing"],
            expected_outputs=[f"workspace/{task.task_id}/beta.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Beta output created"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/alpha.txt", f"workspace/{task.task_id}/beta.txt"],
        acceptance_criteria=["Both files generated"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    plan_res = stack["planner"].create_plan(brief=brief, steps=steps, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=4))
    plan, _ = stack["planner"].apply_plan_to_task(plan_res.stored_plan.plan_id, expected_plan_record_version=plan_res.stored_plan.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=5))
    prop = stack["team_builder"].build_team_proposal(TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id), auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=6))
    assigned = stack["team_builder"].apply_team_assignment(prop.proposal_id, expected_proposal_record_version=prop.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=7))

    out1 = stack["session_svc"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_01",
            agent_id="it.worker_alpha",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            additional_context="PRIVATE_ALPHA_SECRET_CONTEXT",
            timestamp=BASE_TIME + timedelta(seconds=8),
        )
    )
    out2 = stack["session_svc"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_02",
            agent_id="service.worker_beta",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            additional_context="PRIVATE_BETA_CONTEXT",
            timestamp=BASE_TIME + timedelta(seconds=15),
        )
    )
    assert out1.run_id == out2.run_id
    assert out1.session_id != out2.session_id
    assert out1.provider_conversation_id != out2.provider_conversation_id
    assert "PRIVATE_ALPHA_SECRET_CONTEXT" not in (stack["adapter"].calls[1].context or "")
    store.close()

    reopened = SQLiteRepositoryStore(db_path)
    try:
        sessions = reopened.worker_sessions.list_stored_sessions(task_id=task.task_id, run_id=out1.run_id)
        assert len(sessions) == 2
        assert {s.binding.agent_id: s.session.session_id for s in sessions} == {
            "it.worker_alpha": out1.session_id,
            "service.worker_beta": out2.session_id,
        }
    finally:
        reopened.close()


def test_scenario_c_worker_a_to_verified_handoff_to_worker_b_continuation(tmp_path: Path) -> None:
    """Scenario C: Worker A -> artifact -> independent QA verifier -> Handoff -> Worker B -> SQLite restart without fake COMPLETED."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    stack = _open_stack(db_path, ws_root)
    registry: AgentRegistryService = stack["registry"]
    op_auth = _operator_auth()
    agents_root = REPO_ROOT / "agents"
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    registry.register_agent(
        _worker_manifest(agent_id="it.producer_a", department="it", skills=["code_generation"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
        validated_capabilities=["code_generation"],
    )
    registry.register_agent(
        _worker_manifest(agent_id="qa.reviewer_b", department="qa", skills=["artifact_qa_inspection"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
        validated_capabilities=["artifact_qa_inspection"],
    )

    store: SQLiteRepositoryStore = stack["store"]
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Scenario C end-to-end handoff",
            description="Producer creates artifact, QA verifies, Reviewer continues via handoff.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=3),
        )
    ).task
    steps = [
        PlanStepSpec(
            step_id="step_01",
            order=0,
            title="Produce artifact",
            description="Author producer output file.",
            required_capabilities=["code_generation"],
            expected_outputs=[f"workspace/{task.task_id}/it.producer_a_step_01.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Producer output verified on disk"],
        ),
        PlanStepSpec(
            step_id="step_02",
            order=1,
            title="Continue downstream review",
            description="Consume verified handoff and author downstream summary.",
            depends_on=["step_01"],
            required_capabilities=["artifact_qa_inspection"],
            expected_outputs=[f"workspace/{task.task_id}/qa.reviewer_b_step_02.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Downstream review produced"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/qa.reviewer_b_step_02.txt"],
        acceptance_criteria=["Downstream step output generated"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    plan_res = stack["planner"].create_plan(brief=brief, steps=steps, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=4))
    plan, _ = stack["planner"].apply_plan_to_task(plan_res.stored_plan.plan_id, expected_plan_record_version=plan_res.stored_plan.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=5))
    prop = stack["team_builder"].build_team_proposal(TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id), auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=6))
    assigned = stack["team_builder"].apply_team_assignment(prop.proposal_id, expected_proposal_record_version=prop.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=7))

    # 3-4. Worker A executes step_01 via offline EngineAdapter producing artifact file
    out_a = stack["session_svc"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_01",
            agent_id="it.producer_a",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            timestamp=BASE_TIME + timedelta(seconds=8),
        )
    )
    assert out_a.status == "CLOSED"

    # 5. Independent verifier validates output file on disk
    art_rel = f"workspace/{task.task_id}/it.producer_a_step_01.txt"
    qa = verify_engine_artifact(
        workspace_dir=ws_root,
        artifact_relpath=art_rel,
        engine_result=out_a.engine_result,
        required_substrings=["acceptance output for it.producer_a step_01"],
    )
    assert qa.status == "VERIFIED_PASS"
    artifact = store.metadata.save_artifact(
        Artifact(
            artifact_id=new_artifact_id(),
            task_id=task.task_id,
            run_id=out_a.run_id,
            owner_agent_id="it.producer_a",
            relative_path=art_rel,
            content_type="text/plain",
            size_bytes=qa.file_size_bytes,
            sha256=qa.actual_sha256,
            version="1.0.0",
            verification_status="VERIFIED_PASS",
            evidence_reference=new_evidence_id(),
            created_at=BASE_TIME + timedelta(seconds=24),
        )
    )

    # 6-10. Create READY Handoff -> Deliver -> Acknowledge by Worker B
    hoff = stack["handoff_svc"].create_and_validate_handoff(
        HandoffCreateRequest(
            task_id=task.task_id,
            run_id=out_a.run_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            source_step_id="step_01",
            target_step_id="step_02",
            from_agent_id="it.producer_a",
            to_agent_id="qa.reviewer_b",
            from_session_id=out_a.session_id,
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            target_workspace_scope=f"workspace/{task.task_id}",
            artifact_ids=[artifact.artifact_id],
            input_context_summary="Producer step_01 output independently verified.",
            expected_next_action="Continue downstream QA verification step.",
            timestamp=BASE_TIME + timedelta(seconds=25),
        )
    )
    delivered = stack["handoff_svc"].deliver_handoff(
        HandoffDeliveryRequest(
            handoff_id=hoff.handoff.handoff_id,
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            timestamp=BASE_TIME + timedelta(seconds=26),
        )
    )
    acked = stack["handoff_svc"].acknowledge_handoff(
        HandoffAcknowledgeRequest(
            handoff_id=delivered.handoff.handoff_id,
            actor_id="qa.reviewer_b",
            actor_kind="worker_agent",
            timestamp=BASE_TIME + timedelta(seconds=27),
        )
    )
    assert acked.handoff.status == "ACKNOWLEDGED"

    # 11. Worker B executes step_02 strictly consuming acknowledged handoff context package
    out_b = stack["session_svc"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_02",
            agent_id="qa.reviewer_b",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            handoff_ids=[acked.handoff.handoff_id],
            require_acknowledged_handoffs=True,
            timestamp=BASE_TIME + timedelta(seconds=28),
        )
    )
    assert out_b.status == "CLOSED"
    assert "[UNTRUSTED_UPSTREAM_DATA" in (stack["adapter"].calls[-1].context or "")
    store.close()

    # 12-14. Reopen SQLite and verify full provenance & Task is NOT falsely COMPLETED
    reopened = SQLiteRepositoryStore(db_path)
    try:
        final_task = reopened.tasks.get_task(task.task_id)
        assert final_task is not None
        assert final_task.status == "RUNNING"
        assert final_task.status != "COMPLETED"
        reloaded_hoff = reopened.handoffs.get_handoff(acked.handoff.handoff_id)
        assert reloaded_hoff is not None and reloaded_hoff.handoff.status == "ACKNOWLEDGED"
        assert len(reopened.worker_sessions.list_stored_sessions(task_id=task.task_id)) == 2
        assert recover_task(reopened, task.task_id).replay_result.status == "VALID"
    finally:
        reopened.close()


def test_scenario_d_worker_disabled_mid_workflow_distinguishes_live_vs_unstarted(tmp_path: Path) -> None:
    """Scenario D: Mid-workflow disable/manifest update blocks new step without auto-killing or upgrading live session."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    live_adapter = AcceptanceFakeAdapter(ws_root)
    stack = _open_stack(db_path, ws_root, adapter=live_adapter)
    registry: AgentRegistryService = stack["registry"]
    op_auth = _operator_auth()
    agents_root = REPO_ROOT / "agents"
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    w_a = _worker_manifest(agent_id="it.live_a", department="it", skills=["code_generation"])
    w_b = _worker_manifest(agent_id="qa.next_b", department="qa", skills=["artifact_qa_inspection"])
    registry.register_agent(w_a, auth_context=op_auth, timestamp=BASE_TIME + timedelta(seconds=1), validated_capabilities=["code_generation"])
    registry.register_agent(w_b, auth_context=op_auth, timestamp=BASE_TIME + timedelta(seconds=2), validated_capabilities=["artifact_qa_inspection"])

    store: SQLiteRepositoryStore = stack["store"]
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Scenario D mid-flight disable",
            description="Verify drift detection preserves running session and blocks unstarted step.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=3),
        )
    ).task
    steps = [
        PlanStepSpec(
            step_id="step_01",
            order=0,
            title="Active worker A step",
            description="Execute step 1 while active.",
            required_capabilities=["code_generation"],
            expected_outputs=[f"workspace/{task.task_id}/a.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Step 1 output"],
        ),
        PlanStepSpec(
            step_id="step_02",
            order=1,
            title="Unstarted worker B step",
            description="Step 2 waiting for start.",
            required_capabilities=["artifact_qa_inspection"],
            expected_outputs=[f"workspace/{task.task_id}/b.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Step 2 output"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/b.txt"],
        acceptance_criteria=["Outputs created"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    plan_res = stack["planner"].create_plan(brief=brief, steps=steps, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=4))
    plan, _ = stack["planner"].apply_plan_to_task(plan_res.stored_plan.plan_id, expected_plan_record_version=plan_res.stored_plan.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=5))
    prop = stack["team_builder"].build_team_proposal(TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id), auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=6))
    assigned = stack["team_builder"].apply_team_assignment(prop.proposal_id, expected_proposal_record_version=prop.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=7))

    # Pre-initialize Worker B session (not yet started) AND run Worker A live in a thread
    init_b, _ = stack["session_svc"].initialize_worker_session(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_02",
            agent_id="qa.next_b",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            timestamp=BASE_TIME + timedelta(seconds=8),
        )
    )
    entered = threading.Event()
    proceed = threading.Event()

    class _Handle:
        execution_id = "exec_live_a"
        owner_token = "tok_live_a"
        pid = 9991
        def is_running(self) -> bool:
            return not proceed.is_set()
        def request_cancel(self, **kw: Any) -> HandleTerminationOutcome:
            return HandleTerminationOutcome(execution_id=self.execution_id, confirmed_stopped=False, terminal_status="STILL_RUNNING")

    def _blocking_exec(req: EngineRequest, conn: Any, on_ev: Any) -> EngineResult:
        entered.set()
        proceed.wait(timeout=3.0)
        return EngineResult(
            request_id=req.request_id,
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id=req.connection_id,
            model_id=req.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            started_at=BASE_TIME + timedelta(seconds=12),
            finished_at=BASE_TIME + timedelta(seconds=13),
            duration_ms=1000,
            telemetry=EngineTelemetry(duration_ms=1000, telemetry_status="UNAVAILABLE_FROM_PROVIDER", telemetry_source="unavailable"),
        )

    live_adapter.custom_handler = _blocking_exec
    t = threading.Thread(
        target=lambda: stack["session_svc"].execute_worker_step(
            WorkerExecutionRequest(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                team_proposal_id=assigned.stored_proposal.proposal_id,
                step_id="step_01",
                agent_id="it.live_a",
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ws_root),
                workspace_scope=f"workspace/{task.task_id}",
                timestamp=BASE_TIME + timedelta(seconds=9),
            ),
            execution_handle=_Handle(),
        ),
        daemon=True,
    )
    t.start()
    assert entered.wait(timeout=3.0)

    # Mid-workflow mutations: update Worker A to rev 2 AND disable Worker B
    rec_a = registry.get_agent("it.live_a")
    assert rec_a is not None
    registry.update_agent(
        "it.live_a",
        w_a.model_copy(update={"description": "Updated to r2 while active"}),
        expected_record_version=rec_a.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=10),
        change_summary="Update active worker description to r2",
    )
    rec_b = registry.get_agent("qa.next_b")
    assert rec_b is not None
    registry.disable_agent(
        "qa.next_b",
        expected_record_version=rec_b.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=10),
        reason="Disable pending worker mid-workflow",
    )

    # Reconcile mid-workflow: distinguishes LIVE_OBSERVABLE vs INITIALIZED_NOT_STARTED + prevents new step start!
    recon = stack["session_svc"].reconcile_worker_sessions(
        task.task_id,
        timestamp=BASE_TIME + timedelta(seconds=11),
    )
    item_by_agent = {i.agent_id: i for i in recon.items}
    assert item_by_agent["it.live_a"].observable_process_status == "LIVE_OBSERVABLE"
    assert item_by_agent["it.live_a"].manifest_drift_detected is True
    assert item_by_agent["qa.next_b"].observable_process_status == "INITIALIZED_NOT_STARTED"
    assert item_by_agent["qa.next_b"].worker_disabled is True
    assert item_by_agent["qa.next_b"].reconciliation_required is True

    # Launching disabled Worker B is blocked fail-closed; active session stays pinned to r1 & finishes normally
    blocked_b = stack["session_svc"].execute_worker_step(
        WorkerExecutionRequest(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            team_proposal_id=assigned.stored_proposal.proposal_id,
            step_id="step_02",
            agent_id="qa.next_b",
            actor_id="core.master",
            actor_kind="orchestrator",
            workspace_root=str(ws_root),
            workspace_scope=f"workspace/{task.task_id}",
            timestamp=BASE_TIME + timedelta(seconds=11),
        )
    )
    assert blocked_b.status == "BLOCKED"
    assert blocked_b.error_code == "WORKER_DISABLED"

    proceed.set()
    t.join(timeout=3.0)
    sessions_a = store.worker_sessions.list_stored_sessions(task_id=task.task_id, agent_id="it.live_a")
    assert len(sessions_a) == 1
    assert sessions_a[0].session.status == "CLOSED"
    assert sessions_a[0].binding.pinned_manifest_revision == 1
    assert init_b.session.session_id is not None


def test_scenario_e_crash_and_failure_injection_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario E: Verify structured fail-closed handling for all 8 crash & failure injection cases."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    stack = _open_stack(db_path, ws_root)
    registry: AgentRegistryService = stack["registry"]
    op_auth = _operator_auth()
    agents_root = REPO_ROOT / "agents"
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    registry.register_agent(
        _worker_manifest(agent_id="it.e_worker", department="it", skills=["code_generation"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
        validated_capabilities=["code_generation"],
    )
    registry.register_agent(
        _worker_manifest(agent_id="qa.e_worker", department="qa", skills=["artifact_qa_inspection"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
        validated_capabilities=["artifact_qa_inspection"],
    )

    store: SQLiteRepositoryStore = stack["store"]
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Scenario E failure matrix",
            description="Exercise crash & security faults.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=3),
        )
    ).task
    steps = [
        PlanStepSpec(
            step_id="step_01",
            order=0,
            title="Fault step 1",
            description="Step 1 fault checks.",
            required_capabilities=["code_generation"],
            expected_outputs=[f"workspace/{task.task_id}/out.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Verified"],
        ),
        PlanStepSpec(
            step_id="step_02",
            order=1,
            title="Fault step 2",
            description="Step 2 fault checks.",
            depends_on=["step_01"],
            required_capabilities=["artifact_qa_inspection"],
            expected_outputs=[f"workspace/{task.task_id}/qa.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["QA Checked"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/qa.txt"],
        acceptance_criteria=["Done"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    plan_res = stack["planner"].create_plan(brief=brief, steps=steps, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=4))
    plan, _ = stack["planner"].apply_plan_to_task(plan_res.stored_plan.plan_id, expected_plan_record_version=plan_res.stored_plan.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=5))
    prop = stack["team_builder"].build_team_proposal(TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id), auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=6))
    assigned = stack["team_builder"].apply_team_assignment(prop.proposal_id, expected_proposal_record_version=prop.record_version, auth_context=_master_auth(), timestamp=BASE_TIME + timedelta(seconds=7))

    req1 = WorkerExecutionRequest(
        task_id=task.task_id,
        plan_id=plan.plan_id,
        team_proposal_id=assigned.stored_proposal.proposal_id,
        step_id="step_01",
        agent_id="it.e_worker",
        actor_id="core.master",
        actor_kind="orchestrator",
        workspace_root=str(ws_root),
        workspace_scope=f"workspace/{task.task_id}",
        idempotency_key="e-idem-1",
        timestamp=BASE_TIME + timedelta(seconds=8),
    )

    # 1. Event append failure rolls back SQLite cleanly
    orig_append_conn = store.events.append_on_connection
    monkeypatch.setattr(
        store.events,
        "append_on_connection",
        lambda conn, ev: (_ for _ in ()).throw(RuntimeError("boom_event")) if ev.event_type == "session.initialized" else orig_append_conn(conn, ev),
    )
    with pytest.raises(RuntimeError, match="boom_event"):
        stack["session_svc"].initialize_worker_session(req1)
    assert store.runs.list_runs_for_task(task.task_id) == []
    monkeypatch.undo()

    # 2. SQLite transaction failure mid-write rolls back cleanly
    orig_save_session = store.sessions.save_session
    monkeypatch.setattr(store.sessions, "save_session", lambda s: (_ for _ in ()).throw(RuntimeError("boom_sqlite")))
    with pytest.raises(RuntimeError, match="boom_sqlite"):
        stack["session_svc"].initialize_worker_session(req1)
    assert store.runs.list_runs_for_task(task.task_id) == []
    monkeypatch.setattr(store.sessions, "save_session", orig_save_session)

    # 3. Engine returning None produces structured FAILED without fake CLOSED
    stack["adapter"].custom_handler = lambda req, conn, on_ev: None
    miss_out = stack["session_svc"].execute_worker_step(req1.model_copy(update={"idempotency_key": "e-miss"}))
    assert miss_out.status == "FAILED"
    assert miss_out.error_code == "ENGINE_MISSING_RESULT"

    # 4 & 5 & 8. Successful execution with idempotency, collision check, and unobservable session recovery
    stack["adapter"].custom_handler = None
    ok_out = stack["session_svc"].execute_worker_step(req1)
    assert ok_out.status == "CLOSED"

    # 5. Provider conversation collision
    stack["adapter"].custom_handler = lambda req, conn, on_ev: EngineResult(
        request_id=req.request_id,
        provider_id="antigravity",
        adapter_id="antigravity_cli",
        connection_id=req.connection_id,
        model_id=req.model_id,
        platform_kind="agent_execution_platform",
        status="SUCCEEDED",
        session_reference=EngineSessionReference(
            session_id=req.session_reference.session_id if req.session_reference else None,
            provider_conversation_id=ok_out.provider_conversation_id,
            binding_source="adapter_logical",
        ),
        started_at=BASE_TIME + timedelta(seconds=15),
        finished_at=BASE_TIME + timedelta(seconds=16),
        duration_ms=1000,
        telemetry=EngineTelemetry(duration_ms=1000, telemetry_status="UNAVAILABLE_FROM_PROVIDER", telemetry_source="unavailable"),
    )
    col_out = stack["session_svc"].execute_worker_step(
        req1.model_copy(
            update={
                "step_id": "step_02",
                "agent_id": "qa.e_worker",
                "idempotency_key": "e-collide",
                "timestamp": BASE_TIME + timedelta(seconds=15),
            }
        )
    )
    assert col_out.status == "FAILED"
    assert col_out.error_code == "PROVIDER_CONVERSATION_COLLISION"

    # 6 & 7. Handoff missing artifact & unauthorized receiver
    with pytest.raises(HandoffArtifactError, match="requires at least one"):
        stack["handoff_svc"].create_and_validate_handoff(
            HandoffCreateRequest(
                task_id=task.task_id,
                run_id=ok_out.run_id,
                plan_id=plan.plan_id,
                team_proposal_id=assigned.stored_proposal.proposal_id,
                source_step_id="step_01",
                target_step_id="step_02",
                from_agent_id="it.e_worker",
                to_agent_id="qa.e_worker",
                from_session_id=ok_out.session_id,
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ws_root),
                target_workspace_scope=f"workspace/{task.task_id}",
                artifact_ids=[],
                input_context_summary="Missing artifacts test.",
                expected_next_action="Fail closed.",
                timestamp=BASE_TIME + timedelta(seconds=17),
            )
        )
    with pytest.raises(HandoffDependencyError, match="Receiver"):
        stack["handoff_svc"].create_and_validate_handoff(
            HandoffCreateRequest(
                task_id=task.task_id,
                run_id=ok_out.run_id,
                plan_id=plan.plan_id,
                team_proposal_id=assigned.stored_proposal.proposal_id,
                source_step_id="step_01",
                target_step_id="step_02",
                from_agent_id="it.e_worker",
                to_agent_id="qa.stranger",
                from_session_id=ok_out.session_id,
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ws_root),
                target_workspace_scope=f"workspace/{task.task_id}",
                artifact_ids=[new_artifact_id()],
                input_context_summary="Unauthorized receiver test.",
                expected_next_action="Fail closed.",
                timestamp=BASE_TIME + timedelta(seconds=18),
            )
        )

    # 4 & 8. Leave an ACTIVE session persisted across SQLite restart -> NEEDS_RECONCILIATION + Idempotent replay
    act_init, _ = stack["session_svc"].initialize_worker_session(
        req1.model_copy(update={"idempotency_key": "e-active-crash", "timestamp": BASE_TIME + timedelta(seconds=19)})
    )
    stack["session_svc"].transition_session(
        act_init.session.session_id,
        target_status="ACTIVE",
        actor_id="core.master",
        actor_kind="orchestrator",
        reason="Start before simulated crash",
        evidence_summary="inv-crash-1",
        timestamp=BASE_TIME + timedelta(seconds=20),
        invocation_id="inv-crash-1",
    )
    store.close()

    reopened_stack = _open_stack(db_path, ws_root)
    try:
        recon = reopened_stack["session_svc"].reconcile_worker_sessions(
            task.task_id,
            timestamp=BASE_TIME + timedelta(seconds=25),
        )
        assert recon.recovery_status == "NEEDS_RECONCILIATION"
        assert reopened_stack["session_svc"].get_worker_runtime_state("it.e_worker", task_id=task.task_id).observable_running is False
        replay = reopened_stack["session_svc"].execute_worker_step(
            req1.model_copy(update={"timestamp": BASE_TIME + timedelta(seconds=26)})
        )
        assert replay.status == "IDEMPOTENT_REPLAY"
        assert len(reopened_stack["adapter"].calls) == 0
    finally:
        reopened_stack["store"].close()
