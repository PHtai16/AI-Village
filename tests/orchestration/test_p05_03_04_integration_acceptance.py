"""Phase 05 / P05.03 + P05.04 Section VII Offline End-to-End Integration Acceptance Suite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.agents.loader import load_agent_manifest_file
from core.agents.manifest import AgentManifest
from core.agents.registry import AgentRegistryService
from core.contracts import Task, new_task_id
from core.governance.evaluator import PolicyEvaluator
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext
from core.orchestration import (
    DynamicTeamBuilderService,
    MasterPlanningService,
    PlanStepSpec,
    StaleTeamProposalError,
    StructuredUserBrief,
    TeamAssignmentStateError,
    TeamBuildRequest,
)
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteRepositoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 11, 26, 17, 0, 0, tzinfo=timezone.utc)


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


def _fixture_worker(
    *,
    agent_id: str,
    department: str,
    skills: list[str],
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "spec_version": "1.0.0",
            "id": agent_id,
            "name": f"Fixture {agent_id}",
            "role": f"{department} Specialist",
            "system_role": "worker",
            "department": department,
            "description": f"Valid offline test fixture manifest for {agent_id}.",
            "mission": {
                "purpose": f"Execute bounded {department} steps.",
                "responsibilities": ["Deliver verified output within workspace boundary."],
                "non_goals": ["Never bypass QA or Governance."],
            },
            "capabilities": {
                "skills": skills,
                "inputs": inputs or ["task_brief", "text", "structured_json"],
                "outputs": outputs or ["workspace_file_artifact", "structured_execution_report", "text"],
            },
            "engine": {
                "default_type": "antigravity",
                "provider_id": "antigravity",
                "adapter_id": "antigravity_cli",
                "connection_id": "default_antigravity",
                "model_tier": "code",
                "model_id": None,
                "routing_profile": "workspace_sandboxed",
                "fallback_policy": "operator_approved_only",
            },
            "memory": {"scopes": ["private", "project"]},
            "skills": {"allowed": []},
            "tools": {"allowed": []},
            "permissions": {
                "fs_read": ["workspace/{task_id}/inputs/*"],
                "fs_write": ["workspace/{task_id}/outputs/*"],
                "network": "none",
                "requires_approval": ["files.delete"],
            },
            "limits": {"timeout_seconds": 120, "max_retries": 1},
        }
    )


def _bootstrap_runtime_env(
    tmp_path: Path,
) -> tuple[Path, SQLiteRepositoryStore, AgentRegistryService, MasterPlanningService, DynamicTeamBuilderService]:
    db_path = tmp_path / "acceptance.sqlite"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_path)
    pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers={"antigravity"},
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
    ws_root = tmp_path / "workspace_root"
    ws_root.mkdir(parents=True, exist_ok=True)
    planner = MasterPlanningService(
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        workspace_root=ws_root,
        allowed_providers=["antigravity"],
    )
    builder = DynamicTeamBuilderService(
        team_repo=store.teams,
        plan_repo=store.plans,
        agent_registry=registry,
        permission_manager=pm,
        allowed_providers=["antigravity"],
    )
    return db_path, store, registry, planner, builder


def test_scenario_1_simple_task_with_production_manifests_and_restart(tmp_path: Path) -> None:
    """Scenario 1: Production manifests plan and assign single Worker without starting Run."""
    db_path, store, _, planner, builder = _bootstrap_runtime_env(tmp_path)
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Author workspace summary deliverable",
            description="Generate output file from structured brief using active IT worker.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME,
        )
    ).task

    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Simple artifact deliverable",
        objective="Generate delivery artifact from task brief cleanly.",
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/outputs/summary.md"],
        acceptance_criteria=["summary.md written in task output folder"],
    )
    plan_outcome = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=5),
    )
    assert plan_outcome.outcome_status == "PLAN_READY"
    assert plan_outcome.stored_plan is not None
    applied_plan, _ = planner.apply_plan_to_task(
        plan_outcome.stored_plan.plan_id,
        expected_plan_record_version=plan_outcome.stored_plan.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=6),
    )

    proposal = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    assert proposal.status == "READY_FOR_ASSIGNMENT"
    assert [m.agent_id for m in proposal.proposal.pinned_members] == ["it.artifact_worker"]
    assert proposal.proposal.pinned_members[0].manifest_revision == 1

    assigned = builder.apply_team_assignment(
        proposal.proposal_id,
        expected_proposal_record_version=proposal.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=15),
    )
    assert assigned.task.status == "ASSIGNED"
    assert assigned.task.assigned_agent_ids == ["it.artifact_worker"]
    assert store.runs.list_runs_for_task(task.task_id) == []

    store.close()
    reopened = SQLiteRepositoryStore(db_path)
    try:
        task_after = reopened.tasks.get_task(task.task_id)
        assert task_after is not None
        assert task_after.status == "ASSIGNED"
        assert task_after.assigned_agent_ids == ["it.artifact_worker"]
        checklist = reopened.metadata.list_checklist_items(task.task_id)
        assert len(checklist) == 1
        assert checklist[0].status == "PENDING"
        assert checklist[0].assigned_agent_id == "it.artifact_worker"
        assert checklist[0].completed_at is None
        assert checklist[0].evidence_reference is None
        event_types = [e.event.event_type for e in reopened.events.read_entries(task_id=task.task_id)]
        assert {"plan.created", "plan.applied", "team.proposal_created", "task.status_changed", "team.assigned"} <= set(event_types)
    finally:
        reopened.close()


def test_scenario_2_multi_capability_fixture_team_and_minimal_selection(tmp_path: Path) -> None:
    """Scenario 2: Select minimal cross-department fixture team while excluding unnecessary agents."""
    db_path, store, registry, planner, builder = _bootstrap_runtime_env(tmp_path)
    op_auth = _operator_auth()

    registry.register_agent(
        _fixture_worker(
            agent_id="service.requirements_analyst",
            department="service",
            skills=["requirement_analysis"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
    )
    registry.register_agent(
        _fixture_worker(
            agent_id="qa.artifact_verifier",
            department="qa",
            skills=["qa_verification"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
    )
    registry.register_agent(
        _fixture_worker(
            agent_id="marketing.unused_writer",
            department="marketing",
            skills=["social_campaign_writing"],
        ),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=3),
    )

    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Three-stage analysis build and QA plan",
            description="Coordinate service analysis, IT authoring, and QA inspection.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME,
        )
    ).task

    steps = [
        PlanStepSpec(
            step_id="step_01_analyze",
            order=0,
            title="Analyze requirements",
            description="Produce structured spec notes.",
            required_capabilities=["requirement_analysis"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/spec.json"],
            allowed_write_paths=[f"workspace/{task.task_id}/outputs/spec.json"],
            acceptance_criteria=["spec.json written"],
        ),
        PlanStepSpec(
            step_id="step_02_build",
            order=1,
            title="Build artifact",
            description="Author final deliverable markdown.",
            depends_on=["step_01_analyze"],
            required_capabilities=["workspace_artifact_authoring"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/final.md"],
            allowed_write_paths=[f"workspace/{task.task_id}/outputs/final.md"],
            acceptance_criteria=["final.md written"],
        ),
        PlanStepSpec(
            step_id="step_03_qa",
            order=2,
            title="Inspect deliverable",
            description="Produce independent QA check output.",
            depends_on=["step_02_build"],
            required_capabilities=["qa_verification"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/qa.json"],
            allowed_write_paths=[f"workspace/{task.task_id}/outputs/qa.json"],
            acceptance_criteria=["qa.json verified"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Cross-department delivery",
        objective="Analyze brief, author artifact, and inspect deliverable across three departments.",
        requested_by="operator.local",
        acceptance_criteria=["All three checklist items verified independently"],
    )
    plan_rec = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
        steps=steps,
    ).stored_plan
    assert plan_rec is not None
    applied_plan, _ = planner.apply_plan_to_task(
        plan_rec.plan_id,
        expected_plan_record_version=plan_rec.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=11),
    )

    proposal = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=12),
    )
    assert proposal.status == "READY_FOR_ASSIGNMENT"
    chosen = [m.agent_id for m in proposal.proposal.pinned_members]
    assert chosen == ["it.artifact_worker", "qa.artifact_verifier", "service.requirements_analyst"]
    assert "marketing.unused_writer" not in chosen
    assert "core.master" not in chosen
    assert proposal.proposal.departments_involved == ["it", "qa", "service"]

    assign_res = builder.apply_team_assignment(
        proposal.proposal_id,
        expected_proposal_record_version=proposal.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=14),
    )
    assert [c.assigned_agent_id for c in assign_res.checklist_items] == [
        "service.requirements_analyst",
        "it.artifact_worker",
        "qa.artifact_verifier",
    ]
    assert assign_res.task.status == "ASSIGNED"
    assert store.runs.list_runs_for_task(task.task_id) == []

    store.close()
    reopened = SQLiteRepositoryStore(db_path)
    try:
        reloaded_prop = reopened.teams.get_active_proposal_for_task(task.task_id)
        assert reloaded_prop is not None
        assert reloaded_prop.status == "ASSIGNED"
        assert len(reloaded_prop.proposal.pinned_members) == 3
    finally:
        reopened.close()


def test_scenario_3_missing_capability_honest_blocking_no_fake_agents(tmp_path: Path) -> None:
    """Scenario 3: Missing capabilities stay honest (PARTIAL/BLOCKED), blocking assignment."""
    _, store, registry, planner, builder = _bootstrap_runtime_env(tmp_path)
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Need financial modeling specialist",
            description="Prepare workspace artifact and unstaffed financial audit model.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME,
        )
    ).task

    steps = [
        PlanStepSpec(
            step_id="step_01_doc",
            order=0,
            title="Write draft notes",
            description="Prepare initial artifact.",
            required_capabilities=["workspace_artifact_authoring"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/draft.md"],
            acceptance_criteria=["draft.md written"],
        ),
        PlanStepSpec(
            step_id="step_02_audit",
            order=1,
            title="Financial audit modeling",
            description="Run unavailable financial audit modeling step.",
            depends_on=["step_01_doc"],
            required_capabilities=["financial_audit_modeling"],
            expected_outputs=[f"workspace/{task.task_id}/outputs/audit_model.xlsx"],
            acceptance_criteria=["audit_model.xlsx generated"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Partial staffing brief",
        objective="Deliver document and financial audit modeling deliverable.",
        requested_by="operator.local",
        acceptance_criteria=["Complete verified audit model"],
    )
    plan_out = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=5),
        steps=steps,
    )
    assert plan_out.missing_capabilities == ["financial_audit_modeling"]
    assert plan_out.stored_plan is not None
    applied_plan, _ = planner.apply_plan_to_task(
        plan_out.stored_plan.plan_id,
        expected_plan_record_version=plan_out.stored_plan.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=6),
    )

    before_count = len(registry.list_agents(include_disabled=True))
    proposal = builder.build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    after_count = len(registry.list_agents(include_disabled=True))
    assert after_count == before_count  # Zero fake agents fabricated!
    assert proposal.status == "PARTIAL"
    assert proposal.proposal.missing_capabilities == ["financial_audit_modeling"]

    with pytest.raises(TeamAssignmentStateError) as assign_exc:
        builder.apply_team_assignment(
            proposal.proposal_id,
            expected_proposal_record_version=proposal.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=12),
        )
    assert assign_exc.value.error_code == "PROPOSAL_NOT_ASSIGNABLE"

    assert store.tasks.get_task(task.task_id).status == "QUEUED"
    assert store.tasks.get_task(task.task_id).assigned_agent_ids == []
    missing_evs = store.events.read_entries(task_id=task.task_id, event_type="team.missing_capability")
    assert len(missing_evs) == 1
    store.close()


def test_scenario_4_registry_drift_stale_detection_and_explicit_rebuild(tmp_path: Path) -> None:
    """Scenario 4: Detect Registry revision drift & agent disablement before assignment."""
    _, store, registry, planner, builder = _bootstrap_runtime_env(tmp_path)
    op_auth = _operator_auth()
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Track registry drift",
            description="Pin rev v1 then update/disable worker before assignment.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME,
        )
    ).task

    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Drift safeguard task",
        objective="Generate artifact inside task outputs safely.",
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/outputs/drift_safe.md"],
        acceptance_criteria=["drift_safe.md exists"],
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
        timestamp=BASE_TIME + timedelta(seconds=8),
    )
    assert prop_v1.proposal.pinned_members[0].manifest_revision == 1

    # Bump worker manifest to v2 in Registry.
    worker_live = registry.get_agent("it.artifact_worker")
    assert worker_live is not None
    registry.update_agent(
        "it.artifact_worker",
        worker_live.manifest.model_copy(update={"description": "Revision 2 updated description."}),
        expected_record_version=worker_live.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=10),
        change_summary="Update worker description to v2",
    )

    staleness = builder.check_proposal_staleness(
        prop_v1.proposal_id,
        timestamp=BASE_TIME + timedelta(seconds=11),
        auth_context=_master_auth(),
        persist_if_stale=True,
    )
    assert staleness.is_stale is True
    assert staleness.issues[0].issue_code == "MANIFEST_REVISION_DRIFT"
    with pytest.raises(StaleTeamProposalError):
        builder.apply_team_assignment(
            prop_v1.proposal_id,
            expected_proposal_record_version=prop_v1.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=12),
        )

    # Rebuild proposal pinning v2 explicitly, then disable worker -> detect AGENT_NOT_ACTIVE cleanly.
    stale_rec = builder.get_proposal(prop_v1.proposal_id)
    assert stale_rec is not None
    rebuilt_v2 = builder.rebuild_team_proposal(
        prop_v1.proposal_id,
        expected_record_version=stale_rec.record_version,
        request=TeamBuildRequest(
            task_id=task.task_id,
            plan_id=applied_plan.plan_id,
            change_summary="Pin refreshed manifest v2",
        ),
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=14),
    )
    assert rebuilt_v2.proposal.pinned_members[0].manifest_revision == 2

    worker_v2 = registry.get_agent("it.artifact_worker")
    assert worker_v2 is not None
    registry.disable_agent(
        "it.artifact_worker",
        expected_record_version=worker_v2.record_version,
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=16),
        reason="Temporarily disabled for maintenance",
    )
    staleness2 = builder.check_proposal_staleness(
        rebuilt_v2.proposal_id,
        timestamp=BASE_TIME + timedelta(seconds=17),
        auth_context=_master_auth(),
        persist_if_stale=False,
    )
    assert staleness2.is_stale is True
    assert staleness2.issues[0].issue_code == "AGENT_NOT_ACTIVE"
    with pytest.raises(StaleTeamProposalError):
        builder.apply_team_assignment(
            rebuilt_v2.proposal_id,
            expected_proposal_record_version=rebuilt_v2.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=18),
        )

    assert store.tasks.get_task(task.task_id).status == "QUEUED"
    assert store.runs.list_runs_for_task(task.task_id) == []
    store.close()
