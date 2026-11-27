"""Unit, Governance, SQLite persistence, and fail-closed tests for Phase 09 / P09.01 HR Department."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
    AgentCapabilities,
    AgentMission,
    AgentNotFoundError,
    ApprovedSkillDescriptor,
    CapabilityGapRequest,
    DepartmentTransferRequest,
    FailClosedEmptySkillCatalog,
    HrDepartmentService,
    HrExplorerQuery,
    HrRecruitmentDraftRequest,
    HrSkillApprovalError,
    HrStateError,
    HrValidationError,
    RecruitmentActivationRequest,
    RecruitmentEvaluationRequest,
    RoleChangeRequest,
    RuntimeStateReader,
    SkillGrantRequest,
    SkillRevokeRequest,
    StaleAgentVersionError,
    StaticApprovedSkillCatalog,
    load_agent_manifest_file,
    load_department_manifests,
)
from core.contracts import (
    Event,
    Run,
    Session,
    Task,
    new_run_id,
    new_session_id,
    new_task_id,
)
from core.events.journal import AppendResult
from core.governance import GovernanceAuditError, PermissionDeniedError
from core.governance.models import TrustedAuthContext
from pydantic import ValidationError
from core.orchestration.worker_session_models import WorkerRuntimeStateSnapshot
from core.orchestration.worker_sessions import WorkerSessionLifecycleService
from storage.repositories import PersistentWorkflowService, SQLiteRepositoryStore
from tests.agents.conftest import (
    AGENTS_ROOT,
    DEPARTMENTS_ROOT,
    HrTestHarness,
    REPO_ROOT,
    _operator_ctx,
    _utc,
    _worker_ctx,
    _worker_manifest_dict,
    approve_protected_operation,
    make_hr_harness,
)


def _unauthenticated_operator_ctx() -> TrustedAuthContext:
    """Return unauthenticated operator context hitting PolicyEvaluator."""
    return TrustedAuthContext(
        actor_id="operator.unauth",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=False,
    )


def test_p09_01_ast_layer_boundaries_and_fail_closed_skill_catalog(
    hr_harness: HrTestHarness,
) -> None:
    """Verify AST layer purity in core/agents/hr*.py and fail-closed default skill catalog."""
    forbidden_prefixes = (
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
        "core.orchestration",
    )
    for rel in ("core/agents/hr_models.py", "core/agents/hr.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"Forbidden import '{alias.name}' in {rel}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"Forbidden import-from '{node.module}' in {rel}"
                )

    empty_catalog = FailClosedEmptySkillCatalog()
    assert empty_catalog.get_approved_skill("any_skill") is None
    assert empty_catalog.list_approved_skills() == []

    with pytest.raises(HrSkillApprovalError) as unapproved_catalog_err:
        StaticApprovedSkillCatalog(
            [
                ApprovedSkillDescriptor(skill_id="ok_skill").model_copy(
                    update={"status": "DRAFT"}
                )
            ]
        )
    assert unapproved_catalog_err.value.error_code == "UNAPPROVED_SKILL"

    hr_default = HrDepartmentService(registry=hr_harness.registry)
    hr_harness.registry.register_agent(
        _worker_manifest_dict(agent_id="it.code_reviewer", department="it"),
        auth_context=hr_harness.alice,
        timestamp=_utc(9, 0, 0),
    )
    with pytest.raises(HrSkillApprovalError) as fail_closed_err:
        hr_default.grant_skills(
            SkillGrantRequest(
                agent_id="it.code_reviewer",
                skill_ids=["repo_audit"],
                expected_record_version=1,
                reason="Should fail closed without Phase 10 skill catalog",
                auth_context=hr_harness.alice,
                timestamp=_utc(9, 0, 5),
            )
        )
    assert fail_closed_err.value.error_code == "UNAPPROVED_SKILL"


def test_p09_01_end_to_end_roles_transfers_skills_and_persistence(
    tmp_path: Path,
) -> None:
    """Exercise change_role, transfer_department, grant/revoke_skills, disable/enable, and restart persistence."""
    h = make_hr_harness(tmp_path, db_name="hr_lifecycle.db")
    t0 = _utc(9, 0, 0)
    try:
        master_yaml = AGENTS_ROOT / "master" / "agent.yaml"
        worker_yaml = AGENTS_ROOT / "roles" / "it_artifact_worker" / "agent.yaml"
        master_m = load_agent_manifest_file(master_yaml, allowed_root=AGENTS_ROOT)
        worker_m = load_agent_manifest_file(worker_yaml, allowed_root=AGENTS_ROOT)

        h.registry.register_agent(
            master_m,
            auth_context=h.alice,
            source_path="master/agent.yaml",
            timestamp=t0,
        )
        w_reg = h.registry.register_agent(
            worker_m,
            auth_context=h.alice,
            source_path="roles/it_artifact_worker/agent.yaml",
            timestamp=t0 + timedelta(seconds=1),
        )
        assert w_reg.record.active_revision == 1

        # 1. Truthful Explorer catalog BEFORE any runtime observer: ACTIVE != RUNNING.
        catalog_v1 = h.hr.export_explorer_catalog(agents_root=AGENTS_ROOT)
        assert len(catalog_v1.departments) == 9
        assert catalog_v1.approved_skills_available == ["repo_audit", "security_audit_pack"]
        worker_item_v1 = h.hr.get_explorer_agent("it.artifact_worker", agents_root=AGENTS_ROOT)
        assert worker_item_v1.registration_status == "ACTIVE"
        assert worker_item_v1.runtime.runtime_state_source == "NOT_OBSERVED"
        assert worker_item_v1.runtime.observable_running is False
        assert worker_item_v1.runtime.session_initialized is False
        assert worker_item_v1.drift_status == "IN_SYNC"
        assert worker_item_v1.department_name == "IT and Engineering Department"
        assert worker_item_v1.department_zone_id == "zone_it"
        assert worker_item_v1.skills_allowed == []
        assert worker_item_v1.tools_allowed == []
        assert "tools.files.write" in worker_item_v1.department_advisory_tools

        # 2. Change role and add unvalidated capability -> immutable v2, ID & system_role preserved.
        role_out = h.hr.change_role(
            RoleChangeRequest(
                agent_id="it.artifact_worker",
                new_role="Lead Artifact & Contract Engineer",
                new_description="Authors code artifacts and reviews interface contracts.",
                new_mission=AgentMission(
                    purpose="Maintain workspace deliverables and check module contracts.",
                    responsibilities=["Write workspace code.", "Review API schemas."],
                    non_goals=["Never bypass QA verification."],
                ),
                new_capabilities=AgentCapabilities(
                    skills=[*worker_m.capabilities.skills, "api_contract_review"],
                    inputs=[*worker_m.capabilities.inputs, "openapi_spec"],
                    outputs=worker_m.capabilities.outputs,
                ),
                expected_record_version=1,
                change_summary="Expand worker role to include contract review",
                auth_context=h.alice,
                timestamp=t0 + timedelta(seconds=10),
            )
        )
        assert role_out.record.active_revision == 2
        assert role_out.record.record_version == 2
        assert role_out.record.manifest.id == "it.artifact_worker"
        assert role_out.record.manifest.system_role == "worker"
        assert role_out.record.manifest.role == "Lead Artifact & Contract Engineer"
        new_cap = [
            c for c in role_out.record.capability_verification if c.capability_id == "api_contract_review"
        ][0]
        assert new_cap.declared is True and new_cap.validated is False and new_cap.observed_in_execution is False

        # 3. Transfer department IT -> QA under human Approval Gate.
        _, dept_out = approve_protected_operation(
            h,
            action_fn=lambda apr_id: h.hr.transfer_department(
                DepartmentTransferRequest(
                    agent_id="it.artifact_worker",
                    target_department="qa",
                    expected_record_version=2,
                    reason="Reassign engineer temporarily to QA audit unit",
                    auth_context=h.alice,
                    approval_id=apr_id,
                    timestamp=t0 + timedelta(seconds=20),
                )
            ),
            requester=h.alice,
            decider=h.bob,
            risk_summary="Transfer it.artifact_worker to qa department",
            timestamp=t0 + timedelta(seconds=21),
        )
        assert dept_out.record.active_revision == 3
        assert dept_out.record.record_version == 3
        assert dept_out.record.manifest.department == "qa"

        # 4. Grant approved skill 'repo_audit' under Approval Gate -> reflected in Explorer & Registry summaries.
        _, grant_out = approve_protected_operation(
            h,
            action_fn=lambda apr_id: h.hr.grant_skills(
                SkillGrantRequest(
                    agent_id="it.artifact_worker",
                    skill_ids=["repo_audit"],
                    expected_record_version=3,
                    reason="Grant approved repository verification skill",
                    auth_context=h.alice,
                    approval_id=apr_id,
                    timestamp=t0 + timedelta(seconds=30),
                )
            ),
            requester=h.alice,
            decider=h.bob,
            risk_summary="Grant repo_audit skill to it.artifact_worker",
            timestamp=t0 + timedelta(seconds=31),
        )
        assert grant_out.record.active_revision == 4
        assert grant_out.record.manifest.skills.allowed == ["repo_audit"]

        explorer_v4 = h.hr.get_explorer_agent("it.artifact_worker", agents_root=AGENTS_ROOT)
        assert explorer_v4.skills_allowed == ["repo_audit"]
        assert explorer_v4.department == "qa"
        assert explorer_v4.department_name == "Quality Assurance Department"
        assert explorer_v4.drift_status == "DRIFT_DETECTED"
        assert "openapi_spec" in explorer_v4.declared_inputs

        reg_summaries = {s.agent_id: s for s in h.registry.export_explorer_summaries()}
        assert reg_summaries["it.artifact_worker"].skills_allowed == ["repo_audit"]

        # 5. Revoke skill -> revision v5 without deleting history.
        rev_out = h.hr.revoke_skills(
            SkillRevokeRequest(
                agent_id="it.artifact_worker",
                skill_ids=["repo_audit"],
                expected_record_version=4,
                reason="Offboard repo_audit access after audit cycle",
                auth_context=h.alice,
                timestamp=t0 + timedelta(seconds=40),
            )
        )
        assert rev_out.record.active_revision == 5
        assert rev_out.record.manifest.skills.allowed == []

        # 6. Lifecycle disable & enable through HR service.
        dis_out = h.hr.disable_agent(
            "it.artifact_worker",
            expected_record_version=5,
            auth_context=h.alice,
            timestamp=t0 + timedelta(seconds=50),
            reason="Scheduled worker rotation",
        )
        assert dis_out.record.registration_status == "DISABLED"
        en_out = h.hr.enable_agent(
            "it.artifact_worker",
            expected_record_version=dis_out.record.record_version,
            auth_context=h.alice,
            timestamp=t0 + timedelta(seconds=55),
            reason="Rotation finished",
        )
        assert en_out.record.registration_status == "ACTIVE"
        assert [v.manifest_revision for v in h.hr.get_version_history("it.artifact_worker")] == [1, 2, 3, 4, 5]
    finally:
        h.store.close()

    # Reopen SQLite after simulated restart and verify persisted HR state & audit trail.
    reopened = SQLiteRepositoryStore(h.db_path)
    try:
        from tests.agents.test_p05_02_agent_registry import _build_registry

        registry2, _, _ = _build_registry(reopened)
        hr2 = HrDepartmentService(
            registry=registry2,
            departments_root=DEPARTMENTS_ROOT,
            skill_catalog=StaticApprovedSkillCatalog(
                [ApprovedSkillDescriptor(skill_id="repo_audit")]
            ),
        )
        reloaded = hr2.get_explorer_agent("it.artifact_worker")
        assert reloaded.role == "Lead Artifact & Contract Engineer"
        assert reloaded.department == "qa"
        assert reloaded.skills_allowed == []
        assert reloaded.active_revision == 5
        assert reloaded.registration_status == "ACTIVE"
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("bad_actor_label", "auth_factory"),
    [
        ("none_auth", lambda: None),
        ("unauthenticated_operator", _unauthenticated_operator_ctx),
        ("worker_self", lambda: _worker_ctx("it.code_reviewer")),
        ("other_worker", lambda: _worker_ctx("qa.peer_worker")),
        ("master_agent", lambda: _operator_ctx("core.master").model_copy(
            update={"actor_kind": "master_agent", "auth_source": "application_boundary"}
        )),
    ],
)
def test_p09_01_governance_rejects_unauthorized_hr_mutations(
    hr_harness: HrTestHarness,
    bad_actor_label: str,
    auth_factory: Any,
) -> None:
    """Verify Worker, Master, None, unauthenticated, and spoofed actors cannot mutate HR state."""
    t0 = _utc(10, 0, 0)
    with pytest.raises(ValidationError):
        TrustedAuthContext(
            actor_id="operator.spoofed",
            actor_kind="human_operator",
            auth_source="agent_claimed",
            authenticated=True,
        )
    hr_harness.registry.register_agent(
        _worker_manifest_dict(
            agent_id="it.code_reviewer",
            department="it",
            skills=["workspace_artifact_authoring"],
        ),
        auth_context=hr_harness.alice,
        timestamp=t0,
    )
    bad_auth = auth_factory()

    with pytest.raises(PermissionDeniedError):
        hr_harness.hr.change_role(
            RoleChangeRequest(
                agent_id="it.code_reviewer",
                new_role=f"Escalated by {bad_actor_label}",
                expected_record_version=1,
                change_summary="Unauthorized role mutation attempt",
                auth_context=bad_auth,
                timestamp=t0 + timedelta(seconds=1),
            )
        )

    with pytest.raises(PermissionDeniedError):
        hr_harness.hr.transfer_department(
            DepartmentTransferRequest(
                agent_id="it.code_reviewer",
                target_department="qa",
                expected_record_version=1,
                reason="Unauthorized department transfer attempt",
                auth_context=bad_auth,
                timestamp=t0 + timedelta(seconds=2),
            )
        )

    with pytest.raises(PermissionDeniedError):
        hr_harness.hr.grant_skills(
            SkillGrantRequest(
                agent_id="it.code_reviewer",
                skill_ids=["repo_audit"],
                expected_record_version=1,
                reason="Unauthorized skill grant attempt",
                auth_context=bad_auth,
                timestamp=t0 + timedelta(seconds=3),
            )
        )


@pytest.mark.parametrize(
    ("bad_department", "expected_code"),
    [
        ("orchestration", "INVALID_DEPARTMENT"),
        ("openai", "INVALID_DEPARTMENT"),
        ("nonexistent_dept", "INVALID_DEPARTMENT"),
    ],
)
def test_p09_01_fail_closed_department_skill_master_and_concurrency_guards(
    tmp_path: Path,
    bad_department: str,
    expected_code: str,
) -> None:
    """Verify fail-closed Master immutability, invalid/inactive depts, unapproved skills, and optimistic locks."""
    h = make_hr_harness(tmp_path, db_name=f"guards_{bad_department}.db")
    t0 = _utc(11, 0, 0)
    try:
        master_m = load_agent_manifest_file(AGENTS_ROOT / "master" / "agent.yaml", allowed_root=AGENTS_ROOT)
        h.registry.register_agent(master_m, auth_context=h.alice, timestamp=t0)
        h.registry.register_agent(
            _worker_manifest_dict(
                agent_id="it.code_reviewer",
                department="it",
                skills=["workspace_artifact_authoring"],
            ),
            auth_context=h.alice,
            timestamp=t0 + timedelta(seconds=1),
        )

        # 1. core.master identity/role/department immutable against HR mutation.
        with pytest.raises(HrValidationError) as master_err:
            h.hr.change_role(
                RoleChangeRequest(
                    agent_id="core.master",
                    new_role="Renamed Orchestrator",
                    expected_record_version=1,
                    change_summary="Attempt to mutate Master role",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=2),
                )
            )
        assert master_err.value.error_code == "MASTER_IDENTITY_IMMUTABLE"

        with pytest.raises(HrValidationError) as master_dept_err:
            h.hr.transfer_department(
                DepartmentTransferRequest(
                    agent_id="core.master",
                    target_department="hr",
                    expected_record_version=1,
                    reason="Attempt to move Master to HR",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=3),
                )
            )
        assert master_dept_err.value.error_code == "MASTER_IDENTITY_IMMUTABLE"

        # 2. Invalid department rejected.
        with pytest.raises(HrValidationError) as dept_err:
            h.hr.transfer_department(
                DepartmentTransferRequest(
                    agent_id="it.code_reviewer",
                    target_department=bad_department,
                    expected_record_version=1,
                    reason="Invalid transfer target",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=4),
                )
            )
        assert dept_err.value.error_code == expected_code

        # 3. INACTIVE department rejected fail-closed when department catalog marks it inactive.
        depts = load_department_manifests(DEPARTMENTS_ROOT)
        depts["marketing"] = depts["marketing"].model_copy(update={"status": "INACTIVE"})
        hr_inactive = HrDepartmentService(
            registry=h.registry,
            departments=depts,
            skill_catalog=StaticApprovedSkillCatalog(
                [ApprovedSkillDescriptor(skill_id="repo_audit")]
            ),
        )
        with pytest.raises(HrValidationError) as inact_err:
            hr_inactive.transfer_department(
                DepartmentTransferRequest(
                    agent_id="it.code_reviewer",
                    target_department="marketing",
                    expected_record_version=1,
                    reason="Target department suspended",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=5),
                )
            )
        assert inact_err.value.error_code == "INACTIVE_DEPARTMENT"

        # 4. Unapproved skill and missing prerequisite capability fail closed.
        with pytest.raises(HrSkillApprovalError) as skill_err:
            h.hr.grant_skills(
                SkillGrantRequest(
                    agent_id="it.code_reviewer",
                    skill_ids=["unapproved_experimental_skill"],
                    expected_record_version=1,
                    reason="Attempt unapproved grant",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=6),
                )
            )
        assert skill_err.value.error_code == "UNAPPROVED_SKILL"

        with pytest.raises(HrValidationError) as cap_err:
            h.hr.grant_skills(
                SkillGrantRequest(
                    agent_id="it.code_reviewer",
                    skill_ids=["security_audit_pack"],
                    expected_record_version=1,
                    reason="Missing prerequisite security_audit capability",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=7),
                )
            )
        assert cap_err.value.error_code == "MISSING_REQUIRED_CAPABILITY"

        # 5. No-op state checks reject empty mutations cleanly.
        with pytest.raises(HrStateError, match="already assigned"):
            h.hr.transfer_department(
                DepartmentTransferRequest(
                    agent_id="it.code_reviewer",
                    target_department="it",
                    expected_record_version=1,
                    reason="Same department no-op",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=8),
                )
            )
        with pytest.raises(HrStateError, match="not currently granted"):
            h.hr.revoke_skills(
                SkillRevokeRequest(
                    agent_id="it.code_reviewer",
                    skill_ids=["repo_audit"],
                    expected_record_version=1,
                    reason="Revoke non-granted skill",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=9),
                )
            )

        # 6. Stale optimistic lock rejected cleanly without overwriting.
        h.hr.change_role(
            RoleChangeRequest(
                agent_id="it.code_reviewer",
                new_role="Senior Code Reviewer",
                expected_record_version=1,
                change_summary="Valid v2 role update",
                auth_context=h.alice,
                timestamp=t0 + timedelta(seconds=10),
            )
        )
        with pytest.raises(StaleAgentVersionError) as stale_err:
            h.hr.change_role(
                RoleChangeRequest(
                    agent_id="it.code_reviewer",
                    new_role="Stale Overwrite",
                    expected_record_version=1,
                    change_summary="Attempt stale overwrite",
                    auth_context=h.alice,
                    timestamp=t0 + timedelta(seconds=11),
                )
            )
        assert stale_err.value.error_code == "STALE_RECORD_VERSION"

        # 7. Unregistered target raises AgentNotFoundError.
        with pytest.raises(AgentNotFoundError):
            h.hr.get_explorer_agent("qa.missing_agent")
    finally:
        h.store.close()


def test_p09_01_active_run_preservation_and_journal_rollback(
    hr_harness: HrTestHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure active Runs keep pinned manifest revision on HR role update and audit failures roll back cleanly."""
    t0 = _utc(12, 0, 0)
    hr_harness.registry.register_agent(
        _worker_manifest_dict(agent_id="it.code_reviewer", department="it"),
        auth_context=hr_harness.alice,
        timestamp=t0,
    )

    wf = PersistentWorkflowService(hr_harness.store)
    task = Task(
        task_id=new_task_id(),
        title="Verify active run pinning",
        description="Keep active Run on manifest revision v1 during HR role update",
        requested_by="operator.alice",
        status="QUEUED",
        created_at=t0,
    )
    wf.create_task(task)
    run = Run(
        run_id=new_run_id(),
        task_id=task.task_id,
        runtime_id="custom.master_worker_v1",
        status="RUNNING",
        started_at=t0 + timedelta(seconds=1),
    )
    wf.create_run(run)
    hr_harness.store.sessions.save_session(
        Session(
            session_id=new_session_id(),
            agent_id="it.code_reviewer",
            task_id=task.task_id,
            run_id=run.run_id,
            provider_id="antigravity",
            connection_id="default_antigravity",
            provider_conversation_id="conv-hr-active-run",
            status="ACTIVE",
            started_at=t0 + timedelta(seconds=1),
        )
    )

    outcome = hr_harness.hr.change_role(
        RoleChangeRequest(
            agent_id="it.code_reviewer",
            new_role="Principal Reviewer",
            expected_record_version=1,
            change_summary="Promote worker while a Run is active",
            auth_context=hr_harness.alice,
            timestamp=t0 + timedelta(seconds=5),
        )
    )
    assert outcome.record.active_revision == 2
    assert outcome.active_run_ids_not_cancelled == [run.run_id]
    assert any("v1" in w for w in outcome.warnings)
    assert hr_harness.store.runs.get_run(run.run_id).status == "RUNNING"  # type: ignore[union-attr]
    v1_manifest = hr_harness.registry.get_manifest_version("it.code_reviewer", 1)
    assert v1_manifest is not None and v1_manifest.manifest.role == "Specialized Reviewer"

    # Inject Event Journal append failure and verify atomic rollback leaves v2 untouched.
    def _reject_append(event: Event) -> AppendResult:
        raise RuntimeError("Simulated SQLite journal disk error")

    monkeypatch.setattr(hr_harness.store.events, "append", _reject_append)
    with pytest.raises(GovernanceAuditError):
        hr_harness.hr.change_role(
            RoleChangeRequest(
                agent_id="it.code_reviewer",
                new_role="Should Roll Back",
                expected_record_version=2,
                change_summary="Must not commit when audit fails",
                auth_context=hr_harness.alice,
                timestamp=t0 + timedelta(seconds=10),
            )
        )
    current = hr_harness.hr.get_agent("it.code_reviewer")
    assert current is not None and current.active_revision == 2
    assert current.manifest.role == "Principal Reviewer"


def test_p09_01_capability_gap_recruitment_and_explorer_runtime_observer(
    hr_harness: HrTestHarness,
) -> None:
    """Verify HR gap analysis -> fail-closed recruitment proposal -> activation -> Explorer runtime join."""
    t0 = _utc(13, 0, 0)
    hr_harness.registry.register_agent(
        _worker_manifest_dict(agent_id="it.code_reviewer", department="it"),
        auth_context=hr_harness.alice,
        timestamp=t0,
    )

    # 1. Gap analysis shows security_audit missing in QA department.
    gap_before = hr_harness.hr.assess_capability_gap(
        CapabilityGapRequest(
            required_capabilities=["security_audit"],
            department="qa",
        )
    )
    assert gap_before.recommended_recruitment is True
    assert gap_before.covered_capabilities == []
    assert gap_before.missing_capabilities == ["security_audit"]
    assert gap_before.eligible_agent_ids == []
    assert "automated_testing" in gap_before.department_supported_capabilities

    # 2. Candidate manifest with unapproved skill rejected fail-closed BEFORE recruitment record creation.
    bad_candidate = _worker_manifest_dict(
        agent_id="qa.sec_auditor",
        department="qa",
        skills=["security_audit"],
    )
    bad_candidate["skills"] = {"allowed": ["unapproved_exploit_scan"]}
    with pytest.raises(HrSkillApprovalError) as bad_cand_err:
        hr_harness.hr.propose_recruitment(
            HrRecruitmentDraftRequest(
                candidate_manifest=bad_candidate,
                rationale="Attempt recruiting candidate with unapproved skill",
                needed_capabilities=["security_audit"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                auth_context=hr_harness.master_auth,
                timestamp=t0 + timedelta(seconds=2),
            )
        )
    assert bad_cand_err.value.error_code == "UNAPPROVED_SKILL"
    assert hr_harness.recruitment.list_proposals() == []

    # 3. Submit valid recruitment proposal through HR -> visible in Explorer open_recruitments.
    valid_candidate = _worker_manifest_dict(
        agent_id="qa.sec_auditor",
        department="qa",
        skills=["security_audit"],
    )
    proposed = hr_harness.hr.propose_recruitment(
        HrRecruitmentDraftRequest(
            candidate_manifest=valid_candidate,
            rationale="Fill missing security_audit capability in QA unit",
            needed_capabilities=["security_audit"],
            proposer_id="core.master",
            proposer_kind="orchestrator",
            auth_context=hr_harness.master_auth,
            timestamp=t0 + timedelta(seconds=5),
        )
    )
    catalog_open = hr_harness.hr.export_explorer_catalog()
    assert [p.proposal.proposal_id for p in catalog_open.open_recruitments] == [
        proposed.proposal.proposal_id
    ]

    # 4. Evaluate independently & activate candidate -> gap closed automatically.
    hr_harness.recruitment.evaluate_candidate(
        RecruitmentEvaluationRequest(
            proposal_id=proposed.proposal.proposal_id,
            evaluator_id="qa.lead_verifier",
            evaluator_kind="qa_verifier",
            summary="Validated security_audit manifest cleanly.",
            checks_passed=["capability_match"],
            timestamp=t0 + timedelta(seconds=8),
        )
    )
    hr_harness.recruitment.activate_candidate(
        RecruitmentActivationRequest(
            proposal_id=proposed.proposal.proposal_id,
            actor_id="operator.alice",
            actor_kind="operator",
            auth_context=hr_harness.alice,
            validated_capabilities=["security_audit"],
            timestamp=t0 + timedelta(seconds=10),
        )
    )

    gap_after = hr_harness.hr.assess_capability_gap(
        CapabilityGapRequest(
            required_capabilities=["security_audit"],
            department="qa",
        )
    )
    assert gap_after.recommended_recruitment is False
    assert gap_after.covered_capabilities == ["security_audit"]
    assert gap_after.missing_capabilities == []
    assert gap_after.eligible_agent_ids == ["qa.sec_auditor"]

    # 5. Wire structural RuntimeStateReader compatible with WorkerSessionLifecycleService snapshot.
    assert hasattr(WorkerSessionLifecycleService, "get_worker_runtime_state")
    task_id = new_task_id()
    run_id = new_run_id()
    sess_id = new_session_id()

    class _ObservedRuntimeReader:
        def get_worker_runtime_state(
            self, agent_id: str, *, task_id: str | None = None
        ) -> WorkerRuntimeStateSnapshot:
            is_target = agent_id == "qa.sec_auditor"
            return WorkerRuntimeStateSnapshot(
                agent_id=agent_id,
                task_id=task_id,
                in_registry=True,
                registry_status="ACTIVE",
                is_assigned_to_task=is_target,
                assigned_step_ids=["step_01"] if is_target else [],
                session_id=sess_id if is_target else None,
                session_status="ACTIVE" if is_target else None,
                session_initialized=is_target,
                session_active=is_target,
                observable_running=is_target,
                run_id=run_id if is_target else None,
                run_status="RUNNING" if is_target else None,
            )

    observer = _ObservedRuntimeReader()
    assert isinstance(observer, RuntimeStateReader)
    hr_observed = HrDepartmentService(
        registry=hr_harness.registry,
        recruitment_service=hr_harness.recruitment,
        departments=load_department_manifests(DEPARTMENTS_ROOT),
        skill_catalog=StaticApprovedSkillCatalog(
            [ApprovedSkillDescriptor(skill_id="security_audit_pack")]
        ),
        runtime_reader=observer,
    )
    filtered = hr_observed.export_explorer_catalog(
        query=HrExplorerQuery(department="qa", task_id=task_id)
    )
    assert len(filtered.agents) == 1
    qa_item = filtered.agents[0]
    assert qa_item.agent_id == "qa.sec_auditor"
    assert qa_item.runtime.runtime_state_source == "RUNTIME_OBSERVER"
    assert qa_item.runtime.observable_running is True
    assert qa_item.runtime.session_id == sess_id
    assert qa_item.runtime.run_id == run_id
    assert qa_item.runtime.assigned_step_ids == ["step_01"]
    assert qa_item.open_recruitment_proposal_ids == []
    assert filtered.open_recruitments == []
