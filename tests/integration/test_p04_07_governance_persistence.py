"""Persistence & Integration tests for Phase 04 / P04.07 SQLite Approval Gate & Governance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from core.contracts import Artifact, ConnectionRef, Run, Task, new_run_id, new_task_id
from core.governance import (
    ApprovalError,
    ApprovalPolicyContext,
    ApprovalService,
    PermissionDeniedError,
    PermissionManager,
    PolicyEvaluator,
    PolicyRequest,
    RoutingContext,
    TrustedAuthContext,
    compute_request_fingerprint,
)
from core.tasks import (
    FallbackEvaluationRequest,
    FallbackPolicy,
    FallbackTarget,
    WorkflowLifecycleController,
)
from storage.db import apply_migrations, get_latest_migration_version, sqlite_connection
from storage.repositories import SQLiteLifecyclePersistenceAdapter, SQLiteRepositoryStore


def _utc(hour: int = 13, minute: int = 0) -> datetime:
    """Return deterministic UTC datetime."""
    return datetime(2026, 11, 25, hour, minute, 0, tzinfo=timezone.utc)


def _make_task_and_run(store: SQLiteRepositoryStore, ts: datetime) -> tuple[Task, Run]:
    """Seed valid Task and Run in SQLite to satisfy approval FK constraints."""
    task = Task(
        task_id=new_task_id(),
        title="Governance acceptance task",
        description="Task used for P04.07 persistent approval gate verification",
        requested_by="operator.alice",
        status="RUNNING",
        priority="HIGH",
        created_at=ts,
        updated_at=ts,
        assigned_agent_ids=["core.master", "dev.backend"],
        acceptance_criteria=["Verify approval gate durability"],
    )
    store.tasks.create_task(task)
    run = Run(
        run_id=new_run_id(),
        task_id=task.task_id,
        runtime_id="custom_orchestrator",
        provider_id="antigravity",
        status="RUNNING",
        started_at=ts,
        correlation_id="corr-p0407-gov",
    )
    store.runs.create_run(run)
    return task, run


def _init_db(db_file: Path) -> int:
    """Apply SQLite schema migrations to ``db_file`` and return applied version."""
    with sqlite_connection(db_file) as conn:
        result = apply_migrations(conn)
        return result.current_version


def test_migration_0003_creates_approval_requests_table_and_indexes(tmp_path: Path) -> None:
    """Verify schema migration v3 creates approval_requests and its indexes cleanly."""
    db_file = tmp_path / "gov_v3.db"
    version = _init_db(db_file)
    assert version == get_latest_migration_version()
    assert version >= 3

    with sqlite_connection(db_file) as conn:
        table_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'approval_requests';"
        ).fetchone()
        assert table_row is not None
        idx_names = {
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'approval_requests';"
            ).fetchall()
        }
        assert {
            "idx_approvals_status",
            "idx_approvals_task_run",
            "idx_approvals_fingerprint",
        }.issubset(idx_names)


def test_approval_lifecycle_persistence_security_single_use_and_restart(tmp_path: Path) -> None:
    """Verify approval request/approve/reject/expire/tamper/reuse/rollback/restart invariants."""
    db_file = tmp_path / "approval_lifecycle.db"
    _init_db(db_file)

    ts = _utc(10, 0)
    requester = TrustedAuthContext(
        actor_id="core.master",
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )
    approver = TrustedAuthContext(
        actor_id="operator.alice",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )
    worker = TrustedAuthContext(
        actor_id="dev.backend",
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
    )

    with SQLiteRepositoryStore(db_file) as store:
        task, run = _make_task_and_run(store, ts)
        evaluator = PolicyEvaluator()
        approvals = ApprovalService(store.approvals, store.events)
        pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)

        target_res = f"task:{task.task_id}/artifacts/stale.tmp"
        params = {"artifact_id": "art_stale_01", "force": True}
        req_fp = compute_request_fingerprint(
            action="trash.purge",
            resource=target_res,
            task_id=task.task_id,
            run_id=run.run_id,
            parameters=params,
        )

        # 1) Create PENDING approval by master_agent with policy version snapshot.
        pending = approvals.create_approval_request(
            requester=requester,
            action="trash.purge",
            target_resource=target_res,
            task_id=task.task_id,
            run_id=run.run_id,
            request_fingerprint=req_fp,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions=evaluator.policy_snapshot(["gov.require_approval_sensitive_admin_ops"]),
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Purge temporary build artifact from trash",
            requested_at=ts,
            expires_at=ts + timedelta(minutes=30),
        )
        assert pending.status == "PENDING"
        assert pending.policy_snapshot.rule_versions["gov.require_approval_sensitive_admin_ops"] == "1.0.0"

        # 2) Self-approval by requester is forbidden.
        master_spoof_kind = TrustedAuthContext.model_construct(
            actor_id="core.master",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
            allowed_actions=[],
            allowed_tools=[],
            assigned_task_ids=[],
        )
        with pytest.raises(ApprovalError) as self_err:
            approvals.decide_approval(
                pending.approval_id,
                decider=master_spoof_kind,
                approved=True,
                reason="Trying to self approve",
                timestamp=ts + timedelta(minutes=1),
            )
        assert self_err.value.error_code == "SELF_APPROVAL_FORBIDDEN"

        # 3) Worker/Agent cannot approve requests.
        with pytest.raises(ApprovalError) as worker_err:
            approvals.decide_approval(
                pending.approval_id,
                decider=worker,
                approved=True,
                reason="Worker trying to approve",
                timestamp=ts + timedelta(minutes=1),
            )
        assert worker_err.value.error_code == "UNAUTHORIZED_APPROVER"

        # 4) Reject flow blocks execution cleanly.
        rejected_req = approvals.create_approval_request(
            requester=requester,
            action="settings.update",
            target_resource="settings:global",
            task_id=task.task_id,
            run_id=run.run_id,
            request_fingerprint=compute_request_fingerprint(
                action="settings.update",
                resource="settings:global",
                task_id=task.task_id,
                run_id=run.run_id,
                parameters={"mode": "turbo"},
            ),
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions=evaluator.policy_snapshot(["gov.require_approval_sensitive_admin_ops"]),
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Change global runtime setting",
            requested_at=ts,
        )
        approvals.decide_approval(
            rejected_req.approval_id,
            decider=approver,
            approved=False,
            reason="Unsafe setting change",
            timestamp=ts + timedelta(minutes=2),
        )
        with pytest.raises(ApprovalError) as rej_err:
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=approver,
                    action="settings.update",
                    resource="settings:global",
                    task_id=task.task_id,
                    run_id=run.run_id,
                    approval_id=rejected_req.approval_id,
                    parameters={"mode": "turbo"},
                    timestamp=ts + timedelta(minutes=3),
                ),
                lambda: "never_runs",
            )
        assert rej_err.value.error_code == "APPROVAL_REJECTED"

        # 5) Human operator approves pending request -> execute after restart!
        decision = approvals.decide_approval(
            pending.approval_id,
            decider=approver,
            approved=True,
            reason="Verified safe artifact cleanup",
            timestamp=ts + timedelta(minutes=4),
        )
        assert decision.status == "APPROVED"

    # Simulate process restart: open fresh SQLiteRepositoryStore!
    with SQLiteRepositoryStore(db_file) as restarted:
        evaluator = PolicyEvaluator()
        approvals = ApprovalService(restarted.approvals, restarted.events)
        pm = PermissionManager(evaluator=evaluator, journal=restarted.events, approval_service=approvals)

        loaded = restarted.approvals.get_approval(pending.approval_id)
        assert loaded is not None
        assert loaded.status == "APPROVED"
        assert loaded.decided_by == "operator.alice"
        assert loaded.policy_snapshot.rule_versions["gov.require_approval_sensitive_admin_ops"] == "1.0.0"

        target_res = f"task:{task.task_id}/artifacts/stale.tmp"
        params = {"artifact_id": "art_stale_01", "force": True}

        # 6) Tampered parameters after approval -> fingerprint mismatch blocked!
        with pytest.raises(ApprovalError) as fp_err:
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=approver,
                    action="trash.purge",
                    resource=target_res,
                    task_id=task.task_id,
                    run_id=run.run_id,
                    approval_id=pending.approval_id,
                    parameters={"artifact_id": "art_DIFFERENT", "force": True},
                    timestamp=ts + timedelta(minutes=5),
                ),
                lambda: "never_runs",
            )
        assert fp_err.value.error_code == "APPROVAL_FINGERPRINT_MISMATCH"

        # 7) Handler exception triggers atomic rollback of both DB mutation and CONSUMED status!
        before_event_count = len(restarted.events.read_entries(task_id=task.task_id))

        def _mutating_then_crashing_handler() -> str:
            restarted.metadata.save_connection(
                ConnectionRef(
                    provider_id="antigravity",
                    connection_id="conn_tmp",
                    model_id="gemini-2.5-pro",
                    credential_ref="env:AGY_TMP",
                )
            )
            raise RuntimeError("simulated crash")

        with pytest.raises(RuntimeError, match="simulated crash"):
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=approver,
                    action="trash.purge",
                    resource=target_res,
                    task_id=task.task_id,
                    run_id=run.run_id,
                    approval_id=pending.approval_id,
                    parameters=params,
                    timestamp=ts + timedelta(minutes=6),
                ),
                _mutating_then_crashing_handler,
            )
        after_rollback = restarted.approvals.get_approval(pending.approval_id)
        assert after_rollback is not None
        assert after_rollback.status == "APPROVED"
        assert restarted.metadata.get_connection("antigravity", "conn_tmp") is None
        # Only evaluate_and_audit's pre-consumption event was committed; APPROVAL_CONSUMED rolled back.
        after_events = restarted.events.read_entries(task_id=task.task_id)
        assert len(after_events) == before_event_count + 1
        assert all(
            e.event.redacted_payload.get("outcome") != "APPROVAL_CONSUMED" for e in after_events
        )

        # 8) Valid dispatch executes handler ONCE and marks CONSUMED atomically.
        executions: list[str] = []
        dec, res = pm.dispatch_protected_action(
            PolicyRequest(
                auth_context=approver,
                action="trash.purge",
                resource=target_res,
                task_id=task.task_id,
                run_id=run.run_id,
                approval_id=pending.approval_id,
                parameters=params,
                timestamp=ts + timedelta(minutes=7),
            ),
            lambda: (executions.append("purged"), "PURGE_OK")[1],
        )
        assert dec.decision == "APPROVAL_REQUIRED"
        assert res == "PURGE_OK"
        assert executions == ["purged"]

        consumed = restarted.approvals.get_approval(pending.approval_id)
        assert consumed is not None
        assert consumed.status == "CONSUMED"
        assert consumed.consumed_event_id is not None

        # 9) Replay/reuse of single-use approval fails closed without running handler again.
        with pytest.raises(ApprovalError) as replay_err:
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=approver,
                    action="trash.purge",
                    resource=target_res,
                    task_id=task.task_id,
                    run_id=run.run_id,
                    approval_id=pending.approval_id,
                    parameters=params,
                    timestamp=ts + timedelta(minutes=8),
                ),
                lambda: executions.append("replay_must_fail"),
            )
        assert replay_err.value.error_code == "APPROVAL_ALREADY_CONSUMED"
        assert executions == ["purged"]


def test_expired_pending_and_expired_approved_approvals_plus_system_deny_non_overridable(
    tmp_path: Path,
) -> None:
    """Verify expiration on both PENDING and APPROVED requests and non-overridable System DENY."""
    db_file = tmp_path / "approval_expiry.db"
    _init_db(db_file)
    ts = _utc(11, 0)

    requester = TrustedAuthContext(
        actor_id="core.master",
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )
    approver = TrustedAuthContext(
        actor_id="operator.alice",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )

    with SQLiteRepositoryStore(db_file) as store:
        task, run = _make_task_and_run(store, ts)
        evaluator = PolicyEvaluator()
        approvals = ApprovalService(store.approvals, store.events)
        pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)

        # Case A: PENDING expires before human decides.
        exp_pending = approvals.create_approval_request(
            requester=requester,
            action="settings.update",
            target_resource="settings:timeout",
            task_id=task.task_id,
            run_id=run.run_id,
            request_fingerprint=compute_request_fingerprint(
                action="settings.update",
                resource="settings:timeout",
                task_id=task.task_id,
                run_id=run.run_id,
                parameters={"seconds": 60},
            ),
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Adjust timeout setting",
            requested_at=ts,
            expires_at=ts + timedelta(minutes=5),
        )
        with pytest.raises(ApprovalError) as err_a:
            approvals.decide_approval(
                exp_pending.approval_id,
                decider=approver,
                approved=True,
                reason="Too late",
                timestamp=ts + timedelta(minutes=6),
            )
        assert err_a.value.error_code == "APPROVAL_EXPIRED"
        assert store.approvals.get_approval(exp_pending.approval_id).status == "EXPIRED"  # type: ignore[union-attr]

        # Case B: APPROVED request expires before consumption -> APPROVED transitions cleanly to EXPIRED.
        exp_approved = approvals.create_approval_request(
            requester=requester,
            action="settings.update",
            target_resource="settings:cache",
            task_id=task.task_id,
            run_id=run.run_id,
            request_fingerprint=compute_request_fingerprint(
                action="settings.update",
                resource="settings:cache",
                task_id=task.task_id,
                run_id=run.run_id,
                parameters={"enabled": True},
            ),
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Enable cache setting",
            requested_at=ts,
            expires_at=ts + timedelta(minutes=10),
        )
        approvals.decide_approval(
            exp_approved.approval_id,
            decider=approver,
            approved=True,
            reason="Approved in window",
            timestamp=ts + timedelta(minutes=2),
        )
        with pytest.raises(ApprovalError) as err_b:
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=approver,
                    action="settings.update",
                    resource="settings:cache",
                    task_id=task.task_id,
                    run_id=run.run_id,
                    approval_id=exp_approved.approval_id,
                    parameters={"enabled": True},
                    timestamp=ts + timedelta(minutes=11),
                ),
                lambda: "never_runs",
            )
        assert err_b.value.error_code == "APPROVAL_EXPIRED"
        assert store.approvals.get_approval(exp_approved.approval_id).status == "EXPIRED"  # type: ignore[union-attr]

        # Case C: Human approval cannot override System DENY (e.g. local_only violation).
        valid_approval = approvals.create_approval_request(
            requester=requester,
            action="connection.change_provider",
            target_resource="provider:cloud_bad",
            task_id=task.task_id,
            run_id=run.run_id,
            request_fingerprint=compute_request_fingerprint(
                action="connection.change_provider",
                resource="provider:cloud_bad",
                task_id=task.task_id,
                run_id=run.run_id,
                parameters={},
            ),
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Attempt provider switch",
            requested_at=ts,
        )
        approvals.decide_approval(
            valid_approval.approval_id,
            decider=approver,
            approved=True,
            reason="Test override rejection",
            timestamp=ts + timedelta(minutes=1),
        )
        with pytest.raises(PermissionDeniedError) as sys_err:
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=approver,
                    action="connection.change_provider",
                    resource="provider:cloud_bad",
                    task_id=task.task_id,
                    run_id=run.run_id,
                    approval_id=valid_approval.approval_id,
                    routing=RoutingContext(
                        provider_id="cloud_bad",
                        data_residency="local_only",
                        target_residency="cloud",
                    ),
                    timestamp=ts + timedelta(minutes=2),
                ),
                lambda: "never_runs",
            )
        assert sys_err.value.decision.decision == "DENY"
        assert sys_err.value.decision.reason_code == "LOCAL_ONLY_VIOLATION"
        assert sys_err.value.decision.system_deny is True


def test_workflow_lifecycle_controller_enforces_governance_permission_hook(tmp_path: Path) -> None:
    """Ensure WorkflowLifecycleController honors PermissionManager hooks for cancel and fallback."""
    db_file = tmp_path / "lifecycle_gov.db"
    _init_db(db_file)
    ts = _utc(12, 0)

    with SQLiteRepositoryStore(db_file) as store:
        task, run = _make_task_and_run(store, ts)
        pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events)
        adapter = SQLiteLifecyclePersistenceAdapter(store)

        # Worker hook blocks task/runtime cancellation & cross-provider fallback.
        worker_ctx = TrustedAuthContext(
            actor_id="dev.backend",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
        )
        worker_ctrl = WorkflowLifecycleController(
            adapter, permission_hook=pm.build_lifecycle_hook(worker_ctx)
        )
        cancel_res = worker_ctrl.request_cancel(
            task.task_id,
            run.run_id,
            actor_id="dev.backend",
            actor_kind="worker_agent",
            timestamp=ts + timedelta(seconds=10),
            reason="Worker should not cancel task",
        )
        assert cancel_res.status == "PERMISSION_DENIED"
        assert cancel_res.cancel_confirmed is False

        # Master hook blocks unattended cross-provider fallback because connection.change_provider requires approval.
        master_ctx = TrustedAuthContext(
            actor_id="core.master",
            actor_kind="master_agent",
            auth_source="application_boundary",
            authenticated=True,
        )
        master_ctrl = WorkflowLifecycleController(
            adapter, permission_hook=pm.build_lifecycle_hook(master_ctx)
        )
        candidate_target = FallbackTarget(
            provider_id="openai",
            connection_id="conn_backup",
            model_id="gpt-5",
            data_residency="cloud",
        )
        fb_decision = master_ctrl.evaluate_and_record_fallback(
            FallbackPolicy(
                fallback_allowed=True,
                max_retries=2,
                allowed_targets=[candidate_target],
                allow_provider_switch=True,
                allow_data_residency_change=True,
            ),
            FallbackEvaluationRequest(
                task_id=task.task_id,
                run_id=run.run_id,
                actor_id="core.master",
                attempt_number=1,
                operation_is_idempotent=True,
                failure_reason_code="PROVIDER_TIMEOUT",
                current_target=FallbackTarget(
                    provider_id="antigravity",
                    connection_id="conn_main",
                    model_id="gemini-2.5-pro",
                    data_residency="cloud",
                ),
                candidate_target=candidate_target,
                requested_at=ts + timedelta(seconds=20),
            ),
        )
        assert fb_decision.decision == "DENY"
        assert fb_decision.reason_code == "PERMISSION_DENIED"

        gov_events = [
            e.event
            for e in store.events.read_entries(task_id=task.task_id)
            if e.event.event_type in {"governance.decision_recorded", "workflow.fallback_evaluated"}
        ]
        assert len(gov_events) >= 3
