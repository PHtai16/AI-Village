"""Phase 11 Gate G End-to-End Offline Acceptance Tests (P11.01 -> P11.05 -> Process -> Restart & Replay)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from core.contracts import (
    Run,
    Task,
    new_run_id,
    new_task_id,
)
from core.departments import (
    ConversationCreateRequest,
    InterruptRoutingService,
    InterruptSubmitRequest,
    MasterIntakeAckRequest,
    ProcessDepartmentService,
    ReceptionDepartmentService,
    RecordResourceUsageRequest,
    RegisterBudgetPolicyRequest,
    ResourceLedgerQuery,
    ResourcesDepartmentService,
    UserRequestIntake,
    VaultDepartmentService,
    VaultReuseRequest,
    VaultSubmissionRequest,
)
from core.governance import PermissionManager, PolicyEvaluator
from core.optimization import (
    EnqueueTaskRequest,
    ExecutionReleaseRequest,
    ExecutionSchedulerService,
    ExecutionStartConfirmRequest,
    PostTaskCleanupRequest,
    PostTaskCleanupService,
    RegisterQuotaPolicyRequest,
    TempCleanupTarget,
)
from core.tasks import ExecutionEvidence, QaVerificationEvidence, TransitionContext
from storage.repositories import (
    ManagedTempCleanupExecutor,
    SQLiteRepositoryStore,
    recover_task,
)
from tests.departments.conftest import (
    make_department_harness,
    register_worker_agent,
    utc,
    worker_ctx,
)


def test_gate_g_end_to_end_concurrent_tasks_interrupts_scheduler_ledger_cleanup_and_recovery(
    tmp_path: Path,
) -> None:
    """Gate G: verify Task B intake during Task A execution, priority scheduling, interrupts, ledger, cleanup, Process & restart replay."""
    h = make_department_harness(tmp_path)
    t0 = utc(13, 0)
    db_path = h.db_path
    temp_root = tmp_path / "managed_temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    outside_important = tmp_path / "outside_precious.md"
    outside_important.write_text("# Must never be deleted\n", encoding="utf-8")

    try:
        register_worker_agent(
            h,
            agent_id="it.coder",
            department="it",
            skills=["file_writing"],
            tools_allowed=[],
            timestamp=t0,
        )

        reception = ReceptionDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            notification_store=h.store.metadata,
            intake_repository=h.store.reception,
        )
        interrupts = InterruptRoutingService(
            permission_manager=h.permissions,
            journal=h.store.events,
            interrupt_repository=h.store.interrupts,
        )
        scheduler = ExecutionSchedulerService(
            permission_manager=h.permissions,
            scheduler_repository=h.store.scheduler,
        )
        resources = ResourcesDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            metadata_repo=h.store.tasks,
            ledger_repository=h.store.resources,
        )
        process = ProcessDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            retrospective_repo=h.store.process,
        )
        cleanup = PostTaskCleanupService(
            permission_manager=h.permissions,
            cleanup_repository=h.store.cleanup,
            temp_executor=ManagedTempCleanupExecutor(temp_root),
            retrospective_compiler=process,
        )
        vault = VaultDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            artifact_vault=h.artifact_vault,
            knowledge_vault=h.evidence_vault,
        )

        # Configure shared quota bucket across two connections (`conn_a`, `conn_b`) in the same project
        # with max_concurrency=1 so Task B must respect active capacity while Task A runs.
        for cid in ("conn_a", "conn_b"):
            pol = scheduler.register_quota_policy(
                RegisterQuotaPolicyRequest(
                    provider_id="google_gemini",
                    connection_id=cid,
                    project_ref="proj_gate_g",
                    quota_scope_kind="PROJECT_SHARED",
                    independent_quota_proof=False,
                    max_concurrency=1,
                    quota_limit_units=100,
                    timestamp=t0,
                ),
                auth_context=h.alice,
            )
            assert pol.quota_bucket_key == "project:google_gemini:proj_gate_g"

        # Register budget & quota warning/critical policy on shared bucket.
        resources.register_budget_policy(
            RegisterBudgetPolicyRequest(
                scope_kind="QUOTA_BUCKET",
                scope_ref="project:google_gemini:proj_gate_g",
                provider_id="google_gemini",
                project_ref="proj_gate_g",
                quota_scope_kind="PROJECT_SHARED",
                independent_quota_proof=False,
                budget_cost_usd_limit=2.00,
                quota_units_limit=100,
                warning_ratio=0.80,
                critical_ratio=1.00,
                policy_note="Shared Gate G project budget and quota policy.",
                timestamp=t0,
            ),
            auth_context=h.alice,
        )

        # --- G1: Task A intake -> schedule -> reserve -> RUNNING ---
        conv_a = reception.open_conversation(
            ConversationCreateRequest(title="Gate G Stream A", timestamp=t0),
            auth_context=h.alice,
        ).conversation_id
        in_a = reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_a,
                title="Generate Release Package A",
                summary="Build release package and verify SHA digest.",
                priority="NORMAL",
                acceptance_criteria=["Release package verified by independent QA"],
                idempotency_key="gate-g-intake-a",
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h.alice,
        )
        assert in_a.classification == "QUEUED" and in_a.created_task is not None
        task_a = in_a.created_task.task_id
        snap_a_initial = h.store.tasks.get_stored_task(task_a)
        assert snap_a_initial is not None

        run_a = new_run_id()
        h.workflows.create_run(
            Run(
                run_id=run_a,
                task_id=task_a,
                runtime_id="antigravity",
                invocation_id="inv-gate-g-a",
                correlation_id="corr-gate-g-a",
                provider_id="google_gemini",
                connection_id="conn_a",
                model_id="gemini-2.5-pro",
                status="PENDING",
            )
        )
        q_a = scheduler.enqueue_task(
            EnqueueTaskRequest(
                task_id=task_a,
                run_id=run_a,
                provider_id="google_gemini",
                connection_id="conn_a",
                model_id="gemini-2.5-pro",
                priority="NORMAL",
                requested_quota_units=20,
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        assert q_a.status == "READY"
        res_a = scheduler.reserve_next_execution(
            worker_id="it.coder",
            now=t0 + timedelta(seconds=3),
            auth_context=h.master,
        )
        assert res_a.lease is not None
        lease_a = res_a.lease
        assign_a = h.workflows.apply_task_transition(
            task_a,
            "ASSIGNED",
            context=TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=3, milliseconds=500),
                reason="Assign Task A to it.coder under lease.",
                assigned_agent_ids=["it.coder"],
            ),
        )
        assert assign_a.accepted is True
        pair_a = h.workflows.apply_paired_transition(
            "start",
            task_a,
            run_a,
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=4),
                reason="Start Task A under scheduler lease.",
                assigned_agent_ids=["it.coder"],
                execution_evidence=ExecutionEvidence(
                    source="core_runtime",
                    run_id=run_a,
                    reason="Lease granted.",
                ),
            ),
        )
        assert pair_a.accepted is True
        started_a = scheduler.confirm_execution_started(
            ExecutionStartConfirmRequest(
                lease_id=lease_a.lease_id,
                owner_token=lease_a.owner_token,
                run_id=run_a,
                expected_record_version=lease_a.record_version,
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=h.master,
        )
        assert started_a.state == "ACTIVE"
        snap_a_running = h.store.tasks.get_stored_task(task_a)
        assert snap_a_running is not None and snap_a_running.task.status == "RUNNING"

        # --- Mid-run Task B arrives in separate conversation -> Reception -> Master priority -> queued ---
        conv_b = reception.open_conversation(
            ConversationCreateRequest(title="Gate G Stream B", timestamp=t0 + timedelta(seconds=6)),
            auth_context=h.alice,
        ).conversation_id
        in_b = reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_b,
                title="Urgent Security Hotfix B",
                summary="Audit export filters concurrently without stopping Task A.",
                priority="HIGH",
                acceptance_criteria=["Security filter audit exported cleanly"],
                idempotency_key="gate-g-intake-b",
                timestamp=t0 + timedelta(seconds=7),
            ),
            auth_context=h.alice,
        )
        assert in_b.classification == "QUEUED" and in_b.created_task is not None
        task_b = in_b.created_task.task_id
        assert task_b != task_a

        ack_b = reception.acknowledge_intake_by_master(
            MasterIntakeAckRequest(
                intake_id=in_b.intake_id,
                expected_record_version=1,
                decision_note="Master prioritized Task B behind active Task A without overwriting A.",
                timestamp=t0 + timedelta(seconds=8),
            ),
            auth_context=h.master,
        )
        assert ack_b.master_handoff_status == "ACKNOWLEDGED_BY_MASTER"

        # Task A snapshot, baseline & RUNNING status remain 100% untouched by Task B intake!
        snap_a_check = h.store.tasks.get_stored_task(task_a)
        assert snap_a_check is not None
        assert snap_a_check.task == snap_a_running.task
        assert snap_a_check.version == snap_a_running.version
        assert snap_a_check.baseline == snap_a_initial.baseline
        assert snap_a_check.task.active_run_id == run_a

        run_b = new_run_id()
        h.workflows.create_run(
            Run(
                run_id=run_b,
                task_id=task_b,
                runtime_id="antigravity",
                invocation_id="inv-gate-g-b",
                correlation_id="corr-gate-g-b",
                provider_id="google_gemini",
                connection_id="conn_b",
                model_id="gemini-2.5-flash",
                status="PENDING",
            )
        )
        # Task B uses SECOND connection `conn_b` in same shared bucket -> blocked on concurrency 1/1!
        scheduler.enqueue_task(
            EnqueueTaskRequest(
                task_id=task_b,
                run_id=run_b,
                provider_id="google_gemini",
                connection_id="conn_b",
                model_id="gemini-2.5-flash",
                priority="HIGH",
                requested_quota_units=20,
                timestamp=t0 + timedelta(seconds=9),
            ),
            auth_context=h.master,
        )
        res_blocked_b = scheduler.reserve_next_execution(
            worker_id="it.coder",
            now=t0 + timedelta(seconds=10),
            auth_context=h.master,
        )
        assert res_blocked_b.decision.outcome == "BLOCKED_BY_CONCURRENCY"
        assert res_blocked_b.lease is None

        # --- G2: Interrupt classification + dedup + Resource Ledger warnings/critical ---
        sub_i1 = interrupts.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Elevated latency on external webhook during Task A.",
                task_id=task_a,
                run_id=run_a,
                target_department="it",
                dedup_scope=f"task:{task_a}",
                incident_key="webhook-latency",
                event_dedup_key="evt-lat-1",
                timestamp=t0 + timedelta(seconds=11),
            ),
            auth_context=h.master,
        )
        sub_i2 = interrupts.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Elevated latency on external webhook during Task A (repeated).",
                task_id=task_a,
                run_id=run_a,
                target_department="it",
                dedup_scope=f"task:{task_a}",
                incident_key="webhook-latency",
                event_dedup_key="evt-lat-2",
                timestamp=t0 + timedelta(seconds=12),
            ),
            auth_context=h.master,
        )
        assert sub_i1.incident.incident_id == sub_i2.incident.incident_id
        assert sub_i2.incident.occurrence_count == 2
        assert sub_i2.notification_suppressed is True
        assert sub_i1.incident.broadcast_all_departments is False

        # Record Step 1 (provider unavailable tokens/cost) and Step 2 (measured + estimated -> CRITICAL) on Task A!
        resources.record_usage(
            RecordResourceUsageRequest(
                task_id=task_a,
                run_id=run_a,
                agent_id="it.coder",
                provider_id="google_gemini",
                connection_id="conn_a",
                model_id="gemini-2.5-pro",
                project_ref="proj_gate_g",
                quota_scope_kind="PROJECT_SHARED",
                duration_ms=400,
                duration_source="HOST_SAMPLED",
                memory_mb=210.0,
                peak_memory_mb=240.0,
                memory_source="HOST_SAMPLED",
                token_usage_status="UNAVAILABLE_FROM_PROVIDER",
                cost_source="UNAVAILABLE_FROM_PROVIDER",
                quota_units_used=45,
                timestamp=t0 + timedelta(seconds=13),
            ),
            auth_context=h.master,
        )
        warn_out = resources.record_usage(
            RecordResourceUsageRequest(
                task_id=task_a,
                run_id=run_a,
                agent_id="it.coder",
                provider_id="google_gemini",
                connection_id="conn_b",
                model_id="gemini-2.5-pro",
                project_ref="proj_gate_g",
                quota_scope_kind="PROJECT_SHARED",
                duration_ms=500,
                duration_source="PROVIDER_MEASURED",
                total_tokens=2000,
                token_usage_status="ESTIMATED",
                estimated_cost_usd=1.70,
                cost_source="ESTIMATED_LOCAL_PRICING_TABLE",
                pricing_table_ref="gemini-2026-11",
                is_estimated=True,
                estimate_label="ESTIMATED local pricing gemini-2026-11",
                quota_units_used=40,
                timestamp=t0 + timedelta(seconds=14),
            ),
            auth_context=h.master,
        )
        # Shared bucket crossed 85% budget ($1.70/$2.00) and 85% quota (85/100)!
        assert {a.alert.alert_category for a in warn_out.triggered_alerts} >= {"BUDGET_COST", "QUOTA"}
        assert all(a.alert.severity == "WARNING" for a in warn_out.triggered_alerts)

        crit_out = resources.record_usage(
            RecordResourceUsageRequest(
                task_id=task_a,
                run_id=run_a,
                agent_id="it.coder",
                provider_id="google_gemini",
                connection_id="conn_a",
                model_id="gemini-2.5-pro",
                project_ref="proj_gate_g",
                quota_scope_kind="PROJECT_SHARED",
                duration_ms=300,
                duration_source="PROVIDER_MEASURED",
                total_tokens=1000,
                token_usage_status="VERIFIED_MEASURED",
                actual_cost_usd=0.45,
                cost_source="PROVIDER_BILLED",
                quota_units_used=20,
                timestamp=t0 + timedelta(seconds=15),
            ),
            auth_context=h.master,
        )
        assert any(a.escalated_to_critical and a.alert.master_notified for a in crit_out.triggered_alerts)
        # Task A must NOT be cancelled by resource alerts!
        assert h.store.tasks.get_task(task_a).status == "RUNNING"  # type: ignore[union-attr]

        # --- G3: Task A completes cleanly with Vault artifact + QA -> releases lease -> Task B starts -> Safe Cleanup + Process ---
        coder_a = worker_ctx("it.coder", assigned_task_ids=[task_a, task_b])
        evd_a = h.evidence_vault.record_evidence(
            auth_context=h.qa_verifier,
            evidence_kind="qa_log",
            task_id=task_a,
            run_id=run_a,
            summary="Independent QA verified Release Package A output.",
            timestamp=t0 + timedelta(seconds=16),
        )
        art_a = vault.submit_document(
            VaultSubmissionRequest(
                task_id=task_a,
                run_id=run_a,
                owner_agent_id="it.coder",
                logical_key="release_pkg_a",
                relative_path="outputs/release_a.txt",
                content_bytes=b"Release A OK\n",
                verification_status="VERIFIED_PASS",
                evidence_reference=evd_a.evidence_id,
                source_evidence_id=evd_a.evidence_id,
                department="it",
                access_scope="project",
                timestamp=t0 + timedelta(seconds=17),
            ),
            auth_context=coder_a,
        )
        # Task B references Task A's deliverable -> protects shared artifact!
        vault.reuse_artifact_in_task(
            VaultReuseRequest(
                artifact_id=art_a.artifact.artifact_id,
                target_task_id=task_b,
                target_run_id=run_b,
                timestamp=t0 + timedelta(seconds=18),
            ),
            auth_context=coder_a,
        )

        end_r_a = h.workflows.apply_run_transition(
            run_a,
            "SUCCEEDED",
            context=TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=19),
                reason="Task A run succeeded cleanly.",
                execution_evidence=ExecutionEvidence(
                    source="core_runtime",
                    run_id=run_a,
                    evidence_id=evd_a.evidence_id,
                    reason="Verified run output.",
                ),
            ),
        )
        assert end_r_a.accepted is True and end_r_a.run is not None
        end_t_a = h.workflows.apply_task_transition(
            task_a,
            "COMPLETED",
            context=TransitionContext(
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                timestamp=t0 + timedelta(seconds=20),
                reason="Independent QA approved Task A completion.",
                run=end_r_a.run,
                qa_evidence=QaVerificationEvidence(
                    evidence_id=evd_a.evidence_id,
                    verifier_source="independent_qa",
                    verifier_actor_id="qa.verifier",
                    verification_status="VERIFIED_PASS",
                    verified_at=t0 + timedelta(seconds=16),
                    artifact_ids=[art_a.artifact.artifact_id],
                    summary="PASS.",
                ),
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_a,
                    evidence_id=evd_a.evidence_id,
                    reason="QA verified.",
                ),
            ),
        )
        assert end_t_a.accepted is True, end_t_a.error_message

        # Release Task A lease -> Scheduler now lets waiting Task B start!
        scheduler.release_execution(
            ExecutionReleaseRequest(
                lease_id=lease_a.lease_id,
                owner_token=lease_a.owner_token,
                terminal_queue_status="COMPLETED",
                reason="Task A finished cleanly.",
                timestamp=t0 + timedelta(seconds=21),
            ),
            auth_context=h.master,
        )
        res_b = scheduler.reserve_next_execution(
            worker_id="it.coder",
            now=t0 + timedelta(seconds=22),
            auth_context=h.master,
        )
        assert res_b.decision.outcome == "RESERVED" and res_b.lease is not None
        assert res_b.lease.task_id == task_b
        assign_b = h.workflows.apply_task_transition(
            task_b,
            "ASSIGNED",
            context=TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=22, milliseconds=500),
                reason="Assign Task B to it.coder after Task A frees slot.",
                assigned_agent_ids=["it.coder"],
            ),
        )
        assert assign_b.accepted is True
        pair_b = h.workflows.apply_paired_transition(
            "start",
            task_b,
            run_b,
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=23),
                reason="Task B starts cleanly once Task A yields shared quota/concurrency slot.",
                assigned_agent_ids=["it.coder"],
                execution_evidence=ExecutionEvidence(
                    source="core_runtime",
                    run_id=run_b,
                    reason="Task B lease active.",
                ),
            ),
        )
        assert pair_b.accepted is True
        started_b = scheduler.confirm_execution_started(
            ExecutionStartConfirmRequest(
                lease_id=res_b.lease.lease_id,
                owner_token=res_b.lease.owner_token,
                run_id=run_b,
                expected_record_version=res_b.lease.record_version,
                timestamp=t0 + timedelta(seconds=24),
            ),
            auth_context=h.master,
        )
        assert started_b.state == "ACTIVE"

        # Safe Post-Task Cleanup on finished Task A + blocked unsafe/shared paths + Process Handoff!
        scratch_a = temp_root / "task_a_build.tmp"
        scratch_a.write_text("scratch bytes for A", encoding="utf-8")
        shared_temp = temp_root / "shared_between_a_b.tmp"
        shared_temp.write_text("shared by B", encoding="utf-8")

        cleanup_out = cleanup.cleanup_and_handoff(
            PostTaskCleanupRequest(
                task_id=task_a,
                run_id=run_a,
                temp_targets=[
                    TempCleanupTarget(path=str(scratch_a), resource_kind="TEMP_FILE"),
                    TempCleanupTarget(path=str(outside_important), resource_kind="TEMP_FILE"),
                    TempCleanupTarget(path=str(shared_temp), resource_kind="TEMP_FILE"),
                ],
                preserve_paths=[str(shared_temp)],
                idempotency_key="gate-g-clean-a",
                deliver_to_process=True,
                timestamp=t0 + timedelta(seconds=25),
            ),
            auth_context=h.master,
        )
        assert not scratch_a.exists()
        assert outside_important.exists()
        assert shared_temp.exists()
        assert cleanup_out.cleanup.cleanup_status == "PARTIAL"
        assert cleanup_out.cleanup.inventory.deleted_temp_count == 1
        assert cleanup_out.cleanup.inventory.blocked_safety_count == 2
        assert cleanup_out.cleanup.inventory.preserved_artifact_count == 1
        assert cleanup_out.cleanup.inventory.preserved_evidence_count == 1
        assert cleanup_out.cleanup.inventory.preserved_ledger_count == 3
        assert cleanup_out.cleanup.inventory.resource_summary is not None
        assert cleanup_out.cleanup.inventory.resource_summary.actual_cost_usd_total == pytest.approx(0.45)
        assert cleanup_out.cleanup.inventory.resource_summary.estimated_cost_usd_total == pytest.approx(1.70)
        assert cleanup_out.cleanup.inventory.resource_summary.has_unavailable_cost_entries is True
        assert cleanup_out.handoff is not None
        assert cleanup_out.handoff.handoff_status == "DELIVERED_TO_PROCESS"
        assert cleanup_out.handoff.retrospective_id is not None

        retro = h.store.process.get_by_task(task_a)
        assert retro is not None and retro.retrospective_id == cleanup_out.handoff.retrospective_id
    finally:
        h.store.close()

    # --- G4: Restart Persistence + Event Journal Replay (`recover_task`) ---
    reopened = SQLiteRepositoryStore(db_path)
    try:
        pm_reopened = PermissionManager(evaluator=PolicyEvaluator(), journal=reopened.events)
        rx_reopened = ReceptionDepartmentService(
            permission_manager=pm_reopened,
            journal=reopened.events,
            notification_store=reopened.metadata,
            intake_repository=reopened.reception,
        )
        res_reopened = ResourcesDepartmentService(
            permission_manager=pm_reopened,
            journal=reopened.events,
            metadata_repo=reopened.tasks,
            ledger_repository=reopened.resources,
        )
        assert len(rx_reopened.list_conversation_turns(conv_a, auth_context=h.alice)) == 1
        assert len(rx_reopened.list_conversation_turns(conv_b, auth_context=h.alice)) == 1
        assert len(res_reopened.list_resource_ledger(ResourceLedgerQuery(task_id=task_a), auth_context=h.alice, timestamp=t0 + timedelta(seconds=30))) == 3

        rep_a = recover_task(reopened, task_a)
        assert rep_a.recovery_status == "VERIFIED_CLEAN"
        assert rep_a.snapshot_matches_replay is True
        assert rep_a.inconsistencies == []
        assert rep_a.snapshot_task is not None and rep_a.snapshot_task.status == "COMPLETED"

        # Task B was still RUNNING at crash point -> MUST report NEEDS_RECONCILIATION truthfully!
        rep_b = recover_task(reopened, task_b)
        assert rep_b.inconsistencies == [], f"Replay errors: {rep_b.inconsistencies} / {rep_b.replay_result}"
        assert rep_b.recovery_status == "NEEDS_RECONCILIATION"
        assert rep_b.snapshot_matches_replay is True
        assert any(iw.reconciliation_required for iw in rep_b.interrupted_work)
    finally:
        reopened.close()
