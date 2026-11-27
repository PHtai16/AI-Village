"""Unit, Governance, real ToolRuntime, and fail-closed tests for Phase 09 P09.05, P09.06, and P09.07."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError
import pytest

from core.departments import (
    DepartmentGovernanceError,
    IncomingSignalRequest,
    ItProductionMutationError,
    ItRdDepartmentService,
    ProcessDepartmentService,
    ProcessValidationError,
    ReceptionDepartmentService,
    ReceptionValidationError,
    SandboxTestExecutionRequest,
    TaskRetrospectiveInput,
    WorkspaceCodeAuthoringRequest,
    WorkspaceFileSpec,
)
from core.tools.models import ToolExecutionRecord
from tools.code import CommandRunResult
from tests.departments.conftest import (
    DepartmentTestHarness,
    create_task_and_run,
    make_department_harness,
    register_worker_agent,
    utc,
    worker_ctx,
)


@pytest.mark.parametrize(
    "blocked_path",
    [
        "core/departments/it_rd.py",
        "storage/db/migrations/0001_initial.sql",
        "agents/master/agent.yaml",
        "ui/src/App.jsx",
        "AGENTS.md",
        "pyproject.toml",
    ],
)
def test_p09_05_it_blocks_production_paths_and_runs_isolated_workspace_tests(
    tmp_path: Path,
    blocked_path: str,
) -> None:
    """Verify P09.05 rejects production edits fail-closed and runs sandboxed tests inside workspace."""
    with pytest.raises(ItProductionMutationError) as prod_err:
        WorkspaceFileSpec(relative_path=blocked_path, content="# tampered")
    assert prod_err.value.error_code == "PRODUCTION_MUTATION_FORBIDDEN"

    h = make_department_harness(
        tmp_path,
        sandbox_results={
            "test_fail.py": CommandRunResult(
                exit_code=1,
                stdout="1 failed in 0.05s",
                stderr="AssertionError: expected 4 == 5",
                duration_ms=42,
            ),
            "test_calc.py": CommandRunResult(
                exit_code=0,
                stdout="2 passed in 0.04s",
                stderr="",
                duration_ms=35,
            ),
        },
    )
    t0 = utc(9, 0, 0)
    try:
        task_id, run_id = create_task_and_run(
            h,
            title="Build isolated calculator package",
            description="Write module and tests strictly in task workspace.",
            timestamp=t0,
            assigned_agent_ids=["it.developer"],
        )
        register_worker_agent(
            h,
            agent_id="it.developer",
            department="it",
            skills=["file_writing", "code_execution", "automated_testing"],
            tools_allowed=["tools.files.write", "tools.code.run_sandbox"],
            timestamp=t0,
        )
        it_worker = worker_ctx(
            "it.developer",
            allowed_tools=["tools.files.write", "tools.code.run_sandbox"],
            assigned_task_ids=[task_id],
        )
        it_svc = ItRdDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            tool_invoker=h.tool_runtime,
        )

        code_out = it_svc.write_isolated_files(
            WorkspaceCodeAuthoringRequest(
                task_id=task_id,
                run_id=run_id,
                workspace_root=str(h.tmp_path),
                files=[
                    WorkspaceFileSpec(
                        relative_path=f"workspace/{task_id}/calc.py",
                        content="def add(a: int, b: int) -> int:\n    return a + b\n",
                    ),
                    WorkspaceFileSpec(
                        relative_path=f"workspace/{task_id}/test_calc.py",
                        content="from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                    ),
                    WorkspaceFileSpec(
                        relative_path=f"workspace/{task_id}/test_fail.py",
                        content="def test_fail():\n    assert 2 + 2 == 5\n",
                    ),
                ],
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=it_worker,
        )
        assert len(code_out.written_files) == 3
        assert code_out.business_verification_status == "UNVERIFIED"
        assert (h.workspace_root / task_id / "calc.py").is_file()

        pass_res = it_svc.run_sandbox_tests(
            SandboxTestExecutionRequest(
                task_id=task_id,
                run_id=run_id,
                workspace_root=str(h.tmp_path),
                operation="pytest",
                target_relpath=f"workspace/{task_id}/test_calc.py",
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=it_worker,
        )
        assert pass_res.exit_code == 0
        assert pass_res.sandbox_passed is True
        assert pass_res.business_verification_status == "UNVERIFIED"
        assert len(pass_res.evidence_refs) >= 1

        fail_res = it_svc.run_sandbox_tests(
            SandboxTestExecutionRequest(
                task_id=task_id,
                run_id=run_id,
                workspace_root=str(h.tmp_path),
                operation="pytest",
                target_relpath=f"workspace/{task_id}/test_fail.py",
                timestamp=t0 + timedelta(seconds=10),
            ),
            auth_context=it_worker,
        )
        assert fail_res.exit_code == 1
        assert fail_res.exit_state == "NON_ZERO_EXIT"
        assert fail_res.sandbox_passed is False
        assert "AssertionError" in fail_res.stderr

        # Path escape rejected fail-closed at boundary.
        with pytest.raises(ValidationError):
            WorkspaceFileSpec(relative_path="../outside.py", content="x = 1")
    finally:
        h.store.close()


def test_p09_06_reception_5_level_classification_targeted_routing_and_dedup(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.06 routes cleanly across 5 intake levels without alert storms or broadcast spam."""
    t0 = utc(10, 0, 0)
    task_a, run_a = create_task_and_run(
        dept_harness,
        title="Active Task A",
        description="Long-running task receiving refinement and anomaly signals.",
        timestamp=t0,
        assigned_agent_ids=["it.worker", "qa.verifier"],
    )
    reception = ReceptionDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        notification_store=dept_harness.store.metadata,
    )

    # 1. Queued independent request -> routes to Master without touching Task A.
    queued = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="user",
            summary="Build quarterly analytics briefing in workspace",
            target_department="marketing",
            timestamp=t0,
        ),
        auth_context=dept_harness.master,
    )
    assert queued.classification == "QUEUED"
    assert queued.route_targets == ["core.master", "marketing"]
    assert queued.broadcast_all_departments is False

    # 2. Appended refinement -> requires active task_id and routes strictly to Task A squad.
    with pytest.raises(ReceptionValidationError) as refine_err:
        reception.triage_incoming_signal(
            IncomingSignalRequest(
                source_channel="user",
                summary="Add CSV summary appendix",
                is_refinement_for_running_task=True,
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=dept_harness.master,
        )
    assert refine_err.value.error_code == "TASK_ID_REQUIRED_FOR_REFINEMENT"
    appended = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="user",
            summary="Add CSV summary appendix to Task A",
            task_id=task_a,
            run_id=run_a,
            active_task_squad=["it.worker", "qa.verifier"],
            is_refinement_for_running_task=True,
            timestamp=t0 + timedelta(seconds=2),
        ),
        auth_context=dept_harness.master,
    )
    assert appended.classification == "APPENDED"
    assert appended.route_targets == ["it.worker", "qa.verifier"]
    assert appended.escalated_to_master is False

    # 3. Anomaly routes cleanly to QA + Resources + Master and deduplicates repeated alarms.
    anom_1 = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="telemetry",
            summary="Tool retry spike detected on Task A",
            task_id=task_a,
            run_id=run_a,
            is_anomaly=True,
            dedup_key="retry_spike_task_a",
            timestamp=t0 + timedelta(seconds=3),
        ),
        auth_context=dept_harness.alice,
    )
    anom_2 = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="telemetry",
            summary="Tool retry spike detected on Task A",
            task_id=task_a,
            run_id=run_a,
            is_anomaly=True,
            dedup_key="retry_spike_task_a",
            timestamp=t0 + timedelta(seconds=4),
        ),
        auth_context=dept_harness.alice,
    )
    assert anom_1.classification == "ANOMALOUS"
    assert anom_1.route_targets == ["qa", "resources", "core.master"]
    assert anom_1.deduplicated is False and anom_1.occurrence_count == 1
    assert anom_2.deduplicated is True and anom_2.occurrence_count == 2
    assert anom_2.notification_id == anom_1.notification_id

    # 4. Approval required -> contributes APPROVAL_NEEDED Action Inbox item.
    apr_out = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="governance",
            summary="Operator approval required for department transfer",
            task_id=task_a,
            approval_id="apr_pending_01",
            requires_human_approval=True,
            timestamp=t0 + timedelta(seconds=5),
        ),
        auth_context=dept_harness.alice,
    )
    assert apr_out.classification == "APPROVAL_REQUIRED"
    assert apr_out.inbox_item.item_kind == "APPROVAL_NEEDED"

    # 5. Emergency -> CRITICAL alert escalated straight to Master with highest priority in Action Inbox.
    emerg = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="governance",
            summary="Potential raw API key token detected in stdout!",
            task_id=task_a,
            run_id=run_a,
            is_emergency=True,
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=dept_harness.alice,
    )
    assert emerg.classification == "EMERGENCY"
    assert emerg.severity == "CRITICAL"
    assert emerg.notification_type == "SECURITY_ALERT"
    assert emerg.escalated_to_master is True
    assert emerg.route_targets == ["core.master"]

    inbox = reception.list_action_inbox(task_id=task_a)
    assert inbox[0].priority == "CRITICAL"
    assert len(dept_harness.store.metadata.list_notifications(task_id=task_a)) == 4  # 1 deduplicated anomaly skipped


def test_p09_07_process_retrospectives_pattern_detection_and_no_self_activation(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.07 captures factual retrospectives and proposes unapproved candidate skills only on repeats."""
    t0 = utc(11, 0, 0)
    task_1, run_1 = create_task_and_run(
        dept_harness,
        title="Task 1 PDF parse failure",
        description="First occurrence of PDF extraction failure.",
        timestamp=t0,
    )
    task_2, run_2 = create_task_and_run(
        dept_harness,
        title="Task 2 PDF parse failure",
        description="Second occurrence across distinct task.",
        timestamp=t0 + timedelta(seconds=5),
    )

    proc = ProcessDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
    )
    proc_worker = worker_ctx("process.analyst", assigned_task_ids=[task_1, task_2])

    failed_rec = ToolExecutionRecord(
        execution_id="tex_pdf_fail_0001",
        tool_id="tools.office.read_pdf",
        task_id=task_1,
        run_id=run_1,
        actor_id="vault.archivist",
        action="office.read",
        status="FAILED",
        duration_ms=250,
        error_code="PDF_TABLE_MALFORMED",
        error_message="Table headers split across pages",
        executed_at=t0,
    )

    # 1. Single observation does NOT invent a recurring candidate skill.
    proc.record_task_retrospective(
        TaskRetrospectiveInput(
            task_id=task_1,
            run_id=run_1,
            departments_involved=["vault", "process"],
            tool_executions=[failed_rec],
            errors_observed=["Complex multi-page PDF table layout"],
            fixes_applied=["Fallback to deterministic OCR/page parser"],
            pattern_tag="pdf_table_extraction",
            duration_ms=1400,
            timestamp=t0 + timedelta(seconds=1),
        ),
        auth_context=proc_worker,
    )
    assert (
        proc.detect_patterns_and_propose_skills(
            auth_context=proc_worker,
            timestamp=t0 + timedelta(seconds=2),
        )
        == []
    )

    # Disallow lowering recurrence threshold below 2.
    with pytest.raises(ProcessValidationError, match="at least 2"):
        proc.detect_patterns_and_propose_skills(
            auth_context=proc_worker,
            timestamp=t0 + timedelta(seconds=3),
            min_occurrences=1,
        )

    # 2. Second independent task triggers real candidate proposal (unbenchmarked, unapproved!).
    proc.record_task_retrospective(
        TaskRetrospectiveInput(
            task_id=task_2,
            run_id=run_2,
            departments_involved=["vault", "qa"],
            tool_executions=[
                failed_rec.model_copy(
                    update={"execution_id": "tex_pdf_fail_0002", "task_id": task_2, "run_id": run_2}
                )
            ],
            errors_observed=["Multi-page table parse timeout"],
            fixes_applied=["Manual split by page range"],
            pattern_tag="pdf_table_extraction",
            duration_ms=1900,
            cost_usd=None,
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=proc_worker,
    )
    proposals = proc.detect_patterns_and_propose_skills(
        auth_context=proc_worker,
        timestamp=t0 + timedelta(seconds=7),
    )
    assert len(proposals) == 1
    cand = proposals[0]
    assert cand.suggested_skill_id == "pdf_table_extraction"
    assert cand.occurrence_count == 2
    assert cand.evidence_task_ids == [task_1, task_2]
    assert cand.status == "PROPOSED_CANDIDATE"
    assert cand.benchmark_status == "NOT_BENCHMARKED"
    assert cand.approved_for_production is False

    # Re-scanning does not duplicate already proposed patterns.
    assert (
        proc.detect_patterns_and_propose_skills(
            auth_context=proc_worker,
            timestamp=t0 + timedelta(seconds=8),
        )
        == []
    )
    # Unauthorized unassigned worker fails Governance check.
    with pytest.raises(DepartmentGovernanceError):
        proc.record_task_retrospective(
            TaskRetrospectiveInput(
                task_id=task_1,
                departments_involved=["process"],
                duration_ms=100,
                timestamp=t0 + timedelta(seconds=9),
            ),
            auth_context=worker_ctx("unknown_unauthenticated").model_copy(
                update={"authenticated": False}
            ),
        )
