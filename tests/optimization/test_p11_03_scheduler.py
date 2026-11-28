"""Unit, Concurrency, Governance, Quota/Rate-Limit & Recovery tests for Phase 11 / P11.03 Scheduler."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

from pydantic import ValidationError
import pytest

from core.contracts import Run, Task, new_run_id, new_task_id
from core.departments import (
    ReceptionDepartmentService,
    UserRequestIntake,
)
from core.optimization import (
    ConnectionSwitchEvaluationRequest,
    EnqueueTaskRequest,
    ExecutionReleaseRequest,
    ExecutionSchedulerService,
    ExecutionStartConfirmRequest,
    RegisterQuotaPolicyRequest,
    RegisterScheduledTaskRequest,
    SchedulerConflictError,
    SchedulerGovernanceError,
    SchedulerPersistenceError,
    SchedulerStateError,
    compute_quota_bucket_key,
)
from core.tasks import ExecutionEvidence, FallbackPolicy, FallbackTarget, TransitionContext
from storage.db import sqlite_connection
from storage.repositories import SQLiteRepositoryStore, SQLiteSchedulerRepository
from tests.departments.conftest import (
    REPO_ROOT,
    DepartmentTestHarness,
    make_department_harness,
    request_and_approve,
    utc,
    worker_ctx,
)


def _seed_queued_task(
    h: DepartmentTestHarness,
    *,
    title: str,
    timestamp: datetime,
    priority: str = "NORMAL",
    status: str = "QUEUED",
) -> str:
    task_id = new_task_id()
    h.workflows.create_task(
        Task(
            task_id=task_id,
            title=title,
            description=f"Scheduled workload: {title}",
            requested_by="operator.alice",
            status="QUEUED",
            priority=priority,  # type: ignore[arg-type]
            created_at=timestamp,
        )
    )
    if status != "QUEUED":
        if status == "ASSIGNED":
            res = h.workflows.apply_task_transition(
                task_id,
                "ASSIGNED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=timestamp + timedelta(seconds=1),
                    reason="Assigned for execution",
                    assigned_agent_ids=["it.worker"],
                ),
            )
            assert res.accepted is True
        elif status == "WAITING_APPROVAL":
            res1 = h.workflows.apply_task_transition(
                task_id,
                "ASSIGNED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=timestamp + timedelta(seconds=1),
                    reason="Assigned before approval gate",
                    assigned_agent_ids=["it.worker"],
                ),
            )
            assert res1.accepted is True
            res2 = h.workflows.apply_task_transition(
                task_id,
                "WAITING_APPROVAL",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=timestamp + timedelta(seconds=2),
                    reason="Waiting operator approval",
                ),
            )
            assert res2.accepted is True
        elif status == "CANCELLED":
            res = h.workflows.apply_task_transition(
                task_id,
                "CANCELLED",
                TransitionContext(
                    actor_id="operator.alice",
                    actor_kind="operator",
                    timestamp=timestamp + timedelta(seconds=1),
                    reason="Cancelled before enqueue",
                    execution_evidence=ExecutionEvidence(
                        source="core_runtime",
                        reason="Cancelled before enqueue",
                        confirmed_cancelled=True,
                        cancellation_source="core_runtime",
                    ),
                ),
            )
            assert res.accepted is True
    return task_id


def test_p11_03_ast_purity_and_migration_0018_triggers(tmp_path: Path) -> None:
    """Verify core/optimization AST boundaries and append-only triggers on rate_limit_events."""
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
    opt_dir = REPO_ROOT / "core" / "optimization"
    for py_file in sorted(opt_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden)

    # Reject missing repo fail-closed.
    h = make_department_harness(tmp_path)
    try:
        t0 = utc(9, 0, 0)
        unbacked = ExecutionSchedulerService(permission_manager=h.permissions, scheduler_repository=None)
        with pytest.raises(SchedulerPersistenceError):
            unbacked.get_queue_visibility(auth_context=h.alice, timestamp=t0)

        sched = ExecutionSchedulerService(permission_manager=h.permissions, scheduler_repository=h.store.scheduler)
        sched.register_quota_policy(
            RegisterQuotaPolicyRequest(
                provider_id="google_gemini",
                connection_id="gem_main",
                quota_scope_kind="PROJECT_SHARED",
                project_ref="prj_trigger",
                max_concurrency=1,
                rate_limit_max_requests=5,
                rate_limit_window_seconds=60,
                timestamp=t0,
            ),
            auth_context=h.alice,
        )
        tid = _seed_queued_task(h, title="Rate trigger verify", timestamp=t0)
        sched.enqueue_task(
            EnqueueTaskRequest(
                task_id=tid,
                provider_id="google_gemini",
                connection_id="gem_main",
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h.master,
        )
        res = sched.reserve_next_execution(
            worker_id="it.worker",
            now=t0 + timedelta(seconds=2),
            auth_context=h.master,
        )
        assert res.lease is not None
        with sqlite_connection(h.db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE scheduler_rate_limit_events SET recorded_at = '2026-01-01';")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM scheduler_rate_limit_events;")
    finally:
        h.store.close()


def test_p11_03_concurrency_shared_and_unproven_quota_scopes_and_key_rotation_blocked(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Enforce shared project & unproven quota buckets, independent connections, and zero key rotation."""
    t0 = utc(10, 0, 0)
    sched = ExecutionSchedulerService(
        permission_manager=dept_harness.permissions,
        scheduler_repository=dept_harness.store.scheduler,
    )

    # 1. Two keys sharing project_ref='prj_alpha' share ONE quota bucket -> no key rotation evasion!
    pol_a = sched.register_quota_policy(
        RegisterQuotaPolicyRequest(
            provider_id="google_gemini",
            connection_id="gem_key_1",
            project_ref="prj_alpha",
            quota_scope_kind="PROJECT_SHARED",
            max_concurrency=1,
            quota_limit_units=1,
            timestamp=t0,
        ),
        auth_context=dept_harness.alice,
    )
    pol_b = sched.register_quota_policy(
        RegisterQuotaPolicyRequest(
            provider_id="google_gemini",
            connection_id="gem_key_2",
            project_ref="prj_alpha",
            quota_scope_kind="PROJECT_SHARED",
            max_concurrency=2,
            quota_limit_units=5,
            timestamp=t0,
        ),
        auth_context=dept_harness.alice,
    )
    assert pol_a.quota_bucket_key == pol_b.quota_bucket_key == "project:google_gemini:prj_alpha"

    # Unproven connections without project_ref or independent_quota_proof ALSO share bucket!
    unprov_1 = sched.register_quota_policy(
        RegisterQuotaPolicyRequest(
            provider_id="openai",
            connection_id="oai_1",
            quota_scope_kind="UNKNOWN_SHARED",
            max_concurrency=1,
            timestamp=t0,
        ),
        auth_context=dept_harness.alice,
    )
    unprov_2 = sched.register_quota_policy(
        RegisterQuotaPolicyRequest(
            provider_id="openai",
            connection_id="oai_2",
            quota_scope_kind="UNKNOWN_SHARED",
            max_concurrency=1,
            timestamp=t0,
        ),
        auth_context=dept_harness.alice,
    )
    assert unprov_1.quota_bucket_key == unprov_2.quota_bucket_key == "unproven_shared:openai"

    # Trying to declare CONNECTION_VERIFIED without independent_quota_proof=True fails validation!
    with pytest.raises(ValidationError):
        RegisterQuotaPolicyRequest(
            provider_id="google_gemini",
            connection_id="gem_fake_indep",
            quota_scope_kind="CONNECTION_VERIFIED",
            independent_quota_proof=False,
            timestamp=t0,
        )

    # Independent connection on DIFFERENT provider ('antigravity') runs independently!
    pol_indep = sched.register_quota_policy(
        RegisterQuotaPolicyRequest(
            provider_id="antigravity",
            connection_id="agy_local",
            quota_scope_kind="CONNECTION_VERIFIED",
            independent_quota_proof=True,
            max_concurrency=2,
            quota_limit_units=5,
            timestamp=t0,
        ),
        auth_context=dept_harness.alice,
    )
    assert pol_indep.quota_bucket_key == "conn:antigravity:agy_local"

    t1 = _seed_queued_task(dept_harness, title="Quota shared 1", timestamp=t0)
    t2 = _seed_queued_task(dept_harness, title="Quota shared 2 via key_2", timestamp=t0 + timedelta(seconds=1))
    t3 = _seed_queued_task(dept_harness, title="Independent provider task", timestamp=t0 + timedelta(seconds=2))

    sched.enqueue_task(
        EnqueueTaskRequest(task_id=t1, provider_id="google_gemini", connection_id="gem_key_1", timestamp=t0 + timedelta(seconds=3)),
        auth_context=dept_harness.master,
    )
    sched.enqueue_task(
        EnqueueTaskRequest(task_id=t2, provider_id="google_gemini", connection_id="gem_key_2", timestamp=t0 + timedelta(seconds=4)),
        auth_context=dept_harness.master,
    )
    sched.enqueue_task(
        EnqueueTaskRequest(task_id=t3, provider_id="antigravity", connection_id="agy_local", timestamp=t0 + timedelta(seconds=5)),
        auth_context=dept_harness.master,
    )

    # First reserve claims gem_key_1 (uses 1/1 quota on shared prj_alpha bucket).
    r1 = sched.reserve_next_execution(worker_id="it.worker", now=t0 + timedelta(seconds=6), auth_context=dept_harness.master)
    assert r1.decision.outcome == "RESERVED" and r1.queue_entry is not None and r1.queue_entry.task_id == t1

    # Next reserve evaluates t2 (gem_key_2, blocked by shared prj_alpha quota!), skips to independent t3!
    r2 = sched.reserve_next_execution(worker_id="qa.worker", now=t0 + timedelta(seconds=7), auth_context=dept_harness.master)
    assert r2.decision.outcome == "RESERVED" and r2.queue_entry is not None and r2.queue_entry.task_id == t3
    assert any(b.task_id == t2 and b.status == "BLOCKED_BY_QUOTA" for b in r2.blocked_entries)

    vis = sched.get_queue_visibility(auth_context=dept_harness.alice, timestamp=t0 + timedelta(seconds=8))
    assert [e.task_id for e in vis.blocked_by_quota] == [t2]
    assert len(vis.reserved_entries) == 2

    # Key rotation across same provider blocked fail-closed!
    switch_res = sched.evaluate_connection_switch(
        ConnectionSwitchEvaluationRequest(
            task_id=t1,
            run_id=new_run_id(),
            current_target=FallbackTarget(provider_id="google_gemini", connection_id="gem_key_1"),
            candidate_target=FallbackTarget(provider_id="google_gemini", connection_id="gem_key_2"),
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="QUOTA_EXCEEDED",
            available_target_capabilities=["text_generation"],
            fallback_policy=FallbackPolicy(
                fallback_allowed=True,
                max_retries=2,
                allowed_targets=[FallbackTarget(provider_id="google_gemini", connection_id="gem_key_2")],
            ),
            timestamp=t0 + timedelta(seconds=9),
        ),
        auth_context=dept_harness.master,
    )
    assert switch_res.decision == "DENY"
    assert switch_res.reason_code == "KEY_ROTATION_FORBIDDEN"


def test_p11_03_rate_limit_backoff_retry_budget_fairness_and_active_start_truthfulness(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify sliding-window rate limit, bounded backoff, anti-starvation aging, and real Run RUNNING guard."""
    t0 = utc(11, 0, 0)
    sched = ExecutionSchedulerService(
        permission_manager=dept_harness.permissions,
        scheduler_repository=dept_harness.store.scheduler,
    )
    sched.register_quota_policy(
        RegisterQuotaPolicyRequest(
            provider_id="antigravity",
            connection_id="agy_rate",
            quota_scope_kind="CONNECTION_VERIFIED",
            independent_quota_proof=True,
            max_concurrency=3,
            rate_limit_max_requests=1,
            rate_limit_window_seconds=30,
            base_backoff_seconds=10,
            max_backoff_seconds=40,
            max_retries=2,
            timestamp=t0,
        ),
        auth_context=dept_harness.alice,
    )

    # Low priority task submitted first (t=1s), urgent task submitted later (t=200s): fairness aging promotes LOW!
    t_low = _seed_queued_task(dept_harness, title="Aged LOW task", priority="LOW", timestamp=t0 + timedelta(seconds=1))
    t_urgent = _seed_queued_task(dept_harness, title="Fresh URGENT task", priority="URGENT", timestamp=t0 + timedelta(seconds=200))

    sched.enqueue_task(
        EnqueueTaskRequest(
            task_id=t_low,
            provider_id="antigravity",
            connection_id="agy_rate",
            priority="LOW",
            fairness_aging_seconds=30,
            timestamp=t0 + timedelta(seconds=2),
        ),
        auth_context=dept_harness.master,
    )
    sched.enqueue_task(
        EnqueueTaskRequest(
            task_id=t_urgent,
            provider_id="antigravity",
            connection_id="agy_rate",
            priority="URGENT",
            fairness_aging_seconds=30,
            timestamp=t0 + timedelta(seconds=200),
        ),
        auth_context=dept_harness.master,
    )

    # At t0 + 210s: LOW aged ~208s // 30 = 6 -> rank 4 - 6 = -2, beating fresh URGENT rank 1!
    res1 = sched.reserve_next_execution(worker_id="it.worker", now=t0 + timedelta(seconds=210), auth_context=dept_harness.master)
    assert res1.lease is not None and res1.queue_entry is not None
    assert res1.queue_entry.task_id == t_low

    # Active execution view shows RESERVED (0 RUNNING!) because Runtime Run hasn't started yet.
    act1 = sched.get_active_executions(auth_context=dept_harness.alice, timestamp=t0 + timedelta(seconds=210))
    assert act1.total_reserved == 1 and act1.total_running == 0

    # Trying confirm_execution_started with PENDING run fails closed!
    run_low = Run(run_id=new_run_id(), task_id=t_low, runtime_id="custom.master_worker_v1", status="PENDING")
    dept_harness.workflows.apply_task_transition(
        t_low,
        "ASSIGNED",
        TransitionContext(actor_id="core.master", actor_kind="orchestrator", timestamp=t0 + timedelta(seconds=211), reason="Assign", assigned_agent_ids=["it.worker"]),
    )
    dept_harness.workflows.create_run(run_low)
    with pytest.raises(SchedulerStateError) as not_running_err:
        sched.confirm_execution_started(
            ExecutionStartConfirmRequest(
                lease_id=res1.lease.lease_id,
                owner_token=res1.lease.owner_token,
                run_id=run_low.run_id,
                expected_record_version=1,
                timestamp=t0 + timedelta(seconds=212),
            ),
            auth_context=dept_harness.master,
        )
    assert not_running_err.value.error_code == "RUN_NOT_RUNNING_IN_RUNTIME"

    # Transition Run -> RUNNING cleanly -> now Scheduler confirms ACTIVE / RUNNING!
    dept_harness.workflows.apply_paired_transition(
        "start",
        t_low,
        run_low.run_id,
        TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=t0 + timedelta(seconds=212),
            reason="Runtime started",
            execution_evidence=ExecutionEvidence(source="core_runtime", run_id=run_low.run_id, reason="Started"),
        ),
    )
    active_lease = sched.confirm_execution_started(
        ExecutionStartConfirmRequest(
            lease_id=res1.lease.lease_id,
            owner_token=res1.lease.owner_token,
            run_id=run_low.run_id,
            expected_record_version=1,
            timestamp=t0 + timedelta(seconds=213),
        ),
        auth_context=dept_harness.master,
    )
    assert active_lease.state == "ACTIVE"
    act2 = sched.get_active_executions(auth_context=dept_harness.alice, timestamp=t0 + timedelta(seconds=213))
    assert act2.total_reserved == 0 and act2.total_running == 1

    # Immediate second reserve hits sliding-window rate limit (1 req / 30s) -> bounded backoff!
    blk1 = sched.reserve_next_execution(worker_id="qa.worker", now=t0 + timedelta(seconds=215), auth_context=dept_harness.master)
    assert blk1.decision.outcome == "BLOCKED_BY_RATE_LIMIT"
    assert blk1.decision.retry_after_seconds == 10
    assert blk1.decision.next_retry_at == t0 + timedelta(seconds=225)

    # Attempting before backoff expires stays blocked without burning another retry attempt!
    early = sched.reserve_next_execution(worker_id="qa.worker", now=t0 + timedelta(seconds=220), auth_context=dept_harness.master)
    assert early.decision.reason_code == "RATE_LIMIT_BACKOFF_ACTIVE"

    # Attempting at t+226s (still inside 30s window from t+210s) triggers retry #2 (backoff=20s), then t+250s SUCCEEDS!
    blk2 = sched.reserve_next_execution(worker_id="qa.worker", now=t0 + timedelta(seconds=226), auth_context=dept_harness.master)
    assert blk2.decision.outcome == "BLOCKED_BY_RATE_LIMIT"
    assert blk2.decision.retry_after_seconds == 20

    res2 = sched.reserve_next_execution(worker_id="qa.worker", now=t0 + timedelta(seconds=250), auth_context=dept_harness.master)
    assert res2.decision.outcome == "RESERVED" and res2.queue_entry is not None and res2.queue_entry.task_id == t_urgent


def test_p11_03_concurrency_threads_lease_expiry_restart_schedules_and_guards(
    tmp_path: Path,
) -> None:
    """Verify multi-threaded single-slot atomicity, lease expiry + restart recovery, schedules & RBAC."""
    t0 = utc(12, 0, 0)
    h1 = make_department_harness(tmp_path)
    try:
        sched1 = ExecutionSchedulerService(permission_manager=h1.permissions, scheduler_repository=h1.store.scheduler)
        sched1.register_quota_policy(
            RegisterQuotaPolicyRequest(
                provider_id="antigravity",
                connection_id="agy_conc",
                quota_scope_kind="CONNECTION_VERIFIED",
                independent_quota_proof=True,
                max_concurrency=2,
                timestamp=t0,
            ),
            auth_context=h1.alice,
        )

        # Idempotent enqueue vs duplicate vs terminal & approval guards.
        tid_ok1 = _seed_queued_task(h1, title="Concurrent item 1", timestamp=t0)
        tid_ok2 = _seed_queued_task(h1, title="Concurrent item 2", timestamp=t0)
        tid_ok3 = _seed_queued_task(h1, title="Concurrent item 3", timestamp=t0)
        tid_term = _seed_queued_task(h1, title="Terminal cancelled", status="CANCELLED", timestamp=t0)
        tid_appr = _seed_queued_task(h1, title="Needs approval", status="WAITING_APPROVAL", timestamp=t0)

        with pytest.raises(SchedulerStateError):
            sched1.enqueue_task(
                EnqueueTaskRequest(task_id=tid_term, provider_id="antigravity", connection_id="agy_conc", timestamp=t0),
                auth_context=h1.master,
            )

        q1 = sched1.enqueue_task(
            EnqueueTaskRequest(task_id=tid_ok1, provider_id="antigravity", connection_id="agy_conc", idempotency_key="idem-q1", timestamp=t0),
            auth_context=h1.master,
        )
        q1_replay = sched1.enqueue_task(
            EnqueueTaskRequest(task_id=tid_ok1, provider_id="antigravity", connection_id="agy_conc", idempotency_key="idem-q1", timestamp=t0),
            auth_context=h1.master,
        )
        assert q1.queue_entry_id == q1_replay.queue_entry_id
        with pytest.raises(SchedulerConflictError):
            sched1.enqueue_task(
                EnqueueTaskRequest(task_id=tid_ok2, provider_id="antigravity", connection_id="agy_conc", idempotency_key="idem-q1", timestamp=t0),
                auth_context=h1.master,
            )

        sched1.enqueue_task(EnqueueTaskRequest(task_id=tid_ok2, provider_id="antigravity", connection_id="agy_conc", timestamp=t0), auth_context=h1.master)
        sched1.enqueue_task(EnqueueTaskRequest(task_id=tid_ok3, provider_id="antigravity", connection_id="agy_conc", timestamp=t0), auth_context=h1.master)
        q_appr = sched1.enqueue_task(EnqueueTaskRequest(task_id=tid_appr, provider_id="antigravity", connection_id="agy_conc", timestamp=t0 + timedelta(seconds=3)), auth_context=h1.master)
        assert q_appr.status == "BLOCKED_GOVERNANCE"

        # 5 concurrent worker threads race for max_concurrency=2 slots -> strictly 2 granted, no duplicates!
        def _race(idx: int) -> str | None:
            r = sched1.reserve_next_execution(
                worker_id=f"it.worker_{idx}",
                lease_ttl_seconds=30,
                now=t0 + timedelta(seconds=10),
                auth_context=h1.master,
            )
            return r.queue_entry.task_id if r.queue_entry is not None else None

        with ThreadPoolExecutor(max_workers=5) as pool:
            claimed = [tid for tid in pool.map(_race, range(5)) if tid is not None]
        assert len(claimed) == 2
        assert len(set(claimed)) == 2
        assert tid_appr not in claimed

        # Scheduled task metadata validation + approval gating!
        with pytest.raises(ValidationError):
            RegisterScheduledTaskRequest(
                title="Bad TZ",
                description="Invalid tz",
                provider_id="antigravity",
                connection_id="agy_conc",
                timezone_name="NotATimezone",
                scheduled_at=t0 + timedelta(hours=1),
                policy_note="bad tz check",
                created_at=t0,
            )

        tid_sched = _seed_queued_task(h1, title="Scheduled report", timestamp=t0)
        apr_id = request_and_approve(
            h1,
            action="task.transition",
            resource="scheduler:due_run",
            parameters={"task_id": tid_sched},
            requester=h1.master,
            decider=h1.alice,
            task_id=tid_sched,
            risk_summary="Approve scheduled recurring report.",
            timestamp=t0 + timedelta(seconds=5),
        )
        sched1.register_scheduled_task(
            RegisterScheduledTaskRequest(
                title="Unapproved schedule stays blocked",
                description="Missing approval ID",
                task_id=_seed_queued_task(h1, title="No approval schedule", timestamp=t0),
                provider_id="antigravity",
                connection_id="agy_conc",
                timezone_name="America/New_York",
                scheduled_at=t0 + timedelta(seconds=20),
                requires_approval=True,
                approval_id=None,
                policy_note="Must not run without operator approval",
                created_at=t0,
            ),
            auth_context=h1.alice,
        )
        sched1.register_scheduled_task(
            RegisterScheduledTaskRequest(
                title="Approved recurring schedule",
                description="Daily report trigger",
                task_id=tid_sched,
                provider_id="antigravity",
                connection_id="agy_conc",
                timezone_name="Asia/Ho_Chi_Minh",
                scheduled_at=t0 + timedelta(seconds=20),
                recurrence_rule="DAILY",
                recurrence_interval_seconds=86400,
                requires_approval=True,
                approval_id=apr_id,
                policy_note="Approved recurring execution",
                created_at=t0,
            ),
            auth_context=h1.alice,
        )

        # Worker denied from mutating scheduler queues or schedules.
        with pytest.raises(SchedulerGovernanceError):
            sched1.reserve_next_execution(
                worker_id="it.worker",
                now=t0 + timedelta(seconds=25),
                auth_context=worker_ctx("it.worker", assigned_task_ids=[tid_ok1]),
            )
    finally:
        h1.store.close()

    # Simulate restart on persisted SQLite DB!
    h2 = make_department_harness(tmp_path)
    try:
        sched2 = ExecutionSchedulerService(permission_manager=h2.permissions, scheduler_repository=h2.store.scheduler)
        # Active leases & waiting queue survive restart cleanly.
        act_after = sched2.get_active_executions(auth_context=h2.alice, timestamp=t0 + timedelta(seconds=25))
        assert act_after.total_reserved == 2

        # Expire overdue leases (TTL=30s expired by t0 + 45s) -> returns items to READY safely!
        expired = sched2.recover_expired_leases(now=t0 + timedelta(seconds=45), auth_context=h2.master)
        assert len(expired) == 2

        # Evaluate due schedules at t0 + 45s -> approved triggers, unapproved skipped!
        due_outcomes = sched2.evaluate_due_schedules(now=t0 + timedelta(seconds=45), auth_context=h2.master)
        assert sum(1 for d in due_outcomes if d.triggered) == 1
        assert any(d.skip_reason_code == "APPROVAL_REQUIRED_NOT_GRANTED" for d in due_outcomes)

        # Now worker reserves and cleanly releases one slot!
        r_after = sched2.reserve_next_execution(worker_id="it.recovery", now=t0 + timedelta(seconds=46), auth_context=h2.master)
        assert r_after.lease is not None
        released = sched2.release_execution(
            ExecutionReleaseRequest(
                lease_id=r_after.lease.lease_id,
                owner_token=r_after.lease.owner_token,
                terminal_queue_status="COMPLETED",
                reason="Verified completion after restart",
                timestamp=t0 + timedelta(seconds=47),
            ),
            auth_context=h2.master,
        )
        assert released.state == "RELEASED"
    finally:
        h2.store.close()
