"""Integration tests for P04.06: owned subprocess lifecycle, races, idempotency, and restart reconciliation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.contracts import (
    Artifact,
    ChecklistItem,
    Event,
    Run,
    Task,
    new_artifact_id,
    new_checklist_item_id,
    new_event_id,
    new_evidence_id,
    new_run_id,
    new_task_id,
)
from core.tasks import (
    FallbackEvaluationRequest,
    FallbackPolicy,
    FallbackTarget,
    IdempotentSubmissionRequest,
    QaVerificationEvidence,
    TransitionContext,
    WorkflowLifecycleController,
)
from core.tasks.transitions import ExecutionEvidence
from engines.antigravity.adapter import AntigravityAdapter
from engines.antigravity.lifecycle import (
    AntigravityOwnedExecutionHandle,
    AntigravityProcessManager,
    OwnedSubprocessHandle,
)
from engines.antigravity.models import AntigravityStreamRequest
from engines.antigravity.streaming import is_pid_alive
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import (
    PersistentWorkflowService,
    SQLiteLifecyclePersistenceAdapter,
    SQLiteRepositoryStore,
)


def _setup_task_and_run(
    store: SQLiteRepositoryStore,
    t0: datetime,
    *,
    start_running: bool = False,
) -> tuple[Task, Run]:
    """Create a Task (QUEUED -> ASSIGNED -> optionally RUNNING) and matching Run."""
    svc = PersistentWorkflowService(store)
    task = Task(
        task_id=new_task_id(),
        title="Lifecycle integration task",
        description="Exercise real SQLite + owned process lifecycle.",
        requested_by="operator.local",
        status="QUEUED",
        created_at=t0,
    )
    svc.create_task(task)
    assign_res = svc.apply_task_transition(
        task.task_id,
        "ASSIGNED",
        TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=t0 + timedelta(seconds=1),
            reason="Assign worker",
            assigned_agent_ids=["it.worker"],
        ),
    )
    assert assign_res.task is not None
    run = Run(
        run_id=new_run_id(),
        task_id=task.task_id,
        runtime_id="custom.master_worker_v1",
        status="PENDING",
    )
    svc.create_run(run)
    if start_running:
        start_res = svc.apply_paired_transition(
            "start",
            task.task_id,
            run.run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=2),
                reason="Start execution",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run.run_id,
                    reason="Start fixture",
                ),
            ),
        )
        assert start_res.accepted and start_res.task is not None and start_res.run is not None
        return start_res.task, start_res.run
    return assign_res.task, run


def _spawn_sleeper_subprocess(seconds: int = 30) -> subprocess.Popen[str]:
    """Spawn a real Python child process belonging strictly to the test."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_idempotent_submission_conflict_concurrency_and_restart_persistence(tmp_path: Path) -> None:
    """Idempotent submission survives concurrent retries and restarts without creating duplicate Runs."""
    db_path = tmp_path / "idem.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 17, 0, 0, tzinfo=timezone.utc)
    with SQLiteRepositoryStore(db_path) as store1:
        task, _ = _setup_task_and_run(store1, t0, start_running=False)
        task2, _ = _setup_task_and_run(store1, t0, start_running=False)

    # Concurrent threads submit the exact same idempotency_key.
    def submit_worker(idx: int) -> tuple[str, str | None, bool]:
        with SQLiteRepositoryStore(db_path) as s:
            ctrl = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(s))
            res = ctrl.submit_idempotent_run(
                IdempotentSubmissionRequest(
                    scope="workflow_start",
                    idempotency_key="submit-key-alpha",
                    task_id=task.task_id,
                    run=Run(
                        run_id=new_run_id(),
                        task_id=task.task_id,
                        runtime_id="custom.master_worker_v1",
                        provider_id="antigravity",
                        connection_id="default_antigravity",
                    ),
                    payload={"goal": "build", "step": 1},
                    submitted_at=t0 + timedelta(seconds=2 + idx),
                )
            )
            return res.status, res.run_id, res.created_new

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(submit_worker, range(4)))

    created_count = sum(1 for _, _, c in outcomes if c)
    replay_count = sum(1 for st, _, c in outcomes if st == "IDEMPOTENT_REPLAY" and not c)
    run_ids = {rid for _, rid, _ in outcomes}
    assert created_count == 1
    assert replay_count == 3
    assert len(run_ids) == 1
    canonical_run_id = next(iter(run_ids))

    # After restart, same key + same payload replays existing Run; same key + different payload or Task conflicts.
    with SQLiteRepositoryStore(db_path) as store2:
        ctrl2 = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store2))
        replay_after = ctrl2.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="workflow_start",
                idempotency_key="submit-key-alpha",
                task_id=task.task_id,
                run=Run(
                    run_id=new_run_id(),
                    task_id=task.task_id,
                    runtime_id="custom.master_worker_v1",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                payload={"goal": "build", "step": 1},
                submitted_at=t0 + timedelta(seconds=10),
            )
        )
        assert replay_after.status == "IDEMPOTENT_REPLAY"
        assert replay_after.run_id == canonical_run_id

        conflict_payload = ctrl2.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="workflow_start",
                idempotency_key="submit-key-alpha",
                task_id=task.task_id,
                run=Run(
                    run_id=new_run_id(),
                    task_id=task.task_id,
                    runtime_id="custom.master_worker_v1",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                payload={"goal": "different_payload", "step": 9},
                submitted_at=t0 + timedelta(seconds=11),
            )
        )
        assert conflict_payload.status == "CONFLICT_REJECTED"

        conflict_task = ctrl2.submit_idempotent_run(
            IdempotentSubmissionRequest(
                scope="workflow_start",
                idempotency_key="submit-key-alpha",
                task_id=task2.task_id,
                run=Run(
                    run_id=new_run_id(),
                    task_id=task2.task_id,
                    runtime_id="custom.master_worker_v1",
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                payload={"goal": "build", "step": 1},
                submitted_at=t0 + timedelta(seconds=12),
            )
        )
        assert conflict_task.status == "CONFLICT_REJECTED"


def test_owned_subprocess_cancellation_and_unrelated_process_isolation(tmp_path: Path) -> None:
    """Cancelling an owned child process stops only that PID/job tree and leaves sibling processes alive."""
    db_path = tmp_path / "cancel_subproc.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 17, 10, 0, tzinfo=timezone.utc)
    unrelated_proc = _spawn_sleeper_subprocess(30)
    owned_proc = _spawn_sleeper_subprocess(30)
    try:
        assert is_pid_alive(unrelated_proc.pid)
        assert is_pid_alive(owned_proc.pid)

        with SQLiteRepositoryStore(db_path) as store:
            task1, run1 = _setup_task_and_run(store, t0, start_running=True)
            task2, run2 = _setup_task_and_run(store, t0, start_running=True)
            ctrl = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store))

            handle1 = OwnedSubprocessHandle(owned_proc, execution_id="exec-owned-1", owner_token="own-1")
            ctrl.register_owned_execution(task1.task_id, run1.run_id, handle1, timestamp=t0 + timedelta(seconds=2))

            # Cross-task cancellation fails ownership check without harming process.
            wrong_res = ctrl.request_cancel(
                task2.task_id,
                run1.run_id,
                actor_id="operator.local",
                actor_kind="operator",
                timestamp=t0 + timedelta(seconds=3),
                reason="Wrong task ID",
            )
            assert wrong_res.status == "OWNERSHIP_REJECTED"
            assert is_pid_alive(owned_proc.pid)

            # Valid cancellation confirms stop and kills only owned_proc.
            res = ctrl.request_cancel(
                task1.task_id,
                run1.run_id,
                actor_id="operator.local",
                actor_kind="operator",
                timestamp=t0 + timedelta(seconds=4),
                reason="Operator requested stop",
                owner_token="own-1",
            )
            assert res.status == "CANCEL_CONFIRMED"
            assert res.cancel_requested is True
            assert res.cancel_confirmed is True
            assert res.run_status == "CANCELLED"
            assert res.task_status == "CANCELLED"
            assert not is_pid_alive(owned_proc.pid)
            assert is_pid_alive(unrelated_proc.pid)
            assert ctrl.check_checkpoint_can_continue(task1.task_id, run1.run_id) is False

            # Repeated cancel is idempotent ALREADY_TERMINAL.
            repeat_res = ctrl.request_cancel(
                task1.task_id,
                run1.run_id,
                actor_id="operator.local",
                actor_kind="operator",
                timestamp=t0 + timedelta(seconds=5),
                reason="Double-click cancel",
            )
            assert repeat_res.status == "ALREADY_TERMINAL"
            assert store.runs.get_run(run2.run_id).status == "RUNNING"  # type: ignore[union-attr]

            # Verify workflow.cancel_requested and run.cancelled are distinct ordered events.
            ev_types = [e.event.event_type for e in store.events.read_entries(task_id=task1.task_id)]
            assert "workflow.cancel_requested" in ev_types
            assert "run.cancelled" in ev_types
            assert ev_types.index("workflow.cancel_requested") < ev_types.index("run.cancelled")
    finally:
        for proc in (owned_proc, unrelated_proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


def test_cancel_before_stage_and_cannot_cancel_completed_task(tmp_path: Path) -> None:
    """Checkpoint cancel works before execution starts, and COMPLETED Task refuses cancellation."""
    db_path = tmp_path / "cancel_bounds.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 17, 20, 0, tzinfo=timezone.utc)
    with SQLiteRepositoryStore(db_path) as store:
        ctrl = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store))
        svc = PersistentWorkflowService(store)

        # 1. Cancel before stage starts (PENDING Run).
        t_pending, r_pending = _setup_task_and_run(store, t0, start_running=False)
        res_before = ctrl.request_cancel(
            t_pending.task_id,
            r_pending.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=2),
            reason="Cancel before launch",
        )
        assert res_before.status == "CANCEL_CONFIRMED"
        assert res_before.run_status == "CANCELLED"
        assert ctrl.check_checkpoint_can_continue(t_pending.task_id, r_pending.run_id) is False

        # 2. Cannot overwrite COMPLETED Task with CANCELLED.
        t_done, r_done = _setup_task_and_run(store, t0, start_running=True)
        ev_id = new_evidence_id()
        art = Artifact(
            artifact_id=new_artifact_id(),
            task_id=t_done.task_id,
            run_id=r_done.run_id,
            owner_agent_id="it.worker",
            relative_path="output/done.txt",
            sha256="d" * 64,
            verification_status="VERIFIED_PASS",
            evidence_reference=ev_id,
        )
        chk = ChecklistItem(
            checklist_item_id=new_checklist_item_id(),
            task_id=t_done.task_id,
            title="Done",
            status="COMPLETED",
            order=0,
            evidence_reference=ev_id,
            created_at=t0,
            completed_at=t0 + timedelta(seconds=3),
        )
        store.metadata.save_checklist_item(chk)
        store.metadata.save_artifact(art)
        svc.apply_paired_transition(
            "complete_run_only",
            t_done.task_id,
            r_done.run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=4),
                reason="Run succeeded",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=r_done.run_id,
                    reason="Done",
                ),
            ),
        )
        svc.apply_task_transition(
            t_done.task_id,
            "COMPLETED",
            TransitionContext(
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                timestamp=t0 + timedelta(seconds=5),
                reason="QA pass",
                qa_evidence=QaVerificationEvidence(
                    verifier_source="unit_test_fixture",
                    verifier_actor_id="qa.verifier",
                    verification_status="VERIFIED_PASS",
                    verified_at=t0 + timedelta(seconds=5),
                    artifact_ids=[art.artifact_id],
                    expected_sha256="d" * 64,
                    actual_sha256="d" * 64,
                ),
                checklist_items=[chk],
                artifacts=[art],
            ),
        )
        res_after = ctrl.request_cancel(
            t_done.task_id,
            r_done.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=6),
            reason="Late cancel after COMPLETED",
        )
        assert res_after.status == "ALREADY_TERMINAL"
        assert res_after.cancel_confirmed is False
        assert store.tasks.get_task(t_done.task_id).status == "COMPLETED"  # type: ignore[union-attr]


def test_timeout_stops_owned_child_and_races_produce_single_terminal_state(tmp_path: Path) -> None:
    """Timeout terminates owned child process cleanly, and concurrent timeout/cancel/completion settles to one terminal state."""
    db_path = tmp_path / "timeout_race.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 17, 30, 0, tzinfo=timezone.utc)
    proc_timeout = _spawn_sleeper_subprocess(30)
    proc_race = _spawn_sleeper_subprocess(30)
    try:
        with SQLiteRepositoryStore(db_path) as store:
            ctrl = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store))

            # 1. Timeout enforces deadline, stops child PID, and commits Run.TIMED_OUT.
            t_to, r_to = _setup_task_and_run(store, t0, start_running=True)
            handle_to = OwnedSubprocessHandle(proc_timeout, execution_id="exec-to-1", owner_token="tok-to")
            ctrl.register_owned_execution(
                t_to.task_id,
                r_to.run_id,
                handle_to,
                timestamp=t0 + timedelta(seconds=2),
                timeout_deadline_at=t0 + timedelta(seconds=10),
            )
            early = ctrl.handle_timeout(
                t_to.task_id,
                r_to.run_id,
                actor_id="core.master",
                timestamp=t0 + timedelta(seconds=5),
            )
            assert early.status == "NOT_ELAPSED"
            assert is_pid_alive(proc_timeout.pid)

            timed_out = ctrl.handle_timeout(
                t_to.task_id,
                r_to.run_id,
                actor_id="core.master",
                timestamp=t0 + timedelta(seconds=11),
                reason="Stage timed out",
            )
            assert timed_out.status == "TIMED_OUT_CONFIRMED"
            assert timed_out.timed_out_confirmed is True
            assert timed_out.run_status == "TIMED_OUT"
            assert not is_pid_alive(proc_timeout.pid)

            # 2. Race: timeout vs cancel simultaneously on the same Run yields exactly one terminal commit.
            t_race, r_race = _setup_task_and_run(store, t0, start_running=True)
            handle_race = OwnedSubprocessHandle(proc_race, execution_id="exec-race-1", owner_token="tok-race")
            ctrl.register_owned_execution(
                t_race.task_id,
                r_race.run_id,
                handle_race,
                timestamp=t0 + timedelta(seconds=2),
                timeout_deadline_at=t0 + timedelta(seconds=5),
            )

            def do_cancel() -> str:
                r = ctrl.request_cancel(
                    t_race.task_id,
                    r_race.run_id,
                    actor_id="operator.local",
                    actor_kind="operator",
                    timestamp=t0 + timedelta(seconds=6),
                    reason="Race cancel",
                    owner_token="tok-race",
                )
                return r.status

            def do_timeout() -> str:
                r = ctrl.handle_timeout(
                    t_race.task_id,
                    r_race.run_id,
                    actor_id="core.master",
                    timestamp=t0 + timedelta(seconds=6),
                    reason="Race timeout",
                )
                return r.status

            with ThreadPoolExecutor(max_workers=2) as pool:
                f_cancel = pool.submit(do_cancel)
                f_timeout = pool.submit(do_timeout)
                race_results = {f_cancel.result(), f_timeout.result()}

            assert not is_pid_alive(proc_race.pid)
            assert "ALREADY_TERMINAL" in race_results
            assert race_results & {"CANCEL_CONFIRMED", "TIMED_OUT_CONFIRMED"}
            final_run = store.runs.get_run(r_race.run_id)
            assert final_run is not None and final_run.status in {"CANCELLED", "TIMED_OUT"}
            terminal_events = [
                e.event.event_type
                for e in store.events.read_entries(run_id=r_race.run_id)
                if e.event.event_type in {"run.cancelled", "run.timed_out", "run.completed"}
            ]
            assert len(terminal_events) == 1
    finally:
        for proc in (proc_timeout, proc_race):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


def test_restart_reconciliation_unobservable_process_and_antigravity_handle(tmp_path: Path) -> None:
    """After restart while RUNNING, reconcile flags NEEDS_RECONCILIATION without fabricating COMPLETED/FAILED."""
    db_path = tmp_path / "reconcile.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 17, 40, 0, tzinfo=timezone.utc)
    # 1. Verify AntigravityOwnedExecutionHandle integrates with AntigravityProcessManager safely.
    def fake_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        _ = args, kwargs
        return _spawn_sleeper_subprocess(30)

    pm = AntigravityProcessManager(
        adapter=AntigravityAdapter(
            cli_path=sys.executable,
            popen_factory=fake_popen,
        )
    )
    pm_db = tmp_path / "antigravity_pm.db"
    with sqlite_connection(pm_db) as conn:
        apply_migrations(conn)
    with SQLiteRepositoryStore(pm_db) as pm_store:
        t_pm, r_pm = _setup_task_and_run(pm_store, t0, start_running=True)
        ctrl_pm = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(pm_store))
        exec_id = pm.start_execution(
            AntigravityStreamRequest(
                prompt="sleep",
                workspace_path=str(tmp_path),
            ),
            owner_token="tok-agy-1",
        )
        # Wait briefly until process spawns
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and pm.get_execution_status(exec_id).pid is None:
            time.sleep(0.02)
        agy_handle = AntigravityOwnedExecutionHandle(pm, exec_id, "tok-agy-1")
        ctrl_pm.register_owned_execution(t_pm.task_id, r_pm.run_id, agy_handle, timestamp=t0 + timedelta(seconds=2))
        agy_cancel = ctrl_pm.request_cancel(
            t_pm.task_id,
            r_pm.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=3),
            reason="Cancel managed Antigravity execution",
            owner_token="tok-agy-1",
        )
        assert agy_cancel.status == "CANCEL_CONFIRMED"
        assert agy_cancel.run_status == "CANCELLED"
        assert not agy_handle.is_running()

    # 2. Completion vs cancellation race settles cleanly to one terminal state.
    race_db = tmp_path / "comp_cancel_race.db"
    with sqlite_connection(race_db) as conn:
        apply_migrations(conn)
    with SQLiteRepositoryStore(race_db) as race_store:
        t_cc, r_cc = _setup_task_and_run(race_store, t0, start_running=True)
        ctrl_cc = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(race_store))
        p_cc = _spawn_sleeper_subprocess(30)
        try:
            h_cc = OwnedSubprocessHandle(p_cc, execution_id="exec-cc-1", owner_token="tok-cc")
            ctrl_cc.register_owned_execution(t_cc.task_id, r_cc.run_id, h_cc, timestamp=t0 + timedelta(seconds=2))

            def do_complete() -> str:
                ok, _, _, code, _ = ctrl_cc.complete_owned_run(
                    t_cc.task_id,
                    r_cc.run_id,
                    TransitionContext(
                        actor_id="core.master",
                        actor_kind="core_runtime",
                        timestamp=t0 + timedelta(seconds=4),
                        reason="Completed concurrently",
                        execution_evidence=ExecutionEvidence(
                            source="unit_test_fixture",
                            run_id=r_cc.run_id,
                            reason="Done",
                        ),
                    ),
                )
                return "SUCCEEDED" if ok else (code or "REJECTED")

            def do_cancel_cc() -> str:
                return ctrl_cc.request_cancel(
                    t_cc.task_id,
                    r_cc.run_id,
                    actor_id="operator.local",
                    actor_kind="operator",
                    timestamp=t0 + timedelta(seconds=4),
                    reason="Cancel concurrently",
                    owner_token="tok-cc",
                ).status

            with ThreadPoolExecutor(max_workers=2) as pool:
                fc = pool.submit(do_complete)
                fx = pool.submit(do_cancel_cc)
                outcomes_cc = {fc.result(), fx.result()}
            final_cc = race_store.runs.get_run(r_cc.run_id)
            assert final_cc is not None and final_cc.status in {"SUCCEEDED", "CANCELLED"}
            assert len(outcomes_cc) == 2
        finally:
            if p_cc.poll() is None:
                p_cc.terminate()
                p_cc.wait(timeout=2.0)
            if p_cc.stdout:
                p_cc.stdout.close()
            if p_cc.stderr:
                p_cc.stderr.close()
    with SQLiteRepositoryStore(db_path) as store1:
        task, run = _setup_task_and_run(store1, t0, start_running=True)
        ctrl1 = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store1))
        proc = _spawn_sleeper_subprocess(30)
        try:
            handle = OwnedSubprocessHandle(proc, execution_id="exec-reconcile-1", owner_token="tok-rec")
            ctrl1.register_owned_execution(task.task_id, run.run_id, handle, timestamp=t0 + timedelta(seconds=2))
            # Stop the process cleanly to simulate OS cleanup before closing store1, keeping DB in RUNNING state.
            handle.request_cancel(owner_token="tok-rec", wait=True, timeout_seconds=3.0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    # Reopen brand-new store + controller after simulated restart.
    with SQLiteRepositoryStore(db_path) as store2:
        ctrl2 = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(store2))
        rec = ctrl2.reconcile_interrupted_task(task.task_id, timestamp=t0 + timedelta(seconds=20))
        assert rec.recovery_status == "NEEDS_RECONCILIATION"
        assert rec.reconciled_status == "NEEDS_RECONCILIATION"
        assert rec.process_observable is False
        assert rec.reconciliation_required is True
        assert run.run_id in rec.interrupted_run_ids
        assert store2.runs.get_run(run.run_id).status == "RUNNING"  # type: ignore[union-attr]
        assert store2.tasks.get_task(task.task_id).status == "RUNNING"  # type: ignore[union-attr]

        # Attempting cancel after restart without observable process returns PROCESS_NOT_OBSERVABLE, never CANCELLED.
        post_restart_cancel = ctrl2.request_cancel(
            task.task_id,
            run.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=21),
            reason="Post-restart cancel without observable PID",
        )
        assert post_restart_cancel.status == "PROCESS_NOT_OBSERVABLE"
        assert post_restart_cancel.cancel_confirmed is False
        assert store2.runs.get_run(run.run_id).status == "RUNNING"  # type: ignore[union-attr]

        # Fallback policy recording writes traceable Event.
        fb = ctrl2.evaluate_and_record_fallback(
            FallbackPolicy(fallback_allowed=False, max_retries=1),
            FallbackEvaluationRequest(
                task_id=task.task_id,
                run_id=run.run_id,
                current_target=FallbackTarget(
                    provider_id="antigravity",
                    connection_id="default_antigravity",
                ),
                attempt_number=1,
                operation_is_idempotent=True,
                failure_reason_code="PROVIDER_ERROR",
                requested_at=t0 + timedelta(seconds=22),
            ),
        )
        assert fb.decision == "ALLOW_RETRY"
        events = store2.events.read_entries(task_id=task.task_id)
        assert any(e.event.event_type == "workflow.reconciliation_recorded" for e in events)
        assert any(e.event.event_type == "workflow.fallback_evaluated" for e in events)
        assert pm is not None
        _ = AntigravityOwnedExecutionHandle
        _ = AntigravityStreamRequest
