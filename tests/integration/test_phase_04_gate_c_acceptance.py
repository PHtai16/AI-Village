"""Gate C Offline End-to-End Acceptance Suite for AI Village Phase 04."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from core.contracts import (
    Artifact,
    ChecklistItem,
    ConnectionRef,
    Event,
    ProjectRef,
    Run,
    RunTelemetryRef,
    Session,
    Task,
    new_artifact_id,
    new_checklist_item_id,
    new_event_id,
    new_evidence_id,
    new_notification_id,
    new_project_id,
    new_run_id,
    new_session_id,
    new_task_id,
)
from core.contracts import NotificationRef
from core.events import project_activity_timeline, replay_task_history
from core.governance import (
    ApprovalError,
    ApprovalPolicyContext,
    ApprovalService,
    PermissionDeniedError,
    PermissionManager,
    PolicyEvaluator,
    PolicyRequest,
    TrustedAuthContext,
    compute_request_fingerprint,
)
from core.runtime import CustomAgentRuntime
from core.tasks import (
    ExecutionEvidence,
    IdempotentSubmissionRequest,
    TransitionContext,
    WorkflowLifecycleController,
    qa_evidence_from_engine_verification,
)
from engines import EngineRegistry
from engines.antigravity.lifecycle import OwnedSubprocessHandle
from engines.antigravity.streaming import is_pid_alive
from engines.contract import EngineResult, EngineTelemetry
from engines.verification import verify_engine_artifact
from storage.db import apply_migrations, get_latest_migration_version, sqlite_connection
from storage.repositories import (
    PersistentWorkflowService,
    SQLiteLifecyclePersistenceAdapter,
    SQLiteRepositoryStore,
    recover_task,
)


def _utc(hour: int = 15, minute: int = 0, second: int = 0) -> datetime:
    """Return deterministic UTC timestamp."""
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=timezone.utc)


def _init_db(db_path: Path) -> None:
    """Initialize SQLite database to latest schema version."""
    with sqlite_connection(db_path) as conn:
        result = apply_migrations(conn)
        assert result.current_version == get_latest_migration_version()
        assert result.current_version >= 3


def _spawn_sleeper_subprocess(seconds: int = 30) -> subprocess.Popen[str]:
    """Spawn a real Python child process owned by the test."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_gate_c_01_full_workflow_governance_approval_subprocess_qa_and_restart_recovery(
    tmp_path: Path,
) -> None:
    """Verify full Task/Run/Governance/Approval Pause-Resume/Owned Subprocess/QA/Restart pipeline."""
    db_path = tmp_path / "gate_c_happy.db"
    ws_dir = tmp_path / "ws_gate_c"
    ws_dir.mkdir()
    _init_db(db_path)

    # Confirm CustomAgentRuntime still accurately declares no mid-spike workflow cancellation.
    assert (
        CustomAgentRuntime(registry=EngineRegistry()).get_runtime_info().supports_workflow_cancellation
        is False
    )

    t0 = _utc(15, 0, 0)
    task_id = new_task_id()
    project_id = new_project_id()
    session_id = new_session_id()
    checklist_id = new_checklist_item_id()
    artifact_id = new_artifact_id()
    evidence_id = new_evidence_id()
    artifact_rel = "output/gate_c_deliverable.txt"

    master_ctx = TrustedAuthContext(
        actor_id="core.master",
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )
    worker_ctx = TrustedAuthContext(
        actor_id="dev.backend",
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_tools=["tools.filesystem.read_file"],
    )
    operator_ctx = TrustedAuthContext(
        actor_id="operator.alice",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )

    with SQLiteRepositoryStore(db_path) as store1:
        svc1 = PersistentWorkflowService(store1)
        adapter1 = SQLiteLifecyclePersistenceAdapter(store1)
        evaluator = PolicyEvaluator(registered_tools=["tools.filesystem.read_file"])
        approvals = ApprovalService(store1.approvals, store1.events)
        pm1 = PermissionManager(evaluator=evaluator, journal=store1.events, approval_service=approvals)
        master_ctrl = WorkflowLifecycleController(
            adapter1, permission_hook=pm1.build_lifecycle_hook(master_ctx)
        )

        store1.metadata.save_project(ProjectRef(project_id=project_id, name="Gate C Verification"))
        store1.metadata.save_connection(
            ConnectionRef(
                provider_id="antigravity",
                connection_id="default_antigravity",
                model_id="gemini-2.5-pro",
                credential_ref="env:AGY_TOKEN",
            )
        )

        # 1) Create Task via Governance & Persist to SQLite.
        _, create_outcome = pm1.dispatch_protected_action(
            PolicyRequest(
                auth_context=operator_ctx,
                action="task.create",
                resource=f"task:{task_id}",
                task_id=task_id,
                timestamp=t0,
            ),
            lambda: svc1.create_task(
                Task(
                    task_id=task_id,
                    project_id=project_id,
                    title="Gate C end-to-end verification task",
                    description="Exercise governance, approval pause/resume, owned subprocess, QA, and restart replay.",
                    requested_by="operator.alice",
                    status="QUEUED",
                    priority="HIGH",
                    created_at=t0,
                    checklist_item_ids=[checklist_id],
                    acceptance_criteria=["Artifact verified by independent QA SHA-256"],
                ),
                correlation_id="corr-gate-c-01",
            ),
        )
        assert create_outcome.accepted is True

        # Governance blocks Master QA bypass & Worker workspace escape before work starts.
        with pytest.raises(PermissionDeniedError) as qa_bypass_err:
            pm1.dispatch_protected_action(
                PolicyRequest(
                    auth_context=master_ctx,
                    action="task.transition",
                    resource=f"task:{task_id}",
                    task_id=task_id,
                    parameters={"target_status": "COMPLETED"},
                    timestamp=t0 + timedelta(seconds=1),
                ),
                lambda: "must_never_run",
            )
        assert qa_bypass_err.value.decision.reason_code == "QA_BYPASS_DENIED"

        with pytest.raises(PermissionDeniedError) as ws_escape_err:
            pm1.dispatch_protected_action(
                PolicyRequest(
                    auth_context=worker_ctx,
                    action="files.read",
                    resource="workspace:../secret.env",
                    task_id=task_id,
                    workspace_root=str(ws_dir),
                    target_path="../secret.env",
                    timestamp=t0 + timedelta(seconds=1),
                ),
                lambda: "must_never_run",
            )
        assert ws_escape_err.value.decision.reason_code == "PATH_TRAVERSAL_DENIED"

        # 2) Master assigns worker -> ASSIGNED.
        _, assign_res = pm1.dispatch_protected_action(
            PolicyRequest(
                auth_context=master_ctx,
                action="agent.assign",
                resource=f"task:{task_id}",
                task_id=task_id,
                timestamp=t0 + timedelta(seconds=2),
            ),
            lambda: svc1.apply_task_transition(
                task_id,
                "ASSIGNED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=2),
                    reason="Assign backend worker",
                    assigned_agent_ids=["dev.backend"],
                    correlation_id="corr-gate-c-01",
                ),
            ),
        )
        assert assign_res.accepted is True

        # 3) Idempotent Run submission & owned subprocess execution.
        run_id = new_run_id()
        idem_res = master_ctrl.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="gate_c_exec",
                idempotency_key="gate-c-run-01",
                task_id=task_id,
                run=Run(
                    run_id=run_id,
                    task_id=task_id,
                    runtime_id="custom.master_worker_v1",
                    invocation_id="inv-gate-c-01",
                    correlation_id="corr-gate-c-01",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                    status="PENDING",
                ),
                payload={"artifact_rel": artifact_rel},
                submitted_at=t0 + timedelta(seconds=3),
                actor_id="core.master",
            )
        )
        assert idem_res.status == "ACCEPTED_NEW" and idem_res.run_id == run_id

        start_res = svc1.apply_paired_transition(
            "start",
            task_id,
            run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=4),
                reason="Launch owned worker subprocess",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_id,
                    reason="Start owned Python deliverable generator",
                ),
                correlation_id="corr-gate-c-01",
            ),
        )
        assert start_res.accepted is True

        out_file = ws_dir / artifact_rel
        writer_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"p = Path(r'{out_file}'); "
                    "p.parent.mkdir(parents=True, exist_ok=True); "
                    "p.write_bytes(b'AI Village Phase 04 Gate C verified deliverable\\n')"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        owned_handle = OwnedSubprocessHandle(
            writer_proc,
            execution_id="exec-gate-c-01",
            owner_token="owner-gate-c-01",
        )
        master_ctrl.register_owned_execution(
            task_id,
            run_id,
            owned_handle,
            timestamp=t0 + timedelta(seconds=4),
        )
        wait_outcome = owned_handle.wait_for_exit(timeout_seconds=10.0)
        assert wait_outcome.confirmed_stopped is True
        assert wait_outcome.terminal_status == "SUCCEEDED"
        assert wait_outcome.exit_code == 0

        # Persist Session + Checklist item referencing QA evidence target later.
        store1.sessions.save_session(
            Session(
                session_id=session_id,
                agent_id="dev.backend",
                task_id=task_id,
                run_id=run_id,
                invocation_id="inv-gate-c-01",
                provider_id="antigravity",
                connection_id="default_antigravity",
                provider_conversation_id="agy-conv-gate-c-01",
                workspace_reference=str(ws_dir),
                status="CLOSED",
                started_at=t0 + timedelta(seconds=4),
                finished_at=t0 + timedelta(seconds=5),
            )
        )

        # 4) Checkpoint pause at Approval Gate: WAITING_APPROVAL before cleanup/delivery.
        pause_res = svc1.apply_task_transition(
            task_id,
            "WAITING_APPROVAL",
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=6),
                reason="Awaiting human approval for sensitive temp cleanup before QA",
                correlation_id="corr-gate-c-01",
            ),
        )
        assert pause_res.accepted is True
        assert pause_res.task is not None and pause_res.task.status == "WAITING_APPROVAL"

        cleanup_res_name = f"task:{task_id}/scratch/old_cache"
        cleanup_params = {"confirm_purge": True}
        fp = compute_request_fingerprint(
            action="trash.purge",
            resource=cleanup_res_name,
            task_id=task_id,
            run_id=run_id,
            parameters=cleanup_params,
        )
        approval_req = approvals.create_approval_request(
            requester=master_ctx,
            action="trash.purge",
            target_resource=cleanup_res_name,
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=fp,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions=evaluator.policy_snapshot(["gov.require_approval_sensitive_admin_ops"]),
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Approve scratch cache purge prior to final QA verification",
            requested_at=t0 + timedelta(seconds=6),
        )
        with pytest.raises(ApprovalError) as worker_apr_err:
            approvals.decide_approval(
                approval_req.approval_id,
                decider=worker_ctx,
                approved=True,
                reason="Worker cannot self-approve",
                timestamp=t0 + timedelta(seconds=7),
            )
        assert worker_apr_err.value.error_code == "UNAUTHORIZED_APPROVER"

        approvals.decide_approval(
            approval_req.approval_id,
            decider=operator_ctx,
            approved=True,
            reason="Operator approved Gate C checkpoint",
            timestamp=t0 + timedelta(seconds=8),
        )

    # Simulated Restart mid-workflow while WAITING_APPROVAL!
    with SQLiteRepositoryStore(db_path) as store2:
        svc2 = PersistentWorkflowService(store2)
        approvals2 = ApprovalService(store2.approvals, store2.events)
        pm2 = PermissionManager(
            evaluator=PolicyEvaluator(registered_tools=["tools.filesystem.read_file"]),
            journal=store2.events,
            approval_service=approvals2,
        )
        ctrl2 = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store2))

        mid_report = recover_task(store2, task_id)
        assert mid_report.snapshot_task is not None
        assert mid_report.snapshot_task.status == "WAITING_APPROVAL"
        assert mid_report.snapshot_matches_replay is True

        # Consume single-use Approval once and resume WAITING_APPROVAL -> RUNNING.
        active_run = store2.runs.get_run(run_id)
        assert active_run is not None
        cleanup_runs: list[str] = []
        _, resume_res = pm2.dispatch_protected_action(
            PolicyRequest(
                auth_context=operator_ctx,
                action="trash.purge",
                resource=cleanup_res_name,
                task_id=task_id,
                run_id=run_id,
                approval_id=approval_req.approval_id,
                parameters=cleanup_params,
                timestamp=t0 + timedelta(seconds=9),
            ),
            lambda: (
                cleanup_runs.append("purged"),
                svc2.apply_task_transition(
                    task_id,
                    "RUNNING",
                    TransitionContext(
                        actor_id="operator.alice",
                        actor_kind="operator",
                        timestamp=t0 + timedelta(seconds=9),
                        run=active_run,
                        reason="Human operator approved checkpoint; resume Task",
                        execution_evidence=ExecutionEvidence(
                            source="process_manager",
                            run_id=run_id,
                            reason="Human operator approved checkpoint; resume active Run",
                        ),
                        correlation_id="corr-gate-c-01",
                    ),
                ),
            )[1],
        )
        assert resume_res.accepted is True
        assert cleanup_runs == ["purged"]

        # Reuse of approval_id fails closed.
        with pytest.raises(ApprovalError) as replay_apr:
            pm2.dispatch_protected_action(
                PolicyRequest(
                    auth_context=operator_ctx,
                    action="trash.purge",
                    resource=cleanup_res_name,
                    task_id=task_id,
                    run_id=run_id,
                    approval_id=approval_req.approval_id,
                    parameters=cleanup_params,
                    timestamp=t0 + timedelta(seconds=10),
                ),
                lambda: cleanup_runs.append("duplicate"),
            )
        assert replay_apr.value.error_code == "APPROVAL_ALREADY_CONSUMED"

        # 5) Independent QA verifies disk artifact SHA-256 -> Run SUCCEEDED -> Task COMPLETED.
        expected_disk_text = "AI Village Phase 04 Gate C verified deliverable\n"
        assert out_file.read_text(encoding="utf-8") == expected_disk_text
        engine_res = EngineResult(
            request_id="req-gate-c-01",
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id="default_antigravity",
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            observed_tool_names=["owned_subprocess"],
            started_at=t0 + timedelta(seconds=4),
            finished_at=t0 + timedelta(seconds=5),
            duration_ms=1000,
            telemetry=EngineTelemetry(
                duration_ms=1000,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        )
        qa_disk = verify_engine_artifact(
            workspace_dir=ws_dir,
            artifact_relpath=artifact_rel,
            engine_result=engine_res,
            expected_content=expected_disk_text,
        )
        assert qa_disk.status == "VERIFIED_PASS"
        assert qa_disk.actual_sha256 is not None

        store2.metadata.save_checklist_item(
            ChecklistItem(
                checklist_item_id=checklist_id,
                task_id=task_id,
                title="Produce verified deliverable file",
                assigned_agent_id="dev.backend",
                status="COMPLETED",
                order=0,
                evidence_reference=evidence_id,
                created_at=t0 + timedelta(seconds=2),
                completed_at=t0 + timedelta(seconds=11),
            )
        )
        store2.metadata.save_artifact(
            Artifact(
                artifact_id=artifact_id,
                task_id=task_id,
                run_id=run_id,
                owner_agent_id="dev.backend",
                relative_path=artifact_rel,
                content_type="text/plain",
                size_bytes=out_file.stat().st_size,
                sha256=qa_disk.actual_sha256,
                version="1.0.0",
                verification_status="VERIFIED_PASS",
                evidence_reference=evidence_id,
                created_at=t0 + timedelta(seconds=11),
            )
        )

        ok_run, _, run_done, _, err_msg = ctrl2.complete_owned_run(
            task_id,
            run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=11),
                reason="Subprocess deliverable ready with honest unavailable telemetry",
                execution_evidence=ExecutionEvidence(
                    source="process_manager",
                    run_id=run_id,
                    reason="Owned process exited 0",
                    telemetry=RunTelemetryRef(
                        total_duration_ms=1000,
                        input_tokens=None,
                        output_tokens=None,
                        telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                        telemetry_source="unavailable",
                    ),
                ),
            ),
        )
        assert ok_run is True, err_msg
        assert run_done is not None and run_done.status == "SUCCEEDED"

        store2.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=12),
                event_type="qa.verification_recorded",
                source="qa_verifier",
                task_id=task_id,
                run_id=run_id,
                actor_id="qa.verifier",
                artifact_id=artifact_id,
                evidence_id=evidence_id,
                redacted_payload={"verification_status": "VERIFIED_PASS", "sha256": qa_disk.actual_sha256},
            )
        )
        qa_ev = qa_evidence_from_engine_verification(
            qa_disk,
            evidence_id=evidence_id,
            verifier_actor_id="qa.verifier",
            verified_at=t0 + timedelta(seconds=12),
            artifact_ids=[artifact_id],
        )
        complete_task = svc2.apply_task_transition(
            task_id,
            "COMPLETED",
            TransitionContext(
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                timestamp=t0 + timedelta(seconds=12),
                reason="Independent QA verified deliverable SHA-256",
                qa_evidence=qa_ev,
                checklist_items=store2.metadata.list_checklist_items(task_id),
                artifacts=store2.metadata.list_artifacts(task_id=task_id),
                correlation_id="corr-gate-c-01",
            ),
        )
        assert complete_task.accepted is True
        assert complete_task.task is not None and complete_task.task.status == "COMPLETED"

        store2.metadata.save_notification(
            NotificationRef(
                notification_id=new_notification_id(),
                task_id=task_id,
                run_id=run_id,
                event_id=complete_task.entries[0].event.event_id,
                severity="INFO",
                notification_type="QA_RESULT",
                created_at=t0 + timedelta(seconds=12),
            )
        )

    # 6) Second Restart: verify replay, timeline, governance audit, telemetry=None, and CONSUMED approval.
    with SQLiteRepositoryStore(db_path) as store3:
        final_report = recover_task(store3, task_id)
        assert final_report.recovery_status == "VERIFIED_CLEAN"
        assert final_report.snapshot_matches_replay is True
        assert final_report.snapshot_task is not None and final_report.snapshot_task.status == "COMPLETED"
        assert final_report.runs[0].status == "SUCCEEDED"
        assert final_report.runs[0].telemetry is not None and final_report.runs[0].telemetry.input_tokens is None
        assert final_report.artifacts[0].verification_status == "VERIFIED_PASS"
        assert final_report.checklist_items[0].status == "COMPLETED"
        assert store3.approvals.get_approval(approval_req.approval_id).status == "CONSUMED"  # type: ignore[union-attr]

        entries = store3.events.read_entries(task_id=task_id)
        replay_res = replay_task_history(
            entries, task_id=task_id, baseline_task=final_report.baseline_task
        )
        assert replay_res.reconstructed_task is not None
        assert replay_res.reconstructed_task.status == "COMPLETED"
        timeline = project_activity_timeline(store3.events, task_id=task_id)
        event_types = {item.event_type for item in timeline.entries}
        assert {
            "task.created",
            "approval.requested",
            "approval.resolved",
            "governance.decision_recorded",
            "qa.verification_recorded",
        }.issubset(event_types)


def test_gate_c_02_owned_subprocess_cancellation_and_sibling_isolation(tmp_path: Path) -> None:
    """Cancel owned child process cleanly with CANCEL_CONFIRMED while leaving sibling alive."""
    db_path = tmp_path / "gate_c_cancel.db"
    _init_db(db_path)
    t0 = _utc(16, 0, 0)

    owned_proc = _spawn_sleeper_subprocess(30)
    sibling_proc = _spawn_sleeper_subprocess(30)
    try:
        with SQLiteRepositoryStore(db_path) as store:
            svc = PersistentWorkflowService(store)
            pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events)
            worker_ctx = TrustedAuthContext(
                actor_id="dev.backend",
                actor_kind="worker_agent",
                auth_source="application_boundary",
                authenticated=True,
            )
            master_ctx = TrustedAuthContext(
                actor_id="core.master",
                actor_kind="master_agent",
                auth_source="application_boundary",
                authenticated=True,
            )

            task = Task(
                task_id=new_task_id(),
                title="Cancel isolation verification",
                description="Verify owned subprocess cancellation does not disturb sibling PIDs.",
                requested_by="operator.alice",
                status="QUEUED",
                created_at=t0,
            )
            svc.create_task(task)
            svc.apply_task_transition(
                task.task_id,
                "ASSIGNED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=1),
                    reason="Assign worker",
                    assigned_agent_ids=["dev.backend"],
                ),
            )
            run = Run(
                run_id=new_run_id(),
                task_id=task.task_id,
                runtime_id="custom.master_worker_v1",
                status="PENDING",
            )
            svc.create_run(run)
            svc.apply_paired_transition(
                "start",
                task.task_id,
                run.run_id,
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="core_runtime",
                    timestamp=t0 + timedelta(seconds=2),
                    reason="Start owned child",
                    execution_evidence=ExecutionEvidence(
                        source="unit_test_fixture",
                        run_id=run.run_id,
                        reason="Start child for cancel check",
                    ),
                ),
            )

            handle = OwnedSubprocessHandle(
                owned_proc,
                execution_id="exec-cancel-01",
                owner_token="owner-cancel-01",
            )
            adapter = SQLiteLifecyclePersistenceAdapter(store)
            worker_ctrl = WorkflowLifecycleController(
                adapter, permission_hook=pm.build_lifecycle_hook(worker_ctx)
            )
            master_ctrl = WorkflowLifecycleController(
                adapter, permission_hook=pm.build_lifecycle_hook(master_ctx)
            )
            master_ctrl.register_owned_execution(
                task.task_id, run.run_id, handle, timestamp=t0 + timedelta(seconds=2)
            )

            # Worker cannot cancel.
            denied = worker_ctrl.request_cancel(
                task.task_id,
                run.run_id,
                actor_id="dev.backend",
                actor_kind="operator",
                timestamp=t0 + timedelta(seconds=3),
                reason="Unauthorized cancel attempt",
            )
            assert denied.status == "PERMISSION_DENIED"
            assert is_pid_alive(owned_proc.pid) is True

            # Wrong owner_token rejected without killing process.
            wrong_owner = master_ctrl.request_cancel(
                task.task_id,
                run.run_id,
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=4),
                reason="Wrong token",
                owner_token="wrong-token",
            )
            assert wrong_owner.status == "OWNERSHIP_REJECTED"
            assert is_pid_alive(owned_proc.pid) is True

            # Valid cancel stops owned child and leaves sibling untouched.
            cancel_res = master_ctrl.request_cancel(
                task.task_id,
                run.run_id,
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=5),
                reason="Operator requested cancellation",
                owner_token="owner-cancel-01",
            )
            assert cancel_res.status == "CANCEL_CONFIRMED"
            assert cancel_res.cancel_confirmed is True
            assert cancel_res.run_status == "CANCELLED"
            assert cancel_res.task_status == "CANCELLED"
            assert is_pid_alive(owned_proc.pid) is False
            assert is_pid_alive(sibling_proc.pid) is True
            assert sibling_proc.poll() is None
    finally:
        for proc in (owned_proc, sibling_proc):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def test_gate_c_03_timeout_stops_owned_child_and_commits_timed_out(tmp_path: Path) -> None:
    """Confirm handle_timeout honors deadline and kills only owned process before Run.TIMED_OUT."""
    db_path = tmp_path / "gate_c_timeout.db"
    _init_db(db_path)
    t0 = _utc(17, 0, 0)

    proc = _spawn_sleeper_subprocess(30)
    try:
        with SQLiteRepositoryStore(db_path) as store:
            svc = PersistentWorkflowService(store)
            task = Task(
                task_id=new_task_id(),
                title="Timeout enforcement verification",
                description="Verify timeout waits until deadline then terminates child cleanly.",
                requested_by="operator.alice",
                status="QUEUED",
                created_at=t0,
            )
            svc.create_task(task)
            svc.apply_task_transition(
                task.task_id,
                "ASSIGNED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=1),
                    reason="Assign worker",
                    assigned_agent_ids=["dev.backend"],
                ),
            )
            run = Run(
                run_id=new_run_id(),
                task_id=task.task_id,
                runtime_id="custom.master_worker_v1",
                status="PENDING",
            )
            svc.create_run(run)
            svc.apply_paired_transition(
                "start",
                task.task_id,
                run.run_id,
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="core_runtime",
                    timestamp=t0 + timedelta(seconds=2),
                    reason="Start timed run",
                    execution_evidence=ExecutionEvidence(
                        source="unit_test_fixture",
                        run_id=run.run_id,
                        reason="Start child for timeout check",
                    ),
                ),
            )

            ctrl = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store))
            handle = OwnedSubprocessHandle(
                proc, execution_id="exec-timeout-01", owner_token="owner-timeout-01"
            )
            ctrl.register_owned_execution(
                task.task_id,
                run.run_id,
                handle,
                timestamp=t0 + timedelta(seconds=2),
                timeout_deadline_at=t0 + timedelta(seconds=10),
            )

            early = ctrl.handle_timeout(
                task.task_id,
                run.run_id,
                actor_id="core.master",
                timestamp=t0 + timedelta(seconds=5),
            )
            assert early.status == "NOT_ELAPSED"
            assert early.timed_out_confirmed is False
            assert is_pid_alive(proc.pid) is True

            timed_out = ctrl.handle_timeout(
                task.task_id,
                run.run_id,
                actor_id="core.master",
                timestamp=t0 + timedelta(seconds=12),
                reason="Exceeded 10s SLA",
            )
            assert timed_out.status == "TIMED_OUT_CONFIRMED"
            assert timed_out.timed_out_confirmed is True
            assert timed_out.run_status == "TIMED_OUT"
            assert timed_out.task_status == "FAILED"
            assert is_pid_alive(proc.pid) is False
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_gate_c_04_idempotent_retry_conflict_and_interrupted_restart_reconciliation(
    tmp_path: Path,
) -> None:
    """Verify idempotent submission replay/conflict and unobservable RUNNING reconciliation after restart."""
    db_path = tmp_path / "gate_c_reconcile.db"
    _init_db(db_path)
    t0 = _utc(18, 0, 0)

    with SQLiteRepositoryStore(db_path) as store1:
        svc = PersistentWorkflowService(store1)
        task = Task(
            task_id=new_task_id(),
            title="Restart reconciliation verification",
            description="Ensure interrupted RUNNING task is flagged NEEDS_RECONCILIATION, never fabricated.",
            requested_by="operator.alice",
            status="QUEUED",
            created_at=t0,
        )
        svc.create_task(task)
        svc.apply_task_transition(
            task.task_id,
            "ASSIGNED",
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=1),
                reason="Assign worker",
                assigned_agent_ids=["dev.backend"],
            ),
        )
        ctrl1 = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store1))
        run_id = new_run_id()
        first = ctrl1.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="gate_c_reconcile",
                idempotency_key="idem-reconcile-01",
                task_id=task.task_id,
                run=Run(
                    run_id=run_id,
                    task_id=task.task_id,
                    runtime_id="custom.master_worker_v1",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                payload={"step": "analyze"},
                submitted_at=t0 + timedelta(seconds=2),
            )
        )
        assert first.status == "ACCEPTED_NEW"

        svc.apply_paired_transition(
            "start",
            task.task_id,
            run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=3),
                reason="Start execution before simulated crash",
                execution_evidence=ExecutionEvidence(
                    source="process_manager",
                    run_id=run_id,
                    reason="Started execution",
                ),
            ),
        )
        store1.lifecycle.register_execution(
            execution_id="exec-lost-01",
            task_id=task.task_id,
            run_id=run_id,
            owner_token="owner-lost-01",
            pid=999999,
            lifecycle_state="RUNNING",
            timeout_deadline_at=None,
            timestamp=t0 + timedelta(seconds=3),
        )

    # Fresh store after simulated restart: in-memory handles are gone.
    with SQLiteRepositoryStore(db_path) as store2:
        ctrl2 = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store2))

        replay_res = ctrl2.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="gate_c_reconcile",
                idempotency_key="idem-reconcile-01",
                task_id=task.task_id,
                run=Run(
                    run_id=new_run_id(),
                    task_id=task.task_id,
                    runtime_id="custom.master_worker_v1",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                payload={"step": "analyze"},
                submitted_at=t0 + timedelta(seconds=10),
            )
        )
        assert replay_res.status == "IDEMPOTENT_REPLAY"
        assert replay_res.run_id == run_id

        conflict_res = ctrl2.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="gate_c_reconcile",
                idempotency_key="idem-reconcile-01",
                task_id=task.task_id,
                run=Run(
                    run_id=new_run_id(),
                    task_id=task.task_id,
                    runtime_id="custom.master_worker_v1",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                payload={"step": "mutated"},
                submitted_at=t0 + timedelta(seconds=11),
            )
        )
        assert conflict_res.status == "CONFLICT_REJECTED"

        reconciled = ctrl2.reconcile_interrupted_task(
            task.task_id, timestamp=t0 + timedelta(seconds=12)
        )
        assert reconciled.reconciled_status == "NEEDS_RECONCILIATION"
        assert reconciled.reconciliation_required is True
        assert reconciled.process_observable is False
        assert run_id in reconciled.interrupted_run_ids
        assert store2.tasks.get_task(task.task_id).status == "RUNNING"  # type: ignore[union-attr]
        assert store2.runs.get_run(run_id).status == "RUNNING"  # type: ignore[union-attr]
        execs = store2.lifecycle.list_executions_for_run(run_id)
        assert execs[0].lifecycle_state == "UNOBSERVABLE"
