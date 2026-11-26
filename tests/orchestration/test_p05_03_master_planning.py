"""Unit, persistence, security, and restart-recovery tests for Phase 05 / P05.03 Master Planning."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from pydantic import ValidationError
import pytest

from core.agents.loader import load_agent_manifest_file
from core.agents.registry import AgentRegistryService
from core.contracts import Task, new_task_id
from core.governance.evaluator import PolicyEvaluator
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext
from core.orchestration import (
    MasterPlan,
    MasterPlanningService,
    MasterWorkPlan,
    PlanApprovalRequiredError,
    PlanGovernanceDeniedError,
    PlanRoutingProposal,
    PlanStepSpec,
    PlanValidationError,
    SpikeAgentSpec,
    StalePlanVersionError,
    StructuredUserBrief,
    build_default_spike_roster,
    parse_master_plan_json,
)
from engines.connections import EngineConnectionRef
from engines.contract import (
    EngineAdapter,
    EngineCapabilities,
    EngineEvent,
    EngineRequest,
    EngineResult,
    EngineTelemetry,
)
from engines.registry import EngineRegistry
from storage.db import apply_migrations, get_latest_migration_version, sqlite_connection
from storage.repositories import SQLiteRepositoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc)


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


class _ScriptedPlanningAdapter(EngineAdapter):
    """Offline mock EngineAdapter returning pre-scripted JSON/text responses in order."""

    def __init__(self, responses: list[str], *, provider: str = "antigravity", adapter: str = "antigravity_cli") -> None:
        self._responses = list(responses)
        self._provider = provider
        self._adapter = adapter
        self.calls: list[EngineRequest] = []

    @property
    def provider_id(self) -> str:
        return self._provider

    @property
    def adapter_id(self) -> str:
        return self._adapter

    @property
    def platform_kind(self) -> str:
        return "agent_execution_platform"

    def get_capabilities(self, connection: EngineConnectionRef | None = None) -> EngineCapabilities:
        return EngineCapabilities(
            provider_id=self._provider,
            platform_kind="agent_execution_platform",
            data_residency="cloud",
            text_generation="SUPPORTED",
            structured_output="SUPPORTED",
        )

    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        self.calls.append(request)
        text = self._responses.pop(0) if self._responses else "{}"
        return EngineResult(
            request_id=request.request_id,
            provider_id=self._provider,
            adapter_id=self._adapter,
            connection_id=request.connection_id,
            model_id=request.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            response_text=text,
            started_at=BASE_TIME,
            finished_at=BASE_TIME + timedelta(milliseconds=15),
            duration_ms=15,
            telemetry=EngineTelemetry(
                duration_ms=15,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        )


def _build_env(
    tmp_path: Path,
    *,
    register_default_agents: bool = True,
    allowed_providers: list[str] | None = None,
    allowed_models: list[str] | None = None,
    engine_registry: EngineRegistry | None = None,
) -> tuple[Path, SQLiteRepositoryStore, PermissionManager, AgentRegistryService, MasterPlanningService]:
    db_path = tmp_path / "p05_03.sqlite"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_path)
    pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers={"antigravity", "gemini", "openai", "ollama"},
    )
    if register_default_agents:
        op_auth = _operator_auth()
        agents_root = REPO_ROOT / "agents"
        master_manifest = load_agent_manifest_file(
            agents_root / "master" / "agent.yaml",
            allowed_root=agents_root,
        )
        worker_manifest = load_agent_manifest_file(
            agents_root / "roles" / "it_artifact_worker" / "agent.yaml",
            allowed_root=agents_root,
        )
        registry.register_agent(
            master_manifest,
            auth_context=op_auth,
            timestamp=BASE_TIME,
            source_path="agents/master/agent.yaml",
        )
        registry.register_agent(
            worker_manifest,
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
        engine_registry=engine_registry,
        allowed_providers=allowed_providers,
        allowed_models=allowed_models,
        registered_tools={"tools.files.read_file", "tools.files.write_file"},
    )
    return db_path, store, pm, registry, planner


def _seed_queued_task(store: SQLiteRepositoryStore, *, task_id: str | None = None) -> Task:
    tid = task_id or new_task_id()
    task = Task(
        task_id=tid,
        title="Generate structured deliverable report",
        description="Analyze task inputs and author deliverable artifact inside task workspace.",
        requested_by="operator.local",
        status="QUEUED",
        priority="NORMAL",
        created_at=BASE_TIME,
    )
    return store.tasks.create_task(task).task


def test_p05_03_core_layer_purity_and_p03_compatibility() -> None:
    """Verify core/orchestration/planning*.py stays pure and preserves P03.01 models."""
    forbidden_roots = {
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
    for rel in ("core/orchestration/planning_models.py", "core/orchestration/planning.py"):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_roots, f"{rel} imported {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".")[0] not in forbidden_roots, f"{rel} imported {node.module}"

    roster = build_default_spike_roster()
    assert isinstance(roster["core.master"], SpikeAgentSpec)
    legacy_plan = MasterPlan(
        task_summary="Legacy single-worker spike compatibility check",
        target_worker_id="it.artifact_worker",
        worker_objective="Write proof.txt from input",
        allowed_read_paths=["inputs/package.json"],
        allowed_write_paths=["outputs/proof.txt"],
        acceptance_criteria=["proof.txt matches SHA-256"],
    )
    assert legacy_plan.target_worker_id == "it.artifact_worker"


def test_p05_03_create_apply_plan_and_restart_recovery(tmp_path: Path) -> None:
    """Create validated multi-step DAG plan, apply PENDING checklist to Task, and recover after restart."""
    db_path, store, _, _, planner = _build_env(tmp_path)
    task = _seed_queued_task(store)
    task_dir = f"workspace/{task.task_id}"

    steps = [
        PlanStepSpec(
            step_id="step_01_read_brief",
            order=0,
            title="Inspect source config",
            description="Read input JSON file safely within task workspace.",
            required_capabilities=["workspace_artifact_authoring"],
            input_formats=["structured_json"],
            output_formats=["text"],
            expected_inputs=[f"{task_dir}/inputs/source.json"],
            expected_outputs=[f"{task_dir}/outputs/notes.txt"],
            allowed_read_paths=[f"{task_dir}/inputs/source.json"],
            allowed_write_paths=[f"{task_dir}/outputs/notes.txt"],
            required_tools=["tools.files.read_file"],
            acceptance_criteria=["Input fields extracted accurately without modifying source"],
        ),
        PlanStepSpec(
            step_id="step_02_write_deliverable",
            order=1,
            title="Author final deliverable",
            description="Synthesize extracted facts into deliverable markdown artifact.",
            depends_on=["step_01_read_brief"],
            required_capabilities=["workspace_artifact_authoring"],
            input_formats=["text"],
            output_formats=["workspace_file_artifact"],
            expected_inputs=[f"{task_dir}/outputs/notes.txt"],
            expected_outputs=[f"{task_dir}/outputs/deliverable.md"],
            allowed_read_paths=[f"{task_dir}/outputs/notes.txt"],
            allowed_write_paths=[f"{task_dir}/outputs/deliverable.md"],
            required_tools=["tools.files.write_file"],
            acceptance_criteria=["deliverable.md exists inside workspace outputs and matches criteria"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Build workspace deliverable",
        objective="Extract input metadata and author deliverable artifact.",
        requested_by="operator.local",
        required_outputs=[f"{task_dir}/outputs/deliverable.md"],
        input_references=[f"{task_dir}/inputs/source.json"],
        acceptance_criteria=["Source file stays untouched", "Deliverable exists in outputs/"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
    )

    outcome = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
        steps=steps,
    )
    assert outcome.outcome_status == "PLAN_READY"
    assert outcome.plan is not None
    assert outcome.stored_plan is not None
    assert outcome.plan.status == "VALIDATED"
    assert outcome.plan.topological_order() == ["step_01_read_brief", "step_02_write_deliverable"]
    assert outcome.missing_capabilities == []
    assert outcome.plan.capability_assessments[0].status == "COVERED_DECLARED"
    assert "it.artifact_worker" in outcome.plan.capability_assessments[0].matching_active_agent_ids
    # Task remains strictly QUEUED and zero runs created during planning.
    assert store.tasks.get_task(task.task_id).status == "QUEUED"
    assert store.runs.list_runs_for_task(task.task_id) == []

    applied_plan, items = planner.apply_plan_to_task(
        outcome.plan.plan_id,
        expected_plan_record_version=outcome.stored_plan.record_version,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=20),
    )
    assert applied_plan.status == "APPLIED"
    assert len(items) == 2
    assert all(item.status == "PENDING" for item in items)
    assert all(item.completed_at is None for item in items)
    assert all(item.evidence_reference is None for item in items)
    assert all(item.assigned_agent_id is None for item in items)

    task_after = store.tasks.get_task(task.task_id)
    assert task_after is not None
    assert task_after.status == "QUEUED"
    assert task_after.assigned_agent_ids == []
    assert task_after.checklist_item_ids == [i.checklist_item_id for i in items]
    assert "Source file stays untouched" in task_after.acceptance_criteria
    assert store.runs.list_runs_for_task(task.task_id) == []

    # Close and reopen SQLite store to verify full restart persistence.
    store.close()
    assert get_latest_migration_version() >= 5
    reopened = SQLiteRepositoryStore(db_path)
    try:
        reloaded_plan = reopened.plans.get_active_plan_for_task(task.task_id)
        assert reloaded_plan is not None
        assert reloaded_plan.plan_id == outcome.plan.plan_id
        assert reloaded_plan.status == "APPLIED"
        reloaded_items = reopened.metadata.list_checklist_items(task.task_id)
        assert [i.checklist_item_id for i in reloaded_items] == [i.checklist_item_id for i in items]
        events = [e.event.event_type for e in reopened.events.read_entries(task_id=task.task_id)]
        assert "plan.created" in events
        assert "plan.applied" in events
    finally:
        reopened.close()


def test_p05_03_incomplete_brief_needs_clarification_and_blocks_apply(tmp_path: Path) -> None:
    """Ensure incomplete briefs trigger NEEDS_CLARIFICATION and cannot be applied to Task."""
    db_path, store, _, _, planner = _build_env(tmp_path)
    task = _seed_queued_task(store)
    vague_brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Unclear task",
        objective="tbd",
        requested_by="operator.local",
        required_outputs=[],
        acceptance_criteria=[],
    )
    outcome = planner.create_plan(
        brief=vague_brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=5),
    )
    assert outcome.outcome_status == "NEEDS_CLARIFICATION"
    assert outcome.stored_plan is not None
    assert outcome.stored_plan.status == "NEEDS_CLARIFICATION"
    missing_fields = {item.missing_field for item in outcome.clarifications}
    assert {"objective", "required_outputs", "acceptance_criteria"} <= missing_fields
    assert store.metadata.list_checklist_items(task.task_id) == []

    with pytest.raises(PlanValidationError) as exc_info:
        planner.apply_plan_to_task(
            outcome.stored_plan.plan_id,
            expected_plan_record_version=outcome.stored_plan.record_version,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=6),
        )
    assert exc_info.value.error_code == "PLAN_NOT_APPLICABLE"
    store.close()

    reopened = SQLiteRepositoryStore(db_path)
    try:
        reloaded = reopened.plans.get_plan(outcome.stored_plan.plan_id)
        assert reloaded is not None
        assert reloaded.status == "NEEDS_CLARIFICATION"
        assert len(reloaded.plan.missing_information) >= 3
        assert reopened.metadata.list_checklist_items(task.task_id) == []
    finally:
        reopened.close()


def test_p05_03_dag_validation_path_security_and_secret_rejection() -> None:
    """Reject cyclic DAGs, missing dependencies, duplicate steps, path traversal, and secrets."""
    tid = new_task_id()
    routing = PlanRoutingProposal(
        engine_id="antigravity",
        provider_id="antigravity",
        routing_reason="Standard test routing",
    )

    step_a = PlanStepSpec(
        step_id="step_alpha",
        order=0,
        title="Step Alpha",
        description="First step",
        depends_on=["step_beta"],
        required_capabilities=["workspace_artifact_authoring"],
        expected_outputs=["out/a.txt"],
        acceptance_criteria=["a.txt exists"],
    )
    step_b = PlanStepSpec(
        step_id="step_beta",
        order=1,
        title="Step Beta",
        description="Second step",
        depends_on=["step_alpha"],
        required_capabilities=["workspace_artifact_authoring"],
        expected_outputs=["out/b.txt"],
        acceptance_criteria=["b.txt exists"],
    )
    with pytest.raises(ValidationError, match="cycle"):
        MasterWorkPlan(
            task_id=tid,
            goal_summary="Cyclic plan should fail",
            steps=[step_a, step_b],
            acceptance_criteria=["done"],
            routing_proposal=routing,
            created_at=BASE_TIME,
        )

    # Missing dependency
    with pytest.raises(ValidationError, match="unknown dependency"):
        MasterWorkPlan(
            task_id=tid,
            goal_summary="Unknown dep plan",
            steps=[step_a],
            acceptance_criteria=["done"],
            routing_proposal=routing,
            created_at=BASE_TIME,
        )

    # Missing acceptance criteria on step
    with pytest.raises(ValidationError):
        PlanStepSpec(
            step_id="step_nocrit",
            order=0,
            title="No criteria",
            description="Missing criteria",
            required_capabilities=["workspace_artifact_authoring"],
            expected_outputs=["out/a.txt"],
            acceptance_criteria=[],
        )

    # Disallow core.master in proposed_agent_ids
    with pytest.raises(ValidationError, match="core.master"):
        PlanStepSpec(
            step_id="step_bad_agent",
            order=0,
            title="Bad proposal",
            description="Cannot assign core.master as worker",
            required_capabilities=["workspace_artifact_authoring"],
            expected_outputs=["out/a.txt"],
            acceptance_criteria=["done"],
            proposed_agent_ids=["core.master"],
        )

    # Reject path traversal and protected code directory writes
    with pytest.raises(ValidationError, match="traversal"):
        PlanStepSpec(
            step_id="step_escape",
            order=0,
            title="Traversal attempt",
            description="Try reading outside workspace",
            required_capabilities=["workspace_artifact_authoring"],
            expected_outputs=["out.txt"],
            allowed_read_paths=["../../secret.txt"],
            acceptance_criteria=["done"],
        )
    with pytest.raises(ValidationError, match="protected directory"):
        PlanStepSpec(
            step_id="step_write_core",
            order=0,
            title="Protected write attempt",
            description="Try writing into core/",
            required_capabilities=["workspace_artifact_authoring"],
            expected_outputs=["core/hack.py"],
            allowed_write_paths=["core/hack.py"],
            acceptance_criteria=["done"],
        )

    # Reject raw secrets in brief text and metadata
    with pytest.raises(ValidationError, match="secret"):
        StructuredUserBrief(
            task_id=tid,
            title="Leak secret",
            objective="Use key sk-1234567890abcdef directly",
            requested_by="operator.local",
        )
    with pytest.raises(ValidationError, match="secret"):
        StructuredUserBrief(
            task_id=tid,
            title="Clean title",
            objective="Clean multi word objective",
            requested_by="operator.local",
            metadata={"api_key": "plaintext_secret"},
        )

    # Duplicate step_id and duplicate order rejected fail-closed
    step_dup1 = PlanStepSpec(
        step_id="step_same",
        order=0,
        title="Step one",
        description="First copy",
        required_capabilities=["workspace_artifact_authoring"],
        expected_outputs=["out/one.txt"],
        acceptance_criteria=["one.txt exists"],
    )
    step_dup2 = PlanStepSpec(
        step_id="step_same",
        order=1,
        title="Step two",
        description="Duplicate id",
        required_capabilities=["workspace_artifact_authoring"],
        expected_outputs=["out/two.txt"],
        acceptance_criteria=["two.txt exists"],
    )
    with pytest.raises(ValidationError, match="Duplicate step_id"):
        MasterWorkPlan(
            task_id=tid,
            goal_summary="Duplicate step id check",
            steps=[step_dup1, step_dup2],
            acceptance_criteria=["done"],
            routing_proposal=routing,
            created_at=BASE_TIME,
        )

    step_order_dup = step_dup2.model_copy(update={"step_id": "step_other", "order": 0})
    with pytest.raises(ValidationError, match="Duplicate step order"):
        MasterWorkPlan(
            task_id=tid,
            goal_summary="Duplicate order check",
            steps=[step_dup1, step_order_dup],
            acceptance_criteria=["done"],
            routing_proposal=routing,
            created_at=BASE_TIME,
        )


def test_p05_03_engine_assisted_planning_retry_and_fail_closed(tmp_path: Path) -> None:
    """Verify strict JSON parsing, bounded retry, and fail-closed rejection on injection/malformed JSON."""
    valid_json = json.dumps(
        {
            "status": "VALIDATED",
            "goal_summary": "Author workspace summary via engine plan",
            "assumptions": ["Inputs exist under task_inputs/data.json"],
            "missing_information": [],
            "steps": [
                {
                    "step_id": "step_01_generate",
                    "order": 0,
                    "title": "Author report",
                    "description": "Write summary.md from data.json.",
                    "depends_on": [],
                    "required_capabilities": ["workspace_artifact_authoring", "financial_forecasting"],
                    "input_formats": ["structured_json"],
                    "output_formats": ["workspace_file_artifact"],
                    "expected_inputs": ["task_inputs/data.json"],
                    "expected_outputs": ["task_outputs/summary.md"],
                    "allowed_read_paths": ["task_inputs/data.json"],
                    "allowed_write_paths": ["task_outputs/summary.md"],
                    "required_tools": [],
                    "acceptance_criteria": ["summary.md written cleanly"],
                    "proposed_agent_ids": ["it.artifact_worker"],
                    "requires_human_approval": False,
                }
            ],
            "acceptance_criteria": ["Independent QA checks summary.md hash"],
            "routing_proposal": {
                "engine_id": "antigravity",
                "provider_id": "antigravity",
                "data_residency_policy": "cloud_allowed",
                "routing_reason": "Approved Antigravity execution engine",
            },
        }
    )

    adapter = _ScriptedPlanningAdapter(
        responses=["Here is my plan without strict json: ...", valid_json]
    )
    eng_reg = EngineRegistry()
    eng_reg.register_adapter(adapter, set_default_for_provider=True)

    _, store, _, _, planner = _build_env(tmp_path, engine_registry=eng_reg)
    task = _seed_queued_task(store)
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Engine assisted plan",
        objective="Generate summary markdown deliverable and forecast analysis.",
        requested_by="operator.local",
        required_outputs=["task_outputs/summary.md"],
        acceptance_criteria=["summary.md verified"],
    )

    outcome = planner.create_plan_with_engine(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=15),
        max_json_retries=1,
    )
    assert len(adapter.calls) == 2
    assert outcome.outcome_status == "PLAN_READY"
    assert outcome.plan is not None
    assert outcome.plan.planning_source == "engine_assisted"
    # Honest capability assessment flags missing financial_forecasting without inventing a worker.
    assert outcome.missing_capabilities == ["financial_forecasting"]

    # Prompt injection trying to mark task COMPLETED or escape workspace fails closed.
    bad_adapter = _ScriptedPlanningAdapter(
        responses=[
            json.dumps(
                {
                    "status": "COMPLETED",
                    "goal_summary": "Fake complete",
                    "steps": [],
                    "acceptance_criteria": ["none"],
                    "routing_proposal": {"engine_id": "antigravity", "routing_reason": "x"},
                }
            ),
            "still not valid json!",
        ]
    )
    eng_reg2 = EngineRegistry()
    eng_reg2.register_adapter(bad_adapter, set_default_for_provider=True)
    planner2 = MasterPlanningService(
        plan_repo=store.plans,
        agent_registry=planner._registry,  # noqa: SLF001
        permission_manager=planner._pm,  # noqa: SLF001
        engine_registry=eng_reg2,
    )
    fail_outcome = planner2.create_plan_with_engine(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=20),
        max_json_retries=1,
    )
    assert fail_outcome.outcome_status == "FAILED_VALIDATION"
    assert fail_outcome.stored_plan is None
    store.close()


def test_p05_03_governance_denies_worker_and_invalid_routing(tmp_path: Path) -> None:
    """Enforce PermissionManager blocking worker_agent and invalid provider/model/residency routing."""
    _, store, _, _, planner = _build_env(
        tmp_path,
        allowed_providers=["antigravity"],
        allowed_models=["gemini-2.5-pro"],
    )
    task = _seed_queued_task(store)
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Governed task",
        objective="Produce safe deliverable inside isolated workspace folder.",
        requested_by="operator.local",
        required_outputs=["outputs/result.txt"],
        acceptance_criteria=["result.txt exists"],
    )

    with pytest.raises(PlanGovernanceDeniedError):
        planner.create_plan(
            brief=brief,
            auth_context=_worker_auth(),
            timestamp=BASE_TIME + timedelta(seconds=5),
        )

    bad_provider_brief = brief.model_copy(update={"preferred_provider_id": "openai"})
    with pytest.raises(PlanGovernanceDeniedError) as prov_exc:
        planner.create_plan(
            brief=bad_provider_brief,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=6),
        )
    assert prov_exc.value.error_code == "PROVIDER_NOT_ALLOWED"

    bad_residency_brief = brief.model_copy(
        update={
            "preferred_provider_id": "antigravity",
            "data_residency_policy": "local_only",
        }
    )
    with pytest.raises(PlanGovernanceDeniedError) as res_exc:
        planner.create_plan(
            brief=bad_residency_brief,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=7),
        )
    assert res_exc.value.error_code == "LOCAL_ONLY_VIOLATION"

    bad_model_brief = brief.model_copy(
        update={"preferred_provider_id": "antigravity", "preferred_model_id": "unapproved-model"}
    )
    with pytest.raises(PlanGovernanceDeniedError) as mod_exc:
        planner.create_plan(
            brief=bad_model_brief,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=8),
        )
    assert mod_exc.value.error_code == "MODEL_NOT_ALLOWED"

    # REQUIRE_APPROVAL rule on task.update_plan must raise PlanApprovalRequiredError
    from core.governance.evaluator import build_default_policy_rules
    from core.governance.models import PolicyRule

    approval_rules = [
        *build_default_policy_rules(),
        PolicyRule(
            rule_id="gov.require_approval_master_plan_test",
            version="1.0.0",
            name="Require approval for plan updates in strict mode",
            description="Require operator approval for task.update_plan",
            category="approval",
            targets=["master_agent"],
            resources=["*"],
            priority=900,
            effect="REQUIRE_APPROVAL",
            actions=["task.update_plan"],
            approval_required=True,
        ),
    ]
    strict_pm = PermissionManager(
        evaluator=PolicyEvaluator(rules=approval_rules),
        journal=store.events,
    )
    strict_planner = MasterPlanningService(
        plan_repo=store.plans,
        agent_registry=planner._registry,  # noqa: SLF001
        permission_manager=strict_pm,
    )
    with pytest.raises(PlanApprovalRequiredError):
        strict_planner.create_plan(
            brief=brief,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=9),
        )
    store.close()


def test_p05_03_revisions_optimistic_lock_immutability_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify immutable plan revisions, optimistic lock conflict, SQLite triggers, and rollback."""
    db_path, store, _, _, planner = _build_env(tmp_path)
    task = _seed_queued_task(store)
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title="Revision test",
        objective="Author versioned workspace deliverables safely.",
        requested_by="operator.local",
        required_outputs=["outputs/v1.md"],
        acceptance_criteria=["v1.md exists"],
    )
    v1_out = planner.create_plan(
        brief=brief,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=10),
    )
    assert v1_out.stored_plan is not None
    plan_id = v1_out.stored_plan.plan_id

    v2_step = PlanStepSpec(
        step_id="step_01_revised",
        order=0,
        title="Revised delivery step",
        description="Produce v2 output markdown with checklist detail.",
        required_capabilities=["workspace_artifact_authoring"],
        expected_outputs=["outputs/v2.md"],
        allowed_write_paths=[f"workspace/{task.task_id}/outputs/v2.md"],
        acceptance_criteria=["v2.md validated"],
    )
    v2_out = planner.revise_plan(
        plan_id,
        expected_record_version=1,
        auth_context=_master_auth(),
        timestamp=BASE_TIME + timedelta(seconds=20),
        change_summary="Refine deliverable scope to v2.md",
        steps=[v2_step],
    )
    assert v2_out.stored_plan is not None
    assert v2_out.stored_plan.active_revision == 2
    assert v2_out.stored_plan.record_version == 2

    revisions = planner.list_plan_revisions(plan_id)
    assert [r.plan_revision for r in revisions] == [1, 2]
    assert revisions[0].plan.steps[0].expected_outputs == ["outputs/v1.md"]
    assert revisions[1].plan.steps[0].expected_outputs == ["outputs/v2.md"]

    # Optimistic lock rejects stale record_version=1.
    with pytest.raises(StalePlanVersionError):
        planner.revise_plan(
            plan_id,
            expected_record_version=1,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=25),
            change_summary="Stale write attempt",
            steps=[v2_step],
        )

    # SQLite immutability triggers forbid UPDATE/DELETE on master_plan_revisions.
    with store.open_connection() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE master_plan_revisions SET change_summary = 'tampered' WHERE plan_id = ?;",
                (plan_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM master_plan_revisions WHERE plan_id = ?;", (plan_id,))

    # Rollback atomicity: force failure in tasks.save_task_state_on_connection mid-apply.
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Injected Task update failure mid-transaction")

    monkeypatch.setattr(store.tasks, "save_task_state_on_connection", _boom)
    with pytest.raises(RuntimeError, match="Injected Task update failure"):
        planner.apply_plan_to_task(
            plan_id,
            expected_plan_record_version=2,
            auth_context=_master_auth(),
            timestamp=BASE_TIME + timedelta(seconds=30),
        )

    # Entire transaction rolled back: plan remains VALIDATED rev 2 and zero ChecklistItems persisted.
    plan_after_fail = store.plans.get_plan(plan_id)
    assert plan_after_fail is not None
    assert plan_after_fail.status == "VALIDATED"
    assert plan_after_fail.record_version == 2
    assert store.metadata.list_checklist_items(task.task_id) == []
    assert store.tasks.get_task(task.task_id).checklist_item_ids == []
    store.close()
