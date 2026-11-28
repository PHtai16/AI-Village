"""Unit, Governance, Truthfulness, Budget/Quota Alerts, Concurrency & Recovery tests for P11.04."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import sqlite3

from pydantic import ValidationError
import pytest

from core.departments import (
    RecordResourceUsageRequest,
    RegisterBudgetPolicyRequest,
    ResourceLedgerConflictError,
    ResourceLedgerGovernanceError,
    ResourceLedgerQuery,
    ResourceLedgerReferenceError,
    ResourcesDepartmentService,
    derive_quota_bucket_key,
)
from storage.repositories import SQLiteRepositoryStore, SQLiteResourcesRepository
from tests.departments.conftest import (
    REPO_ROOT,
    DepartmentTestHarness,
    create_task_and_run,
    make_department_harness,
    utc,
    worker_ctx,
)


def _new_resources(
    h: DepartmentTestHarness,
    *,
    repo: SQLiteResourcesRepository | None = None,
) -> ResourcesDepartmentService:
    return ResourcesDepartmentService(
        permission_manager=h.permissions,
        journal=h.store.events,
        metadata_repo=h.store.tasks,
        ledger_repository=repo or h.store.resources,
    )


def test_p11_04_ast_purity_and_migration_0019_immutability(tmp_path: Path) -> None:
    """Verify AST boundary purity for resources modules and append-only triggers in migration 0019."""
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
    for rel_path in (
        "core/departments/resources_models.py",
        "core/departments/resources_ledger.py",
        "core/departments/resources.py",
    ):
        tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), f"{rel_path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden), f"{rel_path} imports from {node.module}"

    h = make_department_harness(tmp_path)
    try:
        t0 = utc(11, 0)
        task_id, run_id = create_task_and_run(
            h,
            title="P11.04 Trigger Check",
            description="Ensure resource_ledger_entries is append-only.",
            timestamp=t0,
        )
        res = _new_resources(h)
        out = res.record_usage(
            RecordResourceUsageRequest(
                task_id=task_id,
                run_id=run_id,
                provider_id="antigravity",
                connection_id="agy_main",
                model_id="gemini-2.5-pro",
                duration_ms=250,
                duration_source="PROVIDER_MEASURED",
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h.master,
        )
        with h.store.open_connection() as conn:
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(
                    "UPDATE resource_ledger_entries SET duration_ms = 999 WHERE ledger_entry_id = ?;",
                    (out.entry.ledger_entry_id,),
                )
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(
                    "DELETE FROM resource_ledger_entries WHERE ledger_entry_id = ?;",
                    (out.entry.ledger_entry_id,),
                )
    finally:
        h.store.close()


def test_p11_04_truthful_telemetry_unavailable_vs_estimated_vs_actual(tmp_path: Path) -> None:
    """Reject fabricated zeros/unlabeled estimates and separate actual, estimated, and unavailable metrics."""
    # Validation must forbid mixing or fabricating missing metrics.
    with pytest.raises(ValidationError):
        RecordResourceUsageRequest(
            task_id="task_1234567890abcdef",
            provider_id="google_gemini",
            duration_ms=0,
            duration_source="UNAVAILABLE",
            timestamp=utc(11, 5),
        )
    with pytest.raises(ValidationError):
        RecordResourceUsageRequest(
            task_id="task_1234567890abcdef",
            provider_id="google_gemini",
            total_tokens=0,
            token_usage_status="UNAVAILABLE_FROM_PROVIDER",
            timestamp=utc(11, 5),
        )
    with pytest.raises(ValidationError):
        RecordResourceUsageRequest(
            task_id="task_1234567890abcdef",
            provider_id="google_gemini",
            actual_cost_usd=0.05,
            cost_source="ESTIMATED_LOCAL_PRICING_TABLE",
            pricing_table_ref="tbl_1",
            is_estimated=True,
            estimate_label="ESTIMATED",
            timestamp=utc(11, 5),
        )
    with pytest.raises(ValidationError):
        RecordResourceUsageRequest(
            task_id="task_1234567890abcdef",
            provider_id="google_gemini",
            estimated_cost_usd=0.05,
            cost_source="ESTIMATED_LOCAL_PRICING_TABLE",
            pricing_table_ref="tbl_1",
            is_estimated=False,
            timestamp=utc(11, 5),
        )

    h = make_department_harness(tmp_path)
    try:
        t0 = utc(11, 10)
        tid_unavail, rid_unavail = create_task_and_run(
            h,
            title="Provider Telemetry Unavailable",
            description="Ensure unavailable provider tokens/cost remain None rather than 0.",
            timestamp=t0,
        )
        tid_mixed, rid_mixed = create_task_and_run(
            h,
            title="Mixed Billed Free & Estimated Runs",
            description="Separate verified provider billed vs local zero vs labelled estimate.",
            timestamp=t0,
        )
        res = _new_resources(h)

        # 1. Provider returned unavailable tokens & cost, while host measured duration & memory.
        unavail_out = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid_unavail,
                run_id=rid_unavail,
                provider_id="antigravity",
                connection_id="agy_primary",
                model_id="agy-default",
                duration_ms=420,
                duration_source="HOST_SAMPLED",
                memory_mb=128.5,
                peak_memory_mb=140.0,
                memory_source="HOST_SAMPLED",
                token_usage_status="UNAVAILABLE_FROM_PROVIDER",
                cost_source="UNAVAILABLE_FROM_PROVIDER",
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        assert unavail_out.entry.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
        assert unavail_out.entry.input_tokens is None
        assert unavail_out.entry.actual_cost_usd is None
        assert unavail_out.entry.estimated_cost_usd is None

        sum_unavail = res.get_task_resource_summary(
            tid_unavail, auth_context=h.alice, timestamp=t0 + timedelta(seconds=3)
        )
        assert sum_unavail.actual_cost_usd_total is None
        assert sum_unavail.estimated_cost_usd_total is None
        assert sum_unavail.measured_total_tokens_total is None
        assert sum_unavail.measured_duration_ms_total == 420
        assert sum_unavail.peak_memory_mb_max == pytest.approx(140.0)
        assert sum_unavail.has_unavailable_cost_entries is True
        assert sum_unavail.has_unavailable_token_entries is True
        assert sum_unavail.providers == ["antigravity"]
        assert sum_unavail.connections == ["agy_primary"]
        assert sum_unavail.models == ["agy-default"]

        # 2. Mixed: actual billed + free local zero + labelled estimated cost.
        res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid_mixed,
                run_id=rid_mixed,
                provider_id="google_gemini",
                connection_id="gemini_billed",
                model_id="gemini-2.5-pro",
                duration_ms=800,
                duration_source="PROVIDER_MEASURED",
                input_tokens=1000,
                output_tokens=500,
                total_tokens=1500,
                token_usage_status="VERIFIED_MEASURED",
                actual_cost_usd=0.25,
                cost_source="PROVIDER_BILLED",
                timestamp=t0 + timedelta(seconds=4),
            ),
            auth_context=h.master,
        )
        res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid_mixed,
                run_id=rid_mixed,
                provider_id="ollama",
                connection_id="ollama_local",
                model_id="qwen2.5-coder",
                duration_ms=300,
                duration_source="ADAPTER_MEASURED",
                memory_mb=512.0,
                peak_memory_mb=640.0,
                memory_source="CONTAINER_MEASURED",
                total_tokens=400,
                token_usage_status="VERIFIED_MEASURED",
                actual_cost_usd=0.0,
                cost_source="LOCAL_FREE_ZERO",
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=h.master,
        )
        res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid_mixed,
                run_id=rid_mixed,
                provider_id="google_gemini",
                connection_id="gemini_est",
                model_id="gemini-2.5-flash",
                duration_ms=200,
                duration_source="ADAPTER_MEASURED",
                total_tokens=600,
                token_usage_status="ESTIMATED",
                estimated_cost_usd=0.10,
                cost_source="ESTIMATED_LOCAL_PRICING_TABLE",
                pricing_table_ref="gemini-2026-11",
                is_estimated=True,
                estimate_label="ESTIMATED via local pricing table gemini-2026-11",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=h.master,
        )

        sum_mixed = res.get_task_resource_summary(
            tid_mixed, auth_context=h.alice, timestamp=t0 + timedelta(seconds=7)
        )
        assert sum_mixed.actual_cost_usd_total == pytest.approx(0.25)
        assert sum_mixed.estimated_cost_usd_total == pytest.approx(0.10)
        assert sum_mixed.has_estimated_entries is True
        assert sum_mixed.estimate_labels == ["ESTIMATED via local pricing table gemini-2026-11"]
        assert sum_mixed.measured_total_tokens_total == 2500
        assert sum_mixed.measured_duration_ms_total == 1300
        assert sum_mixed.peak_memory_mb_max == pytest.approx(640.0)
        assert set(sum_mixed.providers) == {"google_gemini", "ollama"}
        assert set(sum_mixed.connections) == {"gemini_billed", "ollama_local", "gemini_est"}
        assert set(sum_mixed.models) == {"gemini-2.5-pro", "qwen2.5-coder", "gemini-2.5-flash"}
    finally:
        h.store.close()


def test_p11_04_shared_and_unproven_quota_scopes_trigger_combined_quota_alerts(tmp_path: Path) -> None:
    """Two keys in the same project or without proof share quota alerts rather than isolating quotas."""
    b_shared_a, sk_a, pr_a = derive_quota_bucket_key(
        provider_id="google_gemini",
        connection_id="key_1",
        project_ref="proj_ai",
        account_ref=None,
        quota_scope_kind="CONNECTION_VERIFIED",
        independent_quota_proof=False,
    )
    b_shared_b, sk_b, pr_b = derive_quota_bucket_key(
        provider_id="google_gemini",
        connection_id="key_2",
        project_ref="proj_ai",
        account_ref=None,
        quota_scope_kind="PROJECT_SHARED",
        independent_quota_proof=False,
    )
    assert b_shared_a == b_shared_b == "project:google_gemini:proj_ai"
    assert sk_a == sk_b == "PROJECT_SHARED"
    assert pr_a is False and pr_b is False

    b_verified, sk_v, pr_v = derive_quota_bucket_key(
        provider_id="google_gemini",
        connection_id="verified_enterprise",
        project_ref="proj_enterprise",
        account_ref=None,
        quota_scope_kind="CONNECTION_VERIFIED",
        independent_quota_proof=True,
    )
    assert b_verified == "conn:google_gemini:verified_enterprise"
    assert sk_v == "CONNECTION_VERIFIED"
    assert pr_v is True

    h = make_department_harness(tmp_path)
    try:
        t0 = utc(11, 20)
        tid1, rid1 = create_task_and_run(h, title="Quota Task 1", description="Key 1 call", timestamp=t0)
        tid2, rid2 = create_task_and_run(h, title="Quota Task 2", description="Key 2 call", timestamp=t0)
        res = _new_resources(h)

        # Register quota threshold on shared bucket (warning at 80%, critical at 100% of 100 units).
        pol = res.register_budget_policy(
            RegisterBudgetPolicyRequest(
                scope_kind="QUOTA_BUCKET",
                scope_ref="project:google_gemini:proj_ai",
                provider_id="google_gemini",
                project_ref="proj_ai",
                quota_scope_kind="PROJECT_SHARED",
                independent_quota_proof=False,
                quota_units_limit=100,
                warning_ratio=0.80,
                critical_ratio=1.00,
                policy_note="Shared Gemini project quota alert policy.",
                timestamp=t0,
            ),
            auth_context=h.alice,
        )
        assert pol.quota_bucket_key == "project:google_gemini:proj_ai"

        # Task 1 consumes 50 units on key_1 -> no alert yet.
        o1 = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid1,
                run_id=rid1,
                provider_id="google_gemini",
                connection_id="key_1",
                model_id="gemini-2.5-flash",
                project_ref="proj_ai",
                quota_scope_kind="PROJECT_SHARED",
                quota_units_used=50,
                quota_limit_units=100,
                quota_remaining_units=50,
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h.master,
        )
        assert o1.triggered_alerts == []

        # Task 2 uses SECOND key `key_2` in the same project for 35 units -> combined 85/100 triggers WARNING!
        o2 = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid2,
                run_id=rid2,
                provider_id="google_gemini",
                connection_id="key_2",
                model_id="gemini-2.5-flash",
                project_ref="proj_ai",
                quota_scope_kind="CONNECTION_VERIFIED",
                independent_quota_proof=False,
                quota_units_used=35,
                quota_limit_units=100,
                quota_remaining_units=15,
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        assert len(o2.triggered_alerts) == 1
        w_alert = o2.triggered_alerts[0].alert
        assert w_alert.alert_category == "QUOTA"
        assert w_alert.severity == "WARNING"
        assert "SHARED OR UNPROVEN QUOTA BUCKET" in w_alert.summary
        assert w_alert.notification_id is not None

        # Task 2 calls again for 20 units -> combined 105/100 escalates to CRITICAL + Action Inbox for Master!
        o3 = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid2,
                run_id=rid2,
                provider_id="google_gemini",
                connection_id="key_2",
                model_id="gemini-2.5-flash",
                project_ref="proj_ai",
                quota_scope_kind="PROJECT_SHARED",
                quota_units_used=20,
                quota_limit_units=100,
                quota_remaining_units=0,
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=h.master,
        )
        assert len(o3.triggered_alerts) == 1
        c_outcome = o3.triggered_alerts[0]
        assert c_outcome.alert.alert_id == w_alert.alert_id
        assert c_outcome.escalated_to_critical is True
        assert c_outcome.alert.severity == "CRITICAL"
        assert c_outcome.alert.master_notified is True
        assert c_outcome.alert.action_inbox_item_id is not None

        bsum = res.get_bucket_quota_summary(
            "project:google_gemini:proj_ai",
            auth_context=h.alice,
            timestamp=t0 + timedelta(seconds=4),
        )
        assert bsum is not None
        assert bsum.quota_independence_proven is False
        assert bsum.quota_independence_status == "QUOTA_SHARED_OR_UNPROVEN"
        assert bsum.quota_units_used_total == 105
        assert set(bsum.connections) == {"key_1", "key_2"}
        assert set(bsum.task_ids) == {tid1, tid2}
        assert bsum.open_alert_count == 1
    finally:
        h.store.close()


def test_p11_04_budget_warning_dedup_escalation_recovery_and_non_destructive_safety(tmp_path: Path) -> None:
    """Verify alert dedup, escalation, estimate labeling, non-destruction of Task/Run, ACK/RESOLVE & recovery."""
    h = make_department_harness(tmp_path)
    try:
        t0 = utc(11, 30)
        tid, rid = create_task_and_run(
            h,
            title="Budget Guarded Execution",
            description="Must warn and notify Master without cancelling running Task.",
            timestamp=t0,
        )
        res = _new_resources(h)
        pol = res.register_budget_policy(
            RegisterBudgetPolicyRequest(
                scope_kind="TASK",
                scope_ref=tid,
                provider_id="antigravity",
                connection_id="agy_main",
                model_id="gemini-2.5-pro",
                budget_cost_usd_limit=1.00,
                warning_ratio=0.80,
                critical_ratio=1.00,
                policy_note="Cap task spend at $1.00 pending operator approval.",
                timestamp=t0,
            ),
            auth_context=h.alice,
        )

        # 1. Cross WARNING with labelled estimate ($0.85 / $1.00).
        out1 = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid,
                run_id=rid,
                provider_id="antigravity",
                connection_id="agy_main",
                model_id="gemini-2.5-pro",
                estimated_cost_usd=0.85,
                cost_source="ESTIMATED_LOCAL_PRICING_TABLE",
                pricing_table_ref="agy-2026-11",
                is_estimated=True,
                estimate_label="ESTIMATED local rate card agy-2026-11",
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h.master,
        )
        assert len(out1.triggered_alerts) == 1
        a1 = out1.triggered_alerts[0].alert
        assert a1.severity == "WARNING"
        assert a1.is_estimated_basis is True
        assert "ESTIMATED BASIS" in a1.summary
        assert out1.triggered_alerts[0].notification_suppressed is False

        # 2. Second step remains in WARNING range ($0.90) -> DEDUPED (no duplicate notification!).
        out2 = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid,
                run_id=rid,
                provider_id="antigravity",
                connection_id="agy_main",
                model_id="gemini-2.5-pro",
                estimated_cost_usd=0.05,
                cost_source="ESTIMATED_LOCAL_PRICING_TABLE",
                pricing_table_ref="agy-2026-11",
                is_estimated=True,
                estimate_label="ESTIMATED local rate card agy-2026-11",
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        assert len(out2.triggered_alerts) == 1
        assert out2.triggered_alerts[0].newly_created is False
        assert out2.triggered_alerts[0].notification_suppressed is True
        assert out2.triggered_alerts[0].alert.alert_id == a1.alert_id
        assert out2.triggered_alerts[0].alert.occurrence_count == 2

        with h.store.open_connection() as conn:
            notif_cnt_warn = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE task_id = ? AND notification_type = 'RESOURCE_WARNING';",
                (tid,),
            ).fetchone()[0]
        assert int(notif_cnt_warn) == 1

        # 3. Cross CRITICAL ($1.05) -> escalates to CRITICAL + notifies Master + creates Action Inbox item.
        out3 = res.record_usage(
            RecordResourceUsageRequest(
                task_id=tid,
                run_id=rid,
                provider_id="antigravity",
                connection_id="agy_main",
                model_id="gemini-2.5-pro",
                actual_cost_usd=0.15,
                cost_source="PROVIDER_BILLED",
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=h.master,
        )
        assert out3.triggered_alerts[0].escalated_to_critical is True
        crit = out3.triggered_alerts[0].alert
        assert crit.severity == "CRITICAL"
        assert crit.occurrence_count == 3
        assert crit.master_notified is True
        assert crit.requires_operator_approval is True
        assert crit.action_inbox_item_id is not None

        # Verify Task & Run were NOT forced to CANCELLED/FAILED by budget alert!
        task_after = h.store.resources.get_task(tid)
        assert task_after is not None and task_after.status == "RUNNING"

        # 4. ACK with optimistic locking + stale version rejection.
        acked = res.acknowledge_resource_alert(
            crit.alert_id,
            expected_record_version=crit.record_version,
            note="Master reviewed cost spike and is pausing next expensive step.",
            timestamp=t0 + timedelta(seconds=4),
            auth_context=h.master,
        )
        assert acked.status == "ACKNOWLEDGED"
        with pytest.raises(ResourceLedgerConflictError) as stale_exc:
            res.resolve_resource_alert(
                crit.alert_id,
                expected_record_version=crit.record_version,
                note="stale version",
                timestamp=t0 + timedelta(seconds=5),
                auth_context=h.alice,
            )
        assert stale_exc.value.error_code == "STALE_ALERT_VERSION"

        # 5. Raising policy budget limit recovers open/acknowledged alert below warning threshold automatically!
        res.register_budget_policy(
            RegisterBudgetPolicyRequest(
                policy_id=pol.policy_id,
                scope_kind="TASK",
                scope_ref=tid,
                provider_id="antigravity",
                connection_id="agy_main",
                model_id="gemini-2.5-pro",
                budget_cost_usd_limit=5.00,
                warning_ratio=0.80,
                critical_ratio=1.00,
                policy_note="Operator expanded task budget to $5.00.",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=h.alice,
        )
        open_alerts = res.list_resource_alerts(
            scope_ref=tid,
            include_resolved=False,
            auth_context=h.alice,
            timestamp=t0 + timedelta(seconds=7),
        )
        assert open_alerts == []
        all_alerts = res.list_resource_alerts(
            scope_ref=tid,
            include_resolved=True,
            auth_context=h.alice,
            timestamp=t0 + timedelta(seconds=7),
        )
        assert len(all_alerts) == 1 and all_alerts[0].status == "RESOLVED"
    finally:
        h.store.close()


def test_p11_04_idempotency_concurrency_rollback_restart_security_and_secret_scrub(tmp_path: Path) -> None:
    """Verify idempotency, worker/actor security, secret scrubbing, rollback, concurrency & restart persistence."""
    h1 = make_department_harness(tmp_path)
    try:
        t0 = utc(11, 40)
        tid, rid = create_task_and_run(
            h1,
            title="Ledger Security & Concurrency",
            description="Test RBAC, redaction, idempotency, concurrency, rollback, and restart.",
            timestamp=t0,
            assigned_agent_ids=["it.worker"],
        )
        tid_other, _ = create_task_and_run(
            h1,
            title="Other Unassigned Task",
            description="Worker must not log against unassigned task.",
            timestamp=t0,
            assigned_agent_ids=["qa.reviewer"],
        )
        res1 = _new_resources(h1)

        # Worker security checks: denied policy management, alert resolution, unassigned task, and identity spoof.
        w_ctx = worker_ctx("it.worker", assigned_task_ids=[tid])
        with pytest.raises(ResourceLedgerGovernanceError):
            res1.register_budget_policy(
                RegisterBudgetPolicyRequest(
                    scope_kind="TASK",
                    scope_ref=tid,
                    provider_id="antigravity",
                    budget_cost_usd_limit=10.0,
                    policy_note="Worker unauthorized policy.",
                    timestamp=t0,
                ),
                auth_context=w_ctx,
            )
        with pytest.raises(ResourceLedgerGovernanceError):
            res1.record_usage(
                RecordResourceUsageRequest(
                    task_id=tid_other,
                    provider_id="antigravity",
                    duration_ms=10,
                    duration_source="ADAPTER_MEASURED",
                    timestamp=t0,
                ),
                auth_context=w_ctx,
            )
        with pytest.raises(ResourceLedgerGovernanceError):
            res1.record_usage(
                RecordResourceUsageRequest(
                    task_id=tid,
                    run_id=rid,
                    agent_id="qa.reviewer",
                    provider_id="antigravity",
                    duration_ms=10,
                    duration_source="ADAPTER_MEASURED",
                    timestamp=t0,
                ),
                auth_context=w_ctx,
            )

        # Missing Task/Run fails cleanly.
        with pytest.raises(ResourceLedgerReferenceError):
            res1.record_usage(
                RecordResourceUsageRequest(
                    task_id="task_missing00000000",
                    provider_id="antigravity",
                    duration_ms=10,
                    duration_source="ADAPTER_MEASURED",
                    timestamp=t0,
                ),
                auth_context=h1.master,
            )

        # Secret scrubbing in notes + raw secret keys rejected in metadata.
        with pytest.raises(ValidationError):
            RecordResourceUsageRequest(
                task_id=tid,
                provider_id="openai",
                metadata={"token": "sk-SuperSecretOpenAIKey1234567890"},
                timestamp=t0,
            )

        res1.register_budget_policy(
            RegisterBudgetPolicyRequest(
                scope_kind="QUOTA_BUCKET",
                scope_ref="project:google_gemini:proj_conc",
                provider_id="google_gemini",
                project_ref="proj_conc",
                quota_scope_kind="PROJECT_SHARED",
                quota_units_limit=100,
                warning_ratio=0.50,
                critical_ratio=0.90,
                policy_note="Concurrent quota check.",
                timestamp=t0,
            ),
            auth_context=h1.alice,
        )

        # Idempotency exact replay & payload mismatch conflict.
        req_idem = RecordResourceUsageRequest(
            task_id=tid,
            run_id=rid,
            agent_id="it.worker",
            provider_id="google_gemini",
            connection_id="conn_conc",
            model_id="gemini-2.5-flash",
            project_ref="proj_conc",
            quota_scope_kind="PROJECT_SHARED",
            quota_units_used=15,
            duration_ms=120,
            duration_source="PROVIDER_MEASURED",
            idempotency_key="step-idem-1",
            note="Used sk-LiveSecretShouldScrub1234567890 during retry.",
            timestamp=t0 + timedelta(seconds=1),
        )
        first = res1.record_usage(req_idem, auth_context=w_ctx)
        replay = res1.record_usage(req_idem, auth_context=w_ctx)
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert replay.entry.ledger_entry_id == first.entry.ledger_entry_id
        assert first.entry.note is not None and "sk-LiveSecretShouldScrub" not in first.entry.note

        with pytest.raises(ResourceLedgerConflictError) as idem_exc:
            res1.record_usage(
                req_idem.model_copy(update={"quota_units_used": 99}),
                auth_context=w_ctx,
            )
        assert idem_exc.value.error_code == "IDEMPOTENCY_PAYLOAD_MISMATCH"

        # Rollback on mid-transaction DB failure leaves zero orphan ledger rows.
        def _boom(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("simulated crash before commit")

        failing_repo = SQLiteResourcesRepository(h1.store, before_commit_hook=_boom)
        res_fail = _new_resources(h1, repo=failing_repo)
        with pytest.raises(Exception, match="rolled back"):
            res_fail.record_usage(
                RecordResourceUsageRequest(
                    task_id=tid,
                    run_id=rid,
                    provider_id="google_gemini",
                    connection_id="conn_conc",
                    project_ref="proj_conc",
                    quota_scope_kind="PROJECT_SHARED",
                    quota_units_used=25,
                    idempotency_key="step-rollback-1",
                    timestamp=t0 + timedelta(seconds=2),
                ),
                auth_context=h1.master,
            )

        # Concurrent multi-threaded inserts into same quota bucket must remain atomic and exact.
        def _worker_record(idx: int) -> None:
            res1.record_usage(
                RecordResourceUsageRequest(
                    task_id=tid,
                    run_id=rid,
                    provider_id="google_gemini",
                    connection_id="conn_conc",
                    project_ref="proj_conc",
                    quota_scope_kind="PROJECT_SHARED",
                    quota_units_used=10,
                    idempotency_key=f"thread-entry-{idx}",
                    timestamp=t0 + timedelta(seconds=10 + idx),
                ),
                auth_context=h1.master,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(_worker_record, range(6)))

        # Verify worker cannot ACK/RESOLVE alert triggered above!
        open_before = res1.list_resource_alerts(
            scope_ref="project:google_gemini:proj_conc",
            auth_context=h1.alice,
            timestamp=t0 + timedelta(seconds=20),
        )
        assert len(open_before) == 1
        assert open_before[0].severity == "WARNING"  # 15 + 60 = 75% >= 50%, < 90%
        with pytest.raises(ResourceLedgerGovernanceError):
            res1.resolve_resource_alert(
                open_before[0].alert_id,
                expected_record_version=open_before[0].record_version,
                note="Worker unauthorized resolve",
                timestamp=t0 + timedelta(seconds=21),
                auth_context=w_ctx,
            )
    finally:
        h1.store.close()

    # Simulated restart on durable SQLite store confirms full persistence.
    h2 = make_department_harness(tmp_path)
    try:
        res2 = _new_resources(h2)
        entries = res2.list_resource_ledger(
            ResourceLedgerQuery(task_id=tid),
            auth_context=h2.alice,
            timestamp=t0 + timedelta(seconds=30),
        )
        assert len(entries) == 7  # 1 idempotent + 6 concurrent (rollback excluded!)
        bsum = res2.get_bucket_quota_summary(
            "project:google_gemini:proj_conc",
            auth_context=h2.alice,
            timestamp=t0 + timedelta(seconds=31),
        )
        assert bsum is not None
        assert bsum.quota_units_used_total == 75
        alerts = res2.list_resource_alerts(
            scope_ref="project:google_gemini:proj_conc",
            auth_context=h2.alice,
            timestamp=t0 + timedelta(seconds=32),
        )
        assert len(alerts) == 1
        resolved = res2.resolve_resource_alert(
            alerts[0].alert_id,
            expected_record_version=alerts[0].record_version,
            note="Operator confirmed workload safely within quota cap.",
            timestamp=t0 + timedelta(seconds=33),
            auth_context=h2.alice,
        )
        assert resolved.status == "RESOLVED"
    finally:
        h2.store.close()
