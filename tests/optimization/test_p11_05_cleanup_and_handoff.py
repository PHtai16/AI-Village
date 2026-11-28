"""Unit, Containment Security, Resource Inventory, Lease Release & Process Handoff tests for P11.05."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3

import pytest

from core.contracts import (
    Run,
    RunErrorRef,
    Task,
    new_run_id,
    new_task_id,
)
from core.departments import (
    ProcessDepartmentService,
    RecordResourceUsageRequest,
    RegisterBudgetPolicyRequest,
    ResourcesDepartmentService,
    VaultDepartmentService,
    VaultReuseRequest,
    VaultSubmissionRequest,
)
from core.optimization import (
    CleanupConflictError,
    CleanupGovernanceError,
    CleanupStateError,
    EnqueueTaskRequest,
    ExecutionSchedulerService,
    PostTaskCleanupRequest,
    PostTaskCleanupService,
    RegisterQuotaPolicyRequest,
    TempCleanupTarget,
)
from core.tasks import ExecutionEvidence, QaVerificationEvidence, TransitionContext
from storage.repositories import (
    ManagedTempCleanupExecutor,
    SQLiteCleanupRepository,
    SQLiteRepositoryStore,
)
from tests.departments.conftest import (
    REPO_ROOT,
    DepartmentTestHarness,
    create_task_and_run,
    make_department_harness,
    register_worker_agent,
    utc,
    worker_ctx,
)


def _complete_task(
    h: DepartmentTestHarness,
    task_id: str,
    run_id: str,
    *,
    timestamp: datetime,
    evidence_id: str | None = None,
) -> None:
    """Drive a RUNNING Task + Run cleanly into terminal COMPLETED status."""
    run = h.store.runs.get_run(run_id)
    assert run is not None
    if run.status == "PENDING":
        out = h.workflows.apply_run_transition(
            run_id,
            "RUNNING",
            context=TransitionContext(
                actor_id=h.master.actor_id,
                actor_kind="orchestrator",
                timestamp=timestamp,
                reason="Start execution run.",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_id,
                    reason="Started run for completion test.",
                ),
            ),
        )
        assert out.accepted is True
    out_r = h.workflows.apply_run_transition(
        run_id,
        "SUCCEEDED",
        context=TransitionContext(
            actor_id=h.master.actor_id,
            actor_kind="orchestrator",
            timestamp=timestamp + timedelta(seconds=1),
            reason="Execution run succeeded.",
            execution_evidence=ExecutionEvidence(
                source="unit_test_fixture",
                run_id=run_id,
                reason="Completed execution run cleanly.",
            ),
        ),
    )
    assert out_r.accepted is True and out_r.run is not None

    ev_id = evidence_id
    if ev_id is None:
        evd = h.evidence_vault.record_evidence(
            auth_context=h.qa_verifier,
            evidence_kind="qa_log",
            task_id=task_id,
            run_id=run_id,
            summary="Independent QA signed task completion.",
            timestamp=timestamp + timedelta(seconds=2),
        )
        ev_id = evd.evidence_id

    out_t = h.workflows.apply_task_transition(
        task_id,
        "COMPLETED",
        context=TransitionContext(
            actor_id="qa.verifier",
            actor_kind="qa_verifier",
            timestamp=timestamp + timedelta(seconds=3),
            reason="Independent QA verified deliverable.",
            run=out_r.run,
            qa_evidence=QaVerificationEvidence(
                evidence_id=ev_id,
                verifier_source="independent_qa",
                verifier_actor_id="qa.verifier",
                verification_status="VERIFIED_PASS",
                verified_at=timestamp + timedelta(seconds=2),
                summary="Verified PASS.",
            ),
            execution_evidence=ExecutionEvidence(
                source="unit_test_fixture",
                run_id=run_id,
                evidence_id=ev_id,
                reason="Task completion backed by independent QA evidence.",
            ),
        ),
    )
    assert out_t.accepted is True, out_t.error_message


def _fail_task_run(h: DepartmentTestHarness, task_id: str, run_id: str, *, timestamp: datetime) -> None:
    """Drive a Run into FAILED while keeping Task active to verify partial run cleanup gate."""
    run = h.store.runs.get_run(run_id)
    assert run is not None
    if run.status == "PENDING":
        res = h.workflows.apply_run_transition(
            run_id,
            "RUNNING",
            context=TransitionContext(
                actor_id=h.master.actor_id,
                actor_kind="orchestrator",
                timestamp=timestamp,
                reason="Start scheduled run.",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_id,
                    reason="Run started.",
                ),
            ),
        )
        assert res.accepted is True
    res2 = h.workflows.apply_run_transition(
        run_id,
        "FAILED",
        context=TransitionContext(
            actor_id=h.master.actor_id,
            actor_kind="orchestrator",
            timestamp=timestamp + timedelta(seconds=1),
            reason="Run failed mid-flight.",
            execution_evidence=ExecutionEvidence(
                source="unit_test_fixture",
                run_id=run_id,
                reason="Simulated execution failure.",
                error_reference=RunErrorRef(
                    error_code="SIMULATED_RUN_FAILURE",
                    message="Simulated execution failure.",
                ),
            ),
        ),
    )
    assert res2.accepted is True


def _new_cleanup_svc(
    h: DepartmentTestHarness,
    *,
    temp_root: Path,
    repo: SQLiteCleanupRepository | None = None,
    with_process: bool = False,
) -> PostTaskCleanupService:
    proc = (
        ProcessDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            retrospective_repo=h.store.process,
        )
        if with_process
        else None
    )
    return PostTaskCleanupService(
        permission_manager=h.permissions,
        cleanup_repository=repo or h.store.cleanup,
        temp_executor=ManagedTempCleanupExecutor(temp_root),
        retrospective_compiler=proc,
    )


def test_p11_05_ast_purity_and_migration_0020_immutability(tmp_path: Path) -> None:
    """Verify AST purity on cleanup modules and append-only triggers on 0020 tables."""
    forbidden = (
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
    )
    for rel_path in ("core/optimization/cleanup_models.py", "core/optimization/cleanup.py"):
        tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), f"{rel_path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden), f"{rel_path} imports from {node.module}"

    h = make_department_harness(tmp_path)
    try:
        t0 = utc(12, 0)
        tid, rid = create_task_and_run(h, title="Trigger Check", description="Append-only check.", timestamp=t0)
        _complete_task(h, tid, rid, timestamp=t0 + timedelta(seconds=1))

        temp_root = tmp_path / "managed_temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        scratch = temp_root / "scratch.tmp"
        scratch.write_text("temp bytes", encoding="utf-8")

        svc = _new_cleanup_svc(h, temp_root=temp_root, with_process=True)
        out = svc.cleanup_and_handoff(
            PostTaskCleanupRequest(
                task_id=tid,
                run_id=rid,
                temp_targets=[TempCleanupTarget(path=str(scratch), resource_kind="TEMP_FILE")],
                timestamp=t0 + timedelta(seconds=10),
            ),
            auth_context=h.master,
        )
        assert len(out.items) >= 1
        assert out.handoff is not None

        with h.store.open_connection() as conn:
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(
                    "UPDATE post_task_cleanup_items SET action = 'FAILED' WHERE cleanup_id = ?;",
                    (out.cleanup.cleanup_id,),
                )
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(
                    "DELETE FROM post_task_cleanup_items WHERE cleanup_id = ?;",
                    (out.cleanup.cleanup_id,),
                )
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(
                    "UPDATE process_learning_handoffs SET handoff_status = 'PENDING_PROCESS_LINK' WHERE handoff_id = ?;",
                    (out.handoff.handoff_id,),
                )
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(
                    "DELETE FROM process_learning_handoffs WHERE handoff_id = ?;",
                    (out.handoff.handoff_id,),
                )
    finally:
        h.store.close()


def test_p11_05_safe_temp_containment_blocks_escape_symlinks_vault_and_shared_refs(tmp_path: Path) -> None:
    """Delete ONLY unshared scratch inside managed_temp_root; preserve escapes, DB, Vault, Evidence & shared refs."""
    h = make_department_harness(tmp_path)
    try:
        t0 = utc(12, 10)
        tid_a, rid_a = create_task_and_run(
            h,
            title="Finished Task A",
            description="Contains scratch temp + deliverables shared with Task B.",
            timestamp=t0,
            assigned_agent_ids=["it.developer"],
        )
        tid_b, rid_b = create_task_and_run(
            h,
            title="Active Task B",
            description="References shared artifact from Task A.",
            timestamp=t0,
            assigned_agent_ids=["it.developer"],
        )
        register_worker_agent(
            h,
            agent_id="it.developer",
            department="it",
            skills=["file_writing"],
            tools_allowed=[],
            timestamp=t0,
        )
        w_ctx = worker_ctx("it.developer", assigned_task_ids=[tid_a, tid_b])

        vault = VaultDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            artifact_vault=h.artifact_vault,
            knowledge_vault=h.evidence_vault,
        )
        shared_art = vault.submit_document(
            VaultSubmissionRequest(
                task_id=tid_a,
                run_id=rid_a,
                owner_agent_id="it.developer",
                logical_key="shared_report_md",
                relative_path="outputs/shared_report.md",
                content_bytes=b"# Important Shared Output\n",
                department="it",
                access_scope="project",
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=w_ctx,
        )
        vault.reuse_artifact_in_task(
            VaultReuseRequest(
                artifact_id=shared_art.artifact.artifact_id,
                target_task_id=tid_b,
                target_run_id=rid_b,
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=w_ctx,
        )
        evd = h.evidence_vault.record_evidence(
            auth_context=h.qa_verifier,
            evidence_kind="qa_log",
            task_id=tid_a,
            run_id=rid_a,
            artifact_id=shared_art.artifact.artifact_id,
            summary="Verified output intact.",
            timestamp=t0 + timedelta(seconds=3),
        )
        _complete_task(h, tid_a, rid_a, timestamp=t0 + timedelta(seconds=4), evidence_id=evd.evidence_id)

        temp_root = tmp_path / "managed_temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        safe_file = temp_root / "scratch_a.log"
        safe_file.write_text("safe scratch content", encoding="utf-8")
        safe_dir = temp_root / "scratch_dir"
        safe_dir.mkdir()
        (safe_dir / "chunk.tmp").write_bytes(b"1234567890")

        outside_file = tmp_path / "outside_important.txt"
        outside_file.write_text("DO NOT DELETE ME", encoding="utf-8")

        shared_temp_file = temp_root / "shared_with_b.csv"
        shared_temp_file.write_text("col1,col2\n1,2\n", encoding="utf-8")

        # Also place a file matching Vault relative_path inside temp_root to verify Vault protection!
        fake_vault_match = temp_root / "outputs" / "shared_report.md"
        fake_vault_match.parent.mkdir(parents=True, exist_ok=True)
        fake_vault_match.write_text("must be protected", encoding="utf-8")

        symlink_path = temp_root / "escape_link.txt"
        symlink_created = False
        try:
            os.symlink(outside_file, symlink_path)
            symlink_created = True
        except OSError:
            symlink_created = False

        targets = [
            TempCleanupTarget(path=str(safe_file), resource_kind="TEMP_FILE"),
            TempCleanupTarget(path=str(safe_dir), resource_kind="TEMP_DIR"),
            TempCleanupTarget(path="../outside_important.txt", resource_kind="TEMP_FILE"),
            TempCleanupTarget(path=str(outside_file), resource_kind="TEMP_FILE"),
            TempCleanupTarget(path=str(h.store.config.db_path), resource_kind="TEMP_FILE"),
            TempCleanupTarget(path=str(shared_temp_file), resource_kind="TEMP_FILE"),
            TempCleanupTarget(path=str(fake_vault_match), resource_kind="TEMP_FILE"),
        ]
        if symlink_created:
            targets.append(TempCleanupTarget(path=str(symlink_path), resource_kind="TEMP_FILE"))

        svc = _new_cleanup_svc(h, temp_root=temp_root, with_process=True)
        out = svc.cleanup_and_handoff(
            PostTaskCleanupRequest(
                task_id=tid_a,
                run_id=rid_a,
                temp_targets=targets,
                preserve_paths=[str(shared_temp_file)],
                timestamp=t0 + timedelta(seconds=10),
            ),
            auth_context=h.master,
        )

        # ONLY safe_file & safe_dir deleted!
        assert not safe_file.exists()
        assert not safe_dir.exists()
        # Everything else preserved intact on disk!
        assert outside_file.exists() and outside_file.read_text(encoding="utf-8") == "DO NOT DELETE ME"
        assert shared_temp_file.exists()
        assert fake_vault_match.exists()
        assert h.store.config.db_path.exists()
        if symlink_created:
            assert os.path.islink(symlink_path)

        assert out.cleanup.cleanup_status == "PARTIAL"
        assert out.cleanup.inventory.deleted_temp_count == 2
        assert out.cleanup.inventory.deleted_bytes_total == len("safe scratch content") + 10
        assert out.cleanup.inventory.blocked_safety_count >= 4
        assert out.cleanup.inventory.preserved_artifact_count == 1
        assert out.cleanup.inventory.preserved_evidence_count == 1

        blocked_actions = {it.action for it in out.items}
        assert "BLOCKED_UNSAFE_PATH" in blocked_actions
        assert "BLOCKED_SHARED_REF" in blocked_actions
        assert "PRESERVED" in blocked_actions
        assert "DELETED" in blocked_actions
    finally:
        h.store.close()


def test_p11_05_blocks_non_terminal_task_releases_leases_and_links_process_handoff(tmp_path: Path) -> None:
    """Enforce terminal guard, release lingering scheduler leases, preserve None metrics, and handoff to Process."""
    h = make_department_harness(tmp_path)
    try:
        t0 = utc(12, 20)
        tid_run, rid_run = create_task_and_run(
            h,
            title="Active Work In Progress",
            description="Must never be cleaned up while actively running.",
            timestamp=t0,
        )
        temp_root = tmp_path / "managed_temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        svc = _new_cleanup_svc(h, temp_root=temp_root, with_process=True)

        # Active Task refused with TASK_NOT_TERMINAL.
        with pytest.raises(CleanupStateError) as st_err:
            svc.cleanup_and_handoff(
                PostTaskCleanupRequest(task_id=tid_run, run_id=rid_run, timestamp=t0 + timedelta(seconds=1)),
                auth_context=h.master,
            )
        assert st_err.value.error_code == "TASK_NOT_TERMINAL"

        # Now test FAILED Run cleanup via force_partial_on_failed_run=True + lease release + resource ledger summary!
        tid_q, rid_q = create_task_and_run(
            h,
            title="Cleanup With Lease & Unavailable Tokens",
            description="Verifies lease release + truthful None tokens + Process link.",
            timestamp=t0,
        )
        # Seed Scheduler policy, enqueue queued task & acquire active lease.
        tid_sched = new_task_id()
        h.workflows.create_task(
            Task(
                task_id=tid_sched,
                title="Queued Task For Lease Test",
                description="Will acquire lease then fail run.",
                requested_by=h.alice.actor_id,
                status="QUEUED",
                priority="NORMAL",
                created_at=t0,
            )
        )
        rid_sched = new_run_id()
        h.workflows.create_run(
            Run(
                run_id=rid_sched,
                task_id=tid_sched,
                runtime_id="antigravity",
                invocation_id="inv-p1105-sched",
                correlation_id="corr-p1105-sched",
                provider_id="antigravity",
                connection_id="agy_cleanup_test",
                model_id="agy-default",
                status="PENDING",
            )
        )
        sched = ExecutionSchedulerService(
            permission_manager=h.permissions,
            scheduler_repository=h.store.scheduler,
        )
        sched.register_quota_policy(
            RegisterQuotaPolicyRequest(
                provider_id="antigravity",
                connection_id="agy_cleanup_test",
                max_concurrency=2,
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h.alice,
        )
        sched.enqueue_task(
            EnqueueTaskRequest(
                task_id=tid_sched,
                run_id=rid_sched,
                provider_id="antigravity",
                connection_id="agy_cleanup_test",
                requested_quota_units=0,
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        resv = sched.reserve_next_execution(
            worker_id="it.worker",
            now=t0 + timedelta(seconds=3),
            auth_context=h.master,
        )
        assert resv.lease is not None

        # Mark run FAILED while keeping Task active -> partial cleanup succeeds when force_partial_on_failed_run=True.
        _fail_task_run(h, tid_sched, rid_sched, timestamp=t0 + timedelta(seconds=4))

        # Record resource usage with unavailable tokens & provider-billed cost + budget warning!
        res_dept = ResourcesDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            metadata_repo=h.store.tasks,
            ledger_repository=h.store.resources,
        )
        res_dept.register_budget_policy(
            RegisterBudgetPolicyRequest(
                scope_kind="TASK",
                scope_ref=tid_sched,
                provider_id="antigravity",
                connection_id="agy_cleanup_test",
                budget_cost_usd_limit=1.00,
                warning_ratio=0.50,
                critical_ratio=1.00,
                policy_note="Cleanup handoff alert snapshot.",
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=h.alice,
        )
        res_dept.record_usage(
            RecordResourceUsageRequest(
                task_id=tid_sched,
                run_id=rid_sched,
                provider_id="antigravity",
                connection_id="agy_cleanup_test",
                model_id="agy-default",
                duration_ms=750,
                duration_source="PROVIDER_MEASURED",
                token_usage_status="UNAVAILABLE_FROM_PROVIDER",
                actual_cost_usd=0.60,
                cost_source="PROVIDER_BILLED",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=h.master,
        )

        # Also finish tid_q normally to assert full completion path too.
        _complete_task(h, tid_q, rid_q, timestamp=t0 + timedelta(seconds=5))

        out = svc.cleanup_and_handoff(
            PostTaskCleanupRequest(
                task_id=tid_sched,
                run_id=rid_sched,
                force_partial_on_failed_run=True,
                deliver_to_process=True,
                timestamp=t0 + timedelta(seconds=10),
            ),
            auth_context=h.master,
        )
        assert out.cleanup.cleanup_status == "COMPLETED"
        assert out.cleanup.inventory.released_lease_count == 1
        assert out.cleanup.inventory.preserved_ledger_count == 1
        assert out.cleanup.inventory.open_alerts_count == 1
        assert out.cleanup.inventory.resource_summary is not None
        assert out.cleanup.inventory.resource_summary.actual_cost_usd_total == pytest.approx(0.60)
        assert out.cleanup.inventory.resource_summary.measured_total_tokens_total is None
        assert out.cleanup.inventory.resource_summary.has_unavailable_token_entries is True

        # Scheduler active executions should now be empty after lease release!
        active_after = sched.get_active_executions(auth_context=h.alice, timestamp=t0 + timedelta(seconds=11))
        assert active_after.total_reserved == 0

        # Process learning handoff compiled & linked to Process retrospective!
        assert out.handoff is not None
        assert out.handoff.handoff_status == "DELIVERED_TO_PROCESS"
        assert out.handoff.retrospective_id is not None
        stored_retro = h.store.process.get_by_task(tid_sched)
        assert stored_retro is not None
        assert stored_retro.retrospective_id == out.handoff.retrospective_id
    finally:
        h.store.close()


def test_p11_05_idempotency_duplicate_handoff_rbac_rollback_and_restart(tmp_path: Path) -> None:
    """Verify idempotency, duplicate handoff denial, worker RBAC denial, rollback & SQLite restart recovery."""
    h1 = make_department_harness(tmp_path)
    try:
        t0 = utc(12, 35)
        tid, rid = create_task_and_run(
            h1,
            title="Restart & Duplicate Handoff Guard",
            description="Ensures cleanup idempotency, rollback & restart durability.",
            timestamp=t0,
            assigned_agent_ids=["it.worker"],
        )
        _complete_task(h1, tid, rid, timestamp=t0 + timedelta(seconds=1))

        temp_root = tmp_path / "managed_temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        scratch = temp_root / "retry_temp.tmp"
        scratch.write_text("clean once", encoding="utf-8")

        svc1 = _new_cleanup_svc(h1, temp_root=temp_root, with_process=True)

        # Worker cannot execute cleanup & handoff.
        w_ctx = worker_ctx("it.worker", assigned_task_ids=[tid])
        with pytest.raises(CleanupGovernanceError):
            svc1.cleanup_and_handoff(
                PostTaskCleanupRequest(task_id=tid, run_id=rid, timestamp=t0 + timedelta(seconds=2)),
                auth_context=w_ctx,
            )

        # Rollback on DB failure before commit keeps cleanups and handoffs empty.
        def _boom(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("simulated crash during cleanup commit")

        failing_svc = _new_cleanup_svc(
            h1,
            temp_root=temp_root,
            repo=SQLiteCleanupRepository(h1.store, before_commit_hook=_boom),
            with_process=True,
        )
        with pytest.raises(Exception, match="rolled back"):
            failing_svc.cleanup_and_handoff(
                PostTaskCleanupRequest(
                    task_id=tid,
                    run_id=rid,
                    idempotency_key="boom-1",
                    timestamp=t0 + timedelta(seconds=3),
                ),
                auth_context=h1.master,
            )
        assert svc1.list_cleanups_for_task(tid, auth_context=h1.alice, timestamp=t0 + timedelta(seconds=4)) == []

        # Execute real cleanup with idempotency_key & sensitive token in note (must scrub).
        req = PostTaskCleanupRequest(
            task_id=tid,
            run_id=rid,
            temp_targets=[TempCleanupTarget(path=str(scratch), resource_kind="TEMP_FILE")],
            idempotency_key="cleanup-idem-1",
            note="Post-task clean after key sk-SecretCleanupNote1234567890",
            timestamp=t0 + timedelta(seconds=5),
        )
        out1 = svc1.cleanup_and_handoff(req, auth_context=h1.master)
        assert not scratch.exists()
        assert out1.idempotent_replay is False

        # Exact replay returns original record without duplicate handoff!
        replay = svc1.cleanup_and_handoff(req, auth_context=h1.master)
        assert replay.idempotent_replay is True
        assert replay.cleanup.cleanup_id == out1.cleanup.cleanup_id

        # Mismatched payload with same idempotency_key fails cleanly.
        with pytest.raises(CleanupConflictError) as idem_err:
            svc1.cleanup_and_handoff(
                req.model_copy(update={"deliver_to_process": False}),
                auth_context=h1.master,
            )
        assert idem_err.value.error_code == "IDEMPOTENCY_PAYLOAD_MISMATCH"

        # Different idempotency_key attempting a second Process handoff for same (task_id, run_id) is blocked!
        with pytest.raises(CleanupConflictError) as dup_err:
            svc1.cleanup_and_handoff(
                PostTaskCleanupRequest(
                    task_id=tid,
                    run_id=rid,
                    idempotency_key="cleanup-second-handoff",
                    deliver_to_process=True,
                    timestamp=t0 + timedelta(seconds=6),
                ),
                auth_context=h1.master,
            )
        assert dup_err.value.error_code == "DUPLICATE_PROCESS_HANDOFF"
    finally:
        h1.store.close()

    # Simulated restart restores cleanup records & Process handoff cleanly.
    h2 = make_department_harness(tmp_path)
    try:
        svc2 = _new_cleanup_svc(h2, temp_root=tmp_path / "managed_temp", with_process=True)
        cleanups = svc2.list_cleanups_for_task(tid, auth_context=h2.alice, timestamp=t0 + timedelta(seconds=20))
        assert len(cleanups) == 1
        assert cleanups[0].cleanup_id == out1.cleanup.cleanup_id
        hoff = svc2.get_handoff_for_task(
            tid,
            run_id=rid,
            auth_context=h2.alice,
            timestamp=t0 + timedelta(seconds=21),
        )
        assert hoff is not None and hoff.handoff_id == out1.handoff.handoff_id
    finally:
        h2.store.close()
