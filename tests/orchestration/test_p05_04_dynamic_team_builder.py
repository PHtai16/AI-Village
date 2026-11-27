"""Unit, Governance, pinning, staleness, and SQLite persistence tests for Phase 05 / P05.04."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import ValidationError
import pytest

from core.agents.loader import load_agent_manifest_file
from core.agents.manifest import AgentManifest
from core.agents.registry import AgentRegistryService
from core.contracts import Task, new_task_id
from core.governance.approvals import ApprovalService
from core.governance.evaluator import PolicyEvaluator, build_default_policy_rules
from core.governance.manager import PermissionManager
from core.governance.models import ApprovalPolicyContext, PolicyRequest, PolicyRule, TrustedAuthContext
from core.orchestration import (
    DynamicTeamBuilderService,
    MasterPlanningService,
    PinnedAgentAssignment,
    PlanStepSpec,
    StaleTeamProposalError,
    StructuredUserBrief,
    TeamApprovalRequiredError,
    TeamAssignmentStateError,
    TeamBuildRequest,
    TeamGovernanceDeniedError,
)
from storage.db import apply_migrations, get_latest_migration_version, sqlite_connection
from storage.repositories import SQLiteRepositoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)


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


def _worker_auth(actor_id: str = "it.artifact_worker") -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def _make_worker_manifest(
    *,
    agent_id: str,
    department: str,
    skills: list[str],
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    provider_id: str = "antigravity",
    default_type: str = "antigravity",
    model_id: str | None = None,
    connection_id: str | None = "default_antigravity",
    fs_write: list[str] | None = None,
    tools_allowed: list[str] | None = None,
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "spec_version": "1.0.0",
            "id": agent_id,
            "name": f"Worker {agent_id}",
            "role": f"Specialist in {department}",
            "system_role": "worker",
            "department": department,
            "description": f"Valid fixture worker manifest for {agent_id}.",
            "mission": {
                "purpose": f"Execute bounded {department} work.",
                "responsibilities": ["Complete assigned workspace deliverable accurately."],
                "non_goals": ["Never bypass Governance or independent QA."],
            },
            "capabilities": {
                "skills": skills,
                "inputs": inputs or ["task_brief", "text", "structured_json"],
                "outputs": outputs or ["workspace_file_artifact", "structured_execution_report", "text"],
            },
            "engine": {
                "default_type": default_type,
                "provider_id": provider_id,
                "adapter_id": f"{provider_id}_cli" if provider_id == "antigravity" else provider_id,
                "connection_id": connection_id,
                "model_tier": "code",
                "model_id": model_id,
                "routing_profile": "workspace_sandboxed",
                "fallback_policy": "operator_approved_only",
            },
            "memory": {"scopes": ["private", "project"]},
            "skills": {"allowed": []},
            "tools": {"allowed": tools_allowed or []},
            "permissions": {
                "fs_read": ["workspace/{task_id}/inputs/*"],
                "fs_write": fs_write if fs_write is not None else ["workspace/{task_id}/outputs/*"],
                "network": "none",
                "requires_approval": ["files.delete"],
            },
            "limits": {"timeout_seconds": 120, "max_retries": 1},
        }
    )


def _setup_store(
    tmp_path: Path,
    *,
    rules: list[PolicyRule] | None = None,
    allowed_providers: list[str] | None = None,
    allowed_models: list[str] | None = None,
    registered_tools: list[str] | None = None,
) -> tuple[
    Path,
    SQLiteRepositoryStore,
    ApprovalService,
    PermissionManager,
    AgentRegistryService,
    MasterPlanningService,
    DynamicTeamBuilderService,
]:
    db_path = tmp_path / "p05_04.sqlite"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_path)
    approvals = ApprovalService(repository=store.approvals, journal=store.events)
    evaluator = PolicyEvaluator(rules=rules, registered_tools=registered_tools)
    pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers={"antigravity", "gemini", "openai", "ollama"},
        registered_tools=registered_tools,
    )
    agents_root = REPO_ROOT / "agents"
    op_auth = _operator_auth()
    registry.register_agent(
        load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    registry.register_agent(
        load_agent_manifest_file(
            agents_root / "roles" / "it_artifact_worker" / "agent.yaml",
            allowed_root=agents_root,
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/roles/it_artifact_worker/agent.yaml",
    )

    ws_root = tmp_path / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    planner = MasterPlanningService(
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        workspace_root=ws_root,
        allowed_providers=allowed_providers,
        allowed_models=allowed_models,
        registered_tools=registered_tools,
    )
    builder = DynamicTeamBuilderService(
        team_repo=store.teams,
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        allowed_providers=allowed_providers,
        allowed_models=allowed_models,
        registered_tools=registered_tools,
    )
    return db_path, store, approvals, pm, registry, planner, builder


def _seed_task(store: SQLiteRepositoryStore) -> Task:
    return store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Coordinate specialist deliverable",
            description="Plan and assemble minimal team without launching runs.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME,
        )
    ).task


def test_p05_04_core_layer_purity_and_pinned_assignment_contract() -> None:
    """Verify core/orchestration/team*.py AST purity and forbid core.master as worker."""
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
    for rel in ("core/orchestration/team_models.py", "core/orchestration/team_builder.py"):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, f"{rel} imported {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".")[0] not in forbidden, f"{rel} imported {node.module}"

    with pytest.raises(ValidationError, match="core.master"):
        PinnedAgentAssignment(
            agent_id="core.master",
            department="orchestration",
            manifest_revision=1,
            manifest_sha256="a" * 64,
            spec_version="1.0.0",
            engine_id="antigravity",
            assigned_step_ids=["step_01"],
            matched_capabilities=["goal_decomposition"],
            selection_reason="Should fail",
        )


def test_p05_04_minimal_cross_dept_team_excludes_irrelevant_and_inactive(tmp_path: Path) -> None:
    """Select minimal team, exclude core.master/DRAFT/DISABLED/VALIDATED, and honor verification levels."""
    _, store, _, _, registry, planner, builder = _setup_store(tmp_path)
    op_auth = _operator_auth()

    # Register valid multi-department fixture workers in tmp SQLite only.
    registry.register_agent(
        _make_worker_manifest(
            agent_id="qa.independent_verifier",
            department="qa",
            skills=["artifact_qa_inspection"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
        validated_capabilities=["artifact_qa_inspection"],
    )
    registry.register_agent(
        _make_worker_manifest(
            agent_id="marketing.copy_worker",
            department="marketing",
            skills=["campaign_copywriting"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
    )
    # Register a DRAFT worker that has all skills but is NOT ACTIVE -> must be excluded!
    registry.register_agent(
        _make_worker_manifest(
            agent_id="it.draft_super_worker",
            department="it",
            skills=["workspace_artifact_authoring", "artifact_qa_inspection", "goal_decomposition"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=3),
        initial_status="DRAFT",
    )
    # Register and DISABLE another capable QA worker -> must be excluded!
    dis_res = registry.register_agent(
        _make_worker_manifest(
            agent_id="qa.disabled_verifier",
            department="qa",
            skills=["artifact_qa_inspection"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=4),
    )
    registry.disable_agent(
        "qa.disabled_verifier",
        expected_record_version=dis_res.record.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=5),
        reason="Maintenance pause",
    )

    task = _seed_task(store)
    steps = [
        PlanStepSpec(
            step_id="step_01_author",
            order=0,
            title="Author workspace artifact",
            description="Generate structured markdown artifact inside workspace outputs.",
            required_capabilities=["workspace_artifact_authoring"],
            input_formats=["structured_json"],
            output_formats=["workspace_file_artifact"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/report.md"],
            allowed_write_paths=[f"workspace/{task.task_id}/outputs/report.md"],
            acceptance_criteria=["report.md exists"],
        ),
        PlanStepSpec(
            step_id="step_02_inspect",
            order=1,
            title="Verify deliverable quality",
            description="Inspect output artifact format and hash.",
            depends_on=["step_01_author"],
            required_capabilities=["artifact_qa_inspection"],
            input_formats=["text"],
            output_formats=["structured_execution_report"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/qa_report.json"],
            allowed_write_paths=[f"workspace/{task.task_id}/outputs/qa_report.json"],
            acceptance_criteria=["QA JSON report emitted"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Cross-dept workflow",
        objective="Produce artifact and inspect deliverable across IT and QA.",
        requested_by="operator.local",
        acceptance_criteria=["Both steps succeed cleanly"],
    )
    plan_out = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
        steps=steps,
    )
    assert plan_out.stored_plan is not None
    applied_plan, pending_items = planner.apply_plan_to_task(
        plan_out.stored_plan.plan_id,
        expected_plan_record_version=plan_out.stored_plan.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=11),
    )

    proposal_rec = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=15),
    )
    assert proposal_rec.status == "READY_FOR_ASSIGNMENT"
    selected_ids = [m.agent_id for m in proposal_rec.proposal.pinned_members]
    assert selected_ids == ["it.artifact_worker", "qa.independent_verifier"]
    assert "core.master" not in selected_ids
    assert "marketing.copy_worker" not in selected_ids
    assert "it.draft_super_worker" not in selected_ids
    assert "qa.disabled_verifier" not in selected_ids
    assert proposal_rec.proposal.departments_involved == ["it", "qa"]

    # Check honest telemetry/verification levels (no fabricated observed_in_execution or quotas).
    it_member = next(m for m in proposal_rec.proposal.pinned_members if m.agent_id == "it.artifact_worker")
    qa_member = next(m for m in proposal_rec.proposal.pinned_members if m.agent_id == "qa.independent_verifier")
    assert it_member.capability_evidence_levels["workspace_artifact_authoring"] == "declared"
    assert qa_member.capability_evidence_levels["artifact_qa_inspection"] == "validated"
    assert it_member.quota_remaining is None
    assert it_member.estimated_cost_usd is None

    # Verify exclusion audit reasons for DRAFT, DISABLED, and orchestrator.
    eval_map = {e.agent_id: e for e in proposal_rec.proposal.candidate_evaluations}
    assert "ORCHESTRATOR_NOT_WORKER" in eval_map["core.master"].exclusion_reasons
    assert "AGENT_NOT_ACTIVE:DRAFT" in eval_map["it.draft_super_worker"].exclusion_reasons
    assert "AGENT_NOT_ACTIVE:DISABLED" in eval_map["qa.disabled_verifier"].exclusion_reasons

    # Requiring observed_in_execution stops assignment honestly because no execution proof exists yet!
    obs_rec = builder.build_team_proposal(
        TeamBuildRequest(
            task_id=task.task_id,
            plan_id=applied_plan.plan_id,
            min_capability_level="observed_in_execution",
        ),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=16),
    )
    assert obs_rec.status == "BLOCKED_MISSING_CAPABILITY"
    assert obs_rec.proposal.pinned_members == []

    # Assign team atomically -> Task=ASSIGNED, ChecklistItem=PENDING with assigned_agent_id, zero Runs!
    assign_res = builder.apply_team_assignment(
        proposal_rec.proposal_id,
        expected_proposal_record_version=proposal_rec.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=20),
    )
    assert assign_res.task.status == "ASSIGNED"
    assert assign_res.task.assigned_agent_ids == ["it.artifact_worker", "qa.independent_verifier"]
    assert assign_res.task.active_run_id is None
    assert store.runs.list_runs_for_task(task.task_id) == []
    assert [c.assigned_agent_id for c in assign_res.checklist_items] == [
        "it.artifact_worker",
        "qa.independent_verifier",
    ]
    assert all(c.status == "PENDING" for c in assign_res.checklist_items)
    assert all(c.completed_at is None for c in assign_res.checklist_items)
    assert all(c.evidence_reference is None for c in assign_res.checklist_items)
    assert len(pending_items) == 2
    store.close()


def test_p05_04_formats_tools_routing_residency_and_governance(tmp_path: Path) -> None:
    """Enforce formats, tools allowlist, local_only residency, Governance denial, and Approval Gate."""
    approval_rules = [
        *build_default_policy_rules(),
        PolicyRule(
            rule_id="gov.require_approval_agent_assign_test",
            version="1.0.0",
            name="Require approval for agent.assign in strict mode",
            description="Require operator approval before assigning workers to tasks",
            category="approval",
            targets=["master_agent"],
            resources=["*"],
            priority=900,
            effect="REQUIRE_APPROVAL",
            actions=["agent.assign"],
            approval_required=True,
        ),
    ]
    _, store, approvals, pm, registry, planner, builder = _setup_store(
        tmp_path,
        rules=approval_rules,
        allowed_providers=["antigravity", "ollama"],
        registered_tools=["tools.files.read_file", "tools.files.write_file"],
    )
    op_auth = _operator_auth()

    # Register worker with matching skill but NO tool permission (must be excluded via TOOL_NOT_ALLOWED).
    registry.register_agent(
        _make_worker_manifest(
            agent_id="it.no_tools_worker",
            department="it",
            skills=["workspace_file_processing"],
            tools_allowed=[],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME,
    )
    # Register worker with tools.allowed=['tools.files.read_file'] via human approval (since tools.allowed is sensitive).
    worker_manifest = _make_worker_manifest(
        agent_id="it.tool_worker",
        department="it",
        skills=["workspace_file_processing"],
        provider_id="antigravity",
        default_type="antigravity",
        tools_allowed=["tools.files.read_file"],
    )
    reg_policy_dec = pm.evaluator.evaluate(
        PolicyRequest(
            auth_context=op_auth,
            action="agent.change_permissions",
            resource="agent:it.tool_worker",
            parameters={
                "target_agent_id": "it.tool_worker",
                "department": "it",
                "initial_status": "ACTIVE",
                "manifest_sha256": worker_manifest.canonical_sha256(),
                "sensitive_initial_grants": True,
            },
            timestamp=BASE_TIME + timedelta(seconds=1),
            correlation_id="corr-agent-reg-it.tool_worker",
        )
    )
    reg_req = approvals.create_approval_request(
        requester=_master_auth(),
        action="agent.change_permissions",
        target_resource="agent:it.tool_worker",
        task_id=None,
        run_id=None,
        request_fingerprint=reg_policy_dec.request_fingerprint,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=reg_policy_dec.matched_rule_ids,
            rule_versions=reg_policy_dec.rule_versions,
            reason_code=reg_policy_dec.reason_code,
        ),
        risk_summary="Approve initial tool permission for fixture worker",
        requested_at=BASE_TIME + timedelta(seconds=1),
    )
    approvals.decide_approval(
        reg_req.approval_id,
        decider=op_auth,
        approved=True,
        reason="Approved fixture tool worker",
        timestamp=BASE_TIME + timedelta(seconds=2),
    )
    registry.register_agent(
        worker_manifest,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=3),
        approval_id=reg_req.approval_id,
        correlation_id="corr-agent-reg-it.tool_worker",
    )

    task = _seed_task(store)
    step = PlanStepSpec(
        step_id="step_01_read",
        order=0,
        title="Read input safely",
        description="Read structured input using registered tool.",
        required_capabilities=["workspace_file_processing"],
        required_tools=["tools.files.read_file"],
        expected_outputs=[f"workspace/{task.task_id}/outputs/out.txt"],
        acceptance_criteria=["out.txt produced"],
    )
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Tool-checked task",
        objective="Execute tool-gated step within approved providers.",
        requested_by="operator.local",
        acceptance_criteria=["Step complete"],
    )
    plan_rec = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=5),
        steps=[step],
    ).stored_plan
    assert plan_rec is not None
    applied_plan, _ = planner.apply_plan_to_task(
        plan_rec.plan_id,
        expected_plan_record_version=plan_rec.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=6),
    )

    # 1) Local-only residency blocks antigravity cloud provider -> BLOCKED_GOVERNANCE.
    local_blocked = builder.build_team_proposal(
        TeamBuildRequest(
            task_id=task.task_id,
            plan_id=applied_plan.plan_id,
            required_data_residency="local_only",
        ),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=7),
    )
    assert local_blocked.status == "BLOCKED_GOVERNANCE"
    eval_tool = next(
        e for e in local_blocked.proposal.candidate_evaluations if e.agent_id == "it.tool_worker"
    )
    assert "DATA_RESIDENCY_BLOCKED:provider=antigravity" in eval_tool.exclusion_reasons

    # 2) Allowed proposal builds cleanly (selecting it.tool_worker, excluding it.no_tools_worker).
    ready = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=8),
    )
    assert ready.status == "READY_FOR_ASSIGNMENT"
    assert [m.agent_id for m in ready.proposal.pinned_members] == ["it.tool_worker"]
    no_tool_eval = next(
        e for e in ready.proposal.candidate_evaluations if e.agent_id == "it.no_tools_worker"
    )
    assert "TOOL_NOT_ALLOWED" in no_tool_eval.exclusion_reasons

    # 2b) Format mismatch check: unsupported output format excludes capable tool worker cleanly.
    fmt_step = step.model_copy(update={"step_id": "step_01_fmt", "output_formats": ["xlsx"]})
    fmt_plan = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=8, microseconds=500),
        steps=[fmt_step],
    ).stored_plan
    assert fmt_plan is not None
    fmt_prop = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=fmt_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=8, microseconds=700),
    )
    assert fmt_prop.status == "BLOCKED_GOVERNANCE"
    fmt_eval = next(
        e for e in fmt_prop.proposal.candidate_evaluations if e.agent_id == "it.tool_worker"
    )
    assert "FORMAT_MISMATCH:outputs" in fmt_eval.exclusion_reasons

    with pytest.raises(TeamGovernanceDeniedError):
        builder.build_team_proposal(
            TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
            auth_context=_worker_auth(),
            timestamp=BASE_TIME + timedelta(seconds=9),
        )
    with pytest.raises(TeamGovernanceDeniedError):
        builder.apply_team_assignment(
            ready.proposal_id,
            expected_proposal_record_version=ready.record_version,
            auth_context=_worker_auth(),
            timestamp=BASE_TIME + timedelta(seconds=10),
        )

    # 3) Missing approval raises TeamApprovalRequiredError; supplying approved token succeeds once.
    with pytest.raises(TeamApprovalRequiredError):
        builder.apply_team_assignment(
            ready.proposal_id,
            expected_proposal_record_version=ready.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=11),
        )

    assign_dec = pm.evaluator.evaluate(
        PolicyRequest(
            auth_context=_master_auth(),
            action="agent.assign",
            resource=f"task:{task.task_id}",
            task_id=task.task_id,
            routing=None,
            parameters={
                "proposal_id": ready.proposal_id,
                "task_id": task.task_id,
                "assigned_agents": ["it.tool_worker"],
                "expected_record_version": ready.record_version,
            },
            timestamp=BASE_TIME + timedelta(seconds=12),
            correlation_id=f"corr-team-assign-{ready.proposal_id}",
        )
    )
    assign_apr = approvals.create_approval_request(
        requester=_master_auth(),
        action="agent.assign",
        target_resource=f"task:{task.task_id}",
        task_id=task.task_id,
        run_id=None,
        request_fingerprint=assign_dec.request_fingerprint,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=assign_dec.matched_rule_ids,
            rule_versions=assign_dec.rule_versions,
            reason_code=assign_dec.reason_code,
        ),
        risk_summary="Approve worker team assignment",
        requested_at=BASE_TIME + timedelta(seconds=12),
    )
    approvals.decide_approval(
        assign_apr.approval_id,
        decider=op_auth,
        approved=True,
        reason="Operator approved team assignment",
        timestamp=BASE_TIME + timedelta(seconds=13),
    )
    assigned = builder.apply_team_assignment(
        ready.proposal_id,
        expected_proposal_record_version=ready.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=14),
        approval_id=assign_apr.approval_id,
    )
    assert assigned.task.status == "ASSIGNED"
    assert assigned.task.assigned_agent_ids == ["it.tool_worker"]
    store.close()


def test_p05_04_staleness_pinning_immutability_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detect Registry revision drift, block stale assignment, preserve history, and rollback faults."""
    db_path, store, _, _, registry, planner, builder = _setup_store(tmp_path)
    op_auth = _operator_auth()
    task = _seed_task(store)

    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Drift test",
        objective="Generate artifact inside task workspace cleanly.",
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/outputs/final.md"],
        acceptance_criteria=["final.md exists"],
    )
    plan_rec = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=5),
    ).stored_plan
    assert plan_rec is not None
    applied_plan, _ = planner.apply_plan_to_task(
        plan_rec.plan_id,
        expected_plan_record_version=plan_rec.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=6),
    )

    prop_v1 = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    assert prop_v1.proposal.pinned_members[0].manifest_revision == 1

    # Mutate it.artifact_worker in Registry to manifest revision v2 (non-escalating description update).
    worker_live = registry.get_agent("it.artifact_worker")
    assert worker_live is not None
    updated_manifest = worker_live.manifest.model_copy(
        update={"description": "Updated static description producing revision v2."}
    )
    registry.update_agent(
        "it.artifact_worker",
        updated_manifest,
        expected_record_version=worker_live.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=15),
        change_summary="Clarify worker description to revision v2",
    )

    # Direct assignment on drifted v1 proposal fails fail-closed BEFORE mutating Task.
    with pytest.raises(StaleTeamProposalError):
        builder.apply_team_assignment(
            prop_v1.proposal_id,
            expected_proposal_record_version=prop_v1.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=16),
        )

    report = builder.check_proposal_staleness(
        prop_v1.proposal_id,
        timestamp=BASE_TIME + timedelta(seconds=17),
        auth_context=_master_auth(),
        persist_if_stale=True,
    )
    assert report.is_stale is True
    assert report.issues[0].issue_code == "MANIFEST_REVISION_DRIFT"
    stale_stored = builder.get_proposal(prop_v1.proposal_id)
    assert stale_stored is not None
    assert stale_stored.status == "STALE"

    # Explicit rebuild produces rev 3 pinned to manifest revision v2 while keeping prior revisions immutable.
    rebuilt = builder.rebuild_team_proposal(
        prop_v1.proposal_id,
        expected_record_version=stale_stored.record_version,
        request=TeamBuildRequest(
            task_id=task.task_id,
            plan_id=applied_plan.plan_id,
            change_summary="Refresh pin to active it.artifact_worker revision v2",
        ),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=20),
    )
    assert rebuilt.status == "READY_FOR_ASSIGNMENT"
    assert rebuilt.proposal.pinned_members[0].manifest_revision == 2

    revs = builder.list_proposal_revisions(prop_v1.proposal_id)
    assert [r.proposal_revision for r in revs] == [1, 2, 3]
    assert revs[0].proposal.pinned_members[0].manifest_revision == 1
    assert revs[1].status == "STALE"
    assert revs[2].proposal.pinned_members[0].manifest_revision == 2

    # SQLite immutability triggers block direct UPDATE/DELETE on team_proposal_revisions.
    with store.open_connection() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE team_proposal_revisions SET change_summary = 'tamper' WHERE proposal_id = ?;",
                (prop_v1.proposal_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "DELETE FROM team_proposal_revisions WHERE proposal_id = ?;",
                (prop_v1.proposal_id,),
            )

    # Fault injection mid-transaction during apply_team_assignment rolls back Task, Checklist, Events, Proposal.
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Simulated mid-assignment SQLite crash")

    monkeypatch.setattr(store.tasks, "save_task_state_on_connection", _boom)
    with pytest.raises(RuntimeError, match="Simulated mid-assignment"):
        builder.apply_team_assignment(
            rebuilt.proposal_id,
            expected_proposal_record_version=rebuilt.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=25),
        )

    store.close()
    assert get_latest_migration_version() >= 6
    reopened = SQLiteRepositoryStore(db_path)
    try:
        task_reloaded = reopened.tasks.get_task(task.task_id)
        assert task_reloaded is not None
        assert task_reloaded.status == "QUEUED"
        assert task_reloaded.assigned_agent_ids == []
        prop_reloaded = reopened.teams.get_proposal(rebuilt.proposal_id)
        assert prop_reloaded is not None
        assert prop_reloaded.status == "READY_FOR_ASSIGNMENT"
        stale_events = reopened.events.read_entries(
            task_id=task.task_id, event_type="team.stale_detected"
        )
        assert len(stale_events) == 1
    finally:
        reopened.close()
