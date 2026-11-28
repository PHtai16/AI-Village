"""Unit, Governance, Persistence, Dedup, Routing & Recovery tests for Phase 11 / P11.02 Interrupts."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

from pydantic import ValidationError
import pytest

from core.contracts import (
    Session,
    Task,
    new_conversation_id,
    new_incident_id,
    new_run_id,
    new_session_id,
    new_task_id,
)
from core.departments import (
    ConversationCreateRequest,
    DepartmentGovernanceError,
    InterruptAckRequest,
    InterruptEscalateRequest,
    InterruptPersistenceError,
    InterruptReferenceError,
    InterruptResolveRequest,
    InterruptRoutingService,
    InterruptStateError,
    InterruptSubmitRequest,
    InterruptValidationError,
    ReceptionDepartmentService,
    StaleInterruptVersionError,
    UserRequestIntake,
)
from core.tasks import ExecutionEvidence, TransitionContext
from storage.db import sqlite_connection
from storage.repositories import SQLiteInterruptRepository, SQLiteRepositoryStore
from tests.departments.conftest import (
    REPO_ROOT,
    DepartmentTestHarness,
    create_task_and_run,
    make_department_harness,
    register_worker_agent,
    utc,
    worker_ctx,
)


def _new_interrupts(
    h: DepartmentTestHarness,
    *,
    repo: SQLiteInterruptRepository | None = None,
) -> InterruptRoutingService:
    return InterruptRoutingService(
        permission_manager=h.permissions,
        journal=h.store.events,
        interrupt_repository=repo or h.store.interrupts,
    )


def _seed_task_run_with_agents(
    h: DepartmentTestHarness,
    *,
    timestamp: datetime,
    assigned_agent_ids: list[str] | None = None,
) -> tuple[str, str]:
    return create_task_and_run(
        h,
        title="P11.02 Monitored Running Task",
        description="Background running task guarded by interrupt policies.",
        timestamp=timestamp,
        assigned_agent_ids=assigned_agent_ids or ["it.worker"],
    )


def test_p11_02_ast_purity_and_migration_0017_triggers(tmp_path: Path) -> None:
    """Verify AST boundaries for interrupt modules and append-only triggers on 0017 tables."""
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
    for mod_name in ("interrupt_models.py", "interrupts.py"):
        mod_path = REPO_ROOT / "core" / "departments" / mod_name
        tree = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden)

    h = make_department_harness(tmp_path)
    try:
        t0 = utc(9, 0, 0)
        register_worker_agent(
            h,
            agent_id="it.worker",
            department="it",
            skills=["code_execution"],
            timestamp=t0,
        )
        task_id, run_id = _seed_task_run_with_agents(h, timestamp=t0 + timedelta(seconds=1))
        svc = _new_interrupts(h)
        out = svc.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Unexpected checksum variance observed",
                task_id=task_id,
                run_id=run_id,
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        esc = svc.escalate_interrupt(
            InterruptEscalateRequest(
                incident_id=out.incident.incident_id,
                expected_record_version=1,
                target_severity="CRITICAL",
                reason="Escalate anomaly to test append-only escalation table",
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=h.master,
        )
        with sqlite_connection(h.db_path) as conn:
            for stmt in (
                ("UPDATE interrupt_events SET summary = 'tampered' WHERE incident_id = ?;", (out.incident.incident_id,)),
                ("DELETE FROM interrupt_events WHERE incident_id = ?;", (out.incident.incident_id,)),
                ("UPDATE interrupt_escalations SET reason = 'tampered' WHERE escalation_id = ?;", (esc.escalation_id,)),
                ("DELETE FROM interrupt_escalations WHERE escalation_id = ?;", (esc.escalation_id,)),
            ):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    conn.execute(stmt[0], stmt[1])
    finally:
        h.store.close()


def test_p11_02_classifications_separate_severity_priority_and_non_broadcast_routing(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Cover all 5 classifications, severity!=priority, agent online check, and Master critical routing."""
    t0 = utc(9, 30, 0)
    register_worker_agent(
        dept_harness,
        agent_id="it.worker",
        department="it",
        skills=["code_execution"],
        timestamp=t0,
    )
    register_worker_agent(
        dept_harness,
        agent_id="qa.verifier",
        department="qa",
        skills=["verification"],
        timestamp=t0,
    )
    task_id, run_id = _seed_task_run_with_agents(
        dept_harness,
        timestamp=t0 + timedelta(seconds=1),
        assigned_agent_ids=["it.worker"],
    )
    reception = ReceptionDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        notification_store=dept_harness.store.metadata,
        intake_repository=dept_harness.store.reception,
    )
    svc = _new_interrupts(dept_harness)

    # 1. INFORMATIONAL: status_update -> INFO + P4_LOW + only Task stakeholder, never all 9 depts, not Master-flooded.
    info_out = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="runtime",
            event_category="status_update",
            summary="Batch step 1 of 3 parsed intermediate CSV chunk.",
            task_id=task_id,
            run_id=run_id,
            timestamp=t0 + timedelta(seconds=2),
        ),
        auth_context=dept_harness.master,
    )
    assert info_out.decision.classification == "INFORMATIONAL"
    assert info_out.decision.severity == "INFO"
    assert info_out.decision.priority == "P4_LOW"
    assert info_out.decision.urgency == "LOW"
    assert info_out.incident.broadcast_all_departments is False
    assert info_out.incident.master_notified is False
    assert [t.target_id for t in info_out.incident.routing_targets] == ["it.worker"]
    # Neither agent is assumed online without an ACTIVE Session row!
    assert info_out.incident.routing_targets[0].verified_online is False
    assert info_out.incident.routing_targets[0].delivery_status == "QUEUED_FOR_ONLINE"

    # 2. ATTENTION_NEEDED: resource_warning -> WARNING + P3_NORMAL routed to resources only.
    attn_out = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="resources",
            event_category="resource_warning",
            summary="Budget utilization reached 82% of allocated threshold.",
            task_id=task_id,
            run_id=run_id,
            timestamp=t0 + timedelta(seconds=3),
        ),
        auth_context=dept_harness.alice,
    )
    assert attn_out.decision.classification == "ATTENTION_NEEDED"
    assert attn_out.decision.severity == "WARNING"
    assert attn_out.decision.priority == "P3_NORMAL"
    assert attn_out.decision.urgency == "MODERATE"
    assert {t.target_id for t in attn_out.incident.routing_targets} == {"resources"}

    # 3. ACTION_REQUIRED with specialist_question targeting offline QA agent vs online QA agent!
    # First: qa.verifier has NO active session -> verified_online MUST be False and fallback to 'qa' added!
    q_offline = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="worker_report",
            event_category="specialist_question",
            summary="Need QA confirmation on column schema tolerance before export.",
            task_id=task_id,
            run_id=run_id,
            target_department="qa",
            target_agent_id="qa.verifier",
            timestamp=t0 + timedelta(seconds=4),
        ),
        auth_context=worker_ctx("it.worker", assigned_task_ids=[task_id]),
    )
    assert q_offline.decision.classification == "ACTION_REQUIRED"
    assert q_offline.decision.severity == "INFO"
    assert q_offline.decision.priority == "P2_HIGH"  # Distinct severity (INFO) vs priority (P2_HIGH)!
    qa_target = next(t for t in q_offline.incident.routing_targets if t.target_id == "qa.verifier")
    assert qa_target.verified_online is False
    assert qa_target.delivery_status == "QUEUED_FOR_ONLINE"
    assert any(t.target_id == "qa" and t.delivery_status == "FALLBACK_DEPARTMENT" for t in q_offline.incident.routing_targets)

    # Confirm runtime session ACTIVE on qa.verifier -> subsequent specialist question sees verified_online=True!
    dept_harness.store.sessions.save_session(
        Session(
            session_id=new_session_id(),
            agent_id="qa.verifier",
            task_id=task_id,
            run_id=run_id,
            status="ACTIVE",
            started_at=t0 + timedelta(seconds=4),
        )
    )
    q_online = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="worker_report",
            event_category="specialist_question",
            summary="Confirming QA round 2 after session started.",
            task_id=task_id,
            run_id=run_id,
            target_department="qa",
            target_agent_id="qa.verifier",
            timestamp=t0 + timedelta(seconds=5),
        ),
        auth_context=worker_ctx("it.worker", assigned_task_ids=[task_id]),
    )
    qa_online_target = next(t for t in q_online.incident.routing_targets if t.target_id == "qa.verifier")
    assert qa_online_target.verified_online is True
    assert qa_online_target.delivery_status == "ROUTED"

    # 4. HUMAN_INTERVENTION_REQUIRED: approval_required -> WARNING severity yet P1_URGENT priority + Action Inbox!
    human_out = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="governance",
            event_category="approval_required",
            summary="Operator approval required before writing external bundle.",
            task_id=task_id,
            run_id=run_id,
            approval_id="apr_p11_02_test",
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=dept_harness.master,
    )
    assert human_out.decision.classification == "HUMAN_INTERVENTION_REQUIRED"
    assert human_out.decision.severity == "WARNING"
    assert human_out.decision.priority == "P1_URGENT"
    assert human_out.action_inbox_item is not None
    assert human_out.action_inbox_item.item_kind == "APPROVAL_NEEDED"
    assert human_out.incident.master_notified is True
    assert len(reception.list_action_inbox(task_id=task_id)) >= 1

    # 5. CRITICAL_EMERGENCY: security_breach -> CRITICAL + P0_IMMEDIATE + IMMEDIATE + Master notified!
    crit_out = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="governance",
            event_category="security_breach",
            summary="Blocked workspace path escape probe outside managed directory.",
            task_id=task_id,
            run_id=run_id,
            timestamp=t0 + timedelta(seconds=7),
        ),
        auth_context=dept_harness.alice,
    )
    assert crit_out.decision.classification == "CRITICAL_EMERGENCY"
    assert crit_out.decision.severity == "CRITICAL"
    assert crit_out.decision.priority == "P0_IMMEDIATE"
    assert crit_out.decision.urgency == "IMMEDIATE"
    assert crit_out.incident.master_notified is True
    assert any(t.target_id == "core.master" for t in crit_out.incident.routing_targets)
    assert crit_out.action_inbox_item is not None
    assert crit_out.action_inbox_item.priority == "CRITICAL"

    # Task & Run status NEVER auto-mutated by any interrupt classification!
    task_after = dept_harness.store.tasks.get_task(task_id)
    run_after = dept_harness.store.runs.get_run(run_id)
    assert task_after is not None and task_after.status == "RUNNING"
    assert run_after is not None and run_after.status == "RUNNING"


def test_p11_02_worker_security_spoofing_and_unauthorized_target_blocked(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Block worker critical self-escalation, actor spoofing, unassigned tasks, and bad targets."""
    t0 = utc(10, 0, 0)
    register_worker_agent(
        dept_harness,
        agent_id="it.worker",
        department="it",
        skills=["code_execution"],
        timestamp=t0,
    )
    register_worker_agent(
        dept_harness,
        agent_id="hr.disabled",
        department="hr",
        skills=["hr_operations"],
        timestamp=t0,
    )
    dept_harness.agent_registry.disable_agent(
        "hr.disabled",
        reason="Disabled agent must not receive routing",
        expected_record_version=1,
        auth_context=dept_harness.alice,
        timestamp=t0 + timedelta(seconds=1),
    )
    task_id, run_id = _seed_task_run_with_agents(dept_harness, timestamp=t0 + timedelta(seconds=2))
    svc = _new_interrupts(dept_harness)
    assigned_worker = worker_ctx("it.worker", assigned_task_ids=[task_id])
    foreign_worker = worker_ctx("qa.other", assigned_task_ids=[])

    # Worker cannot declare CRITICAL or emergency_stop / security_breach.
    for bad_req in (
        InterruptSubmitRequest(
            source="worker_report",
            event_category="anomaly",
            summary="Worker attempting CRITICAL claim",
            task_id=task_id,
            claimed_severity="CRITICAL",
            timestamp=t0 + timedelta(seconds=3),
        ),
        InterruptSubmitRequest(
            source="worker_report",
            event_category="emergency_stop",
            summary="Worker attempting emergency stop escalation",
            task_id=task_id,
            timestamp=t0 + timedelta(seconds=3),
        ),
        InterruptSubmitRequest(
            source="worker_report",
            event_category="anomaly",
            summary="Worker claiming system-wide impact",
            task_id=task_id,
            claimed_impact="SYSTEM_WIDE",
            timestamp=t0 + timedelta(seconds=3),
        ),
    ):
        with pytest.raises(DepartmentGovernanceError) as err:
            svc.submit_interrupt(bad_req, auth_context=assigned_worker)
        assert err.value.error_code == "WORKER_CRITICAL_ESCALATION_DENIED"

    # Worker claiming ERROR on routine status update or anomaly gets capped by policy.
    capped = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="worker_report",
            event_category="status_update",
            summary="Worker inflating severity on normal progress",
            task_id=task_id,
            claimed_severity="ERROR",
            timestamp=t0 + timedelta(seconds=4),
        ),
        auth_context=assigned_worker,
    )
    assert capped.decision.severity_override_applied is True
    assert capped.decision.severity == "INFO"
    assert capped.decision.priority == "P4_LOW"

    # Spoofed actor identity rejected fail-closed.
    with pytest.raises(DepartmentGovernanceError) as spoof_err:
        svc.submit_interrupt(
            InterruptSubmitRequest(
                source="user",
                event_category="status_update",
                summary="Spoofing Master identity",
                claimed_actor_id="core.master",
                task_id=task_id,
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=dept_harness.alice,
        )
    assert spoof_err.value.error_code == "ACTOR_IDENTITY_MISMATCH"

    # Foreign worker unassigned to Task blocked.
    with pytest.raises(DepartmentGovernanceError) as own_err:
        svc.submit_interrupt(
            InterruptSubmitRequest(
                source="worker_report",
                event_category="status_update",
                summary="Unassigned worker injecting interrupt",
                task_id=task_id,
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=foreign_worker,
        )
    assert own_err.value.error_code == "TASK_OWNERSHIP_DENIED"

    # Unauthorized targets: disabled agent, unregistered agent, mismatched dept/agent, worker routing out of scope.
    with pytest.raises(DepartmentGovernanceError) as disabled_err:
        svc.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="specialist_question",
                summary="Routing to disabled agent",
                task_id=task_id,
                target_agent_id="hr.disabled",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=dept_harness.master,
        )
    assert disabled_err.value.error_code == "UNAUTHORIZED_ROUTING_TARGET"

    with pytest.raises(DepartmentGovernanceError) as mismatch_err:
        svc.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="specialist_question",
                summary="Routing IT worker as if QA department",
                task_id=task_id,
                target_department="qa",
                target_agent_id="it.worker",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=dept_harness.master,
        )
    assert mismatch_err.value.error_code == "UNAUTHORIZED_ROUTING_TARGET"

    # Bad task/run/conversation refs rejected cleanly.
    with pytest.raises(InterruptReferenceError):
        svc.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Missing task id",
                task_id=new_task_id(),
                timestamp=t0 + timedelta(seconds=7),
            ),
            auth_context=dept_harness.master,
        )
    with pytest.raises(InterruptReferenceError):
        svc.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Mismatched run id",
                task_id=task_id,
                run_id=new_run_id(),
                timestamp=t0 + timedelta(seconds=7),
            ),
            auth_context=dept_harness.master,
        )

    # Secrets in metadata rejected; secrets in free text scrubbed before persistence.
    with pytest.raises(ValidationError):
        InterruptSubmitRequest(
            source="runtime",
            event_category="anomaly",
            summary="Secret in metadata",
            metadata={"api_key": "sk-1234567890abcdef1234567890abcdef"},
            timestamp=t0 + timedelta(seconds=8),
        )
    scrub_out = svc.submit_interrupt(
        InterruptSubmitRequest(
            source="telemetry",
            event_category="anomaly",
            summary="Upstream error reported Bearer secret_abc_999999 and sk-1234567890abcdef1234",
            task_id=task_id,
            run_id=run_id,
            timestamp=t0 + timedelta(seconds=8),
        ),
        auth_context=dept_harness.master,
    )
    assert "secret_abc_999999" not in scrub_out.incident.summary
    assert "1234567890abcdef1234" not in scrub_out.incident.summary
    assert "[REDACTED" in scrub_out.incident.summary


def test_p11_02_persistent_dedup_exact_retry_incident_aggregation_and_recurrence(
    tmp_path: Path,
) -> None:
    """Verify exact retry, multi-event incident aggregation, distinct same-text incidents & recurrence."""
    t0 = utc(11, 0, 0)
    h1 = make_department_harness(tmp_path)
    try:
        task_id, run_id = _seed_task_run_with_agents(h1, timestamp=t0)
        svc1 = _new_interrupts(h1)

        # Case A1: First event for incident_key='disk-io-stall', event_dedup_key='ev-1'.
        out1 = svc1.submit_interrupt(
            InterruptSubmitRequest(
                source="telemetry",
                event_category="resource_warning",
                summary="High disk latency observed in worker scratch dir",
                task_id=task_id,
                run_id=run_id,
                event_dedup_key="ev-1",
                incident_key="disk-io-stall",
                timestamp=t0 + timedelta(seconds=1),
            ),
            auth_context=h1.alice,
        )
        assert out1.deduplicated is False
        assert out1.dedup_reason == "NEW_INCIDENT"
        assert out1.notification_suppressed is False
        assert out1.notification_id is not None

        # Case A2: Exact retry of 'ev-1' -> returns replay with ZERO new notifications or count increment!
        retry1 = svc1.submit_interrupt(
            InterruptSubmitRequest(
                source="telemetry",
                event_category="resource_warning",
                summary="High disk latency observed in worker scratch dir",
                task_id=task_id,
                run_id=run_id,
                event_dedup_key="ev-1",
                incident_key="disk-io-stall",
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h1.alice,
        )
        assert retry1.deduplicated is True
        assert retry1.dedup_reason == "EXACT_EVENT_RETRY"
        assert retry1.notification_suppressed is True
        assert retry1.incident.incident_id == out1.incident.incident_id
        assert retry1.incident.occurrence_count == 1

        # Case A3: Conflicting payload reusing same event_dedup_key fails closed.
        with pytest.raises(InterruptValidationError) as conflict_err:
            svc1.submit_interrupt(
                InterruptSubmitRequest(
                    source="telemetry",
                    event_category="resource_warning",
                    summary="Completely different problem text on reused dedup key",
                    task_id=task_id,
                    run_id=run_id,
                    event_dedup_key="ev-1",
                    incident_key="disk-io-stall",
                    timestamp=t0 + timedelta(seconds=2),
                ),
                auth_context=h1.alice,
            )
        assert conflict_err.value.error_code == "EVENT_DEDUP_CONFLICT"

        # Case B: Second distinct event 'ev-2' belonging to SAME active incident_key aggregates safely!
        out2 = svc1.submit_interrupt(
            InterruptSubmitRequest(
                source="telemetry",
                event_category="anomaly",
                summary="Latency spike continued past 5s window",
                task_id=task_id,
                run_id=run_id,
                event_dedup_key="ev-2",
                incident_key="disk-io-stall",
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=h1.alice,
        )
        assert out2.deduplicated is True
        assert out2.dedup_reason == "ACTIVE_INCIDENT_AGGREGATED"
        assert out2.notification_suppressed is True
        assert out2.incident.incident_id == out1.incident.incident_id
        assert out2.incident.occurrence_count == 2
        assert out2.notification_id == out1.notification_id
        assert len(svc1.list_interrupt_events(out1.incident.incident_id)) == 2

        # Case C: Two independent incidents with IDENTICAL summary text & no keys are NEVER merged!
        ind1 = svc1.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Identical anomaly phrase on independent incident",
                task_id=task_id,
                timestamp=t0 + timedelta(seconds=4),
            ),
            auth_context=h1.master,
        )
        ind2 = svc1.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Identical anomaly phrase on independent incident",
                task_id=task_id,
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=h1.master,
        )
        assert ind1.incident.incident_id != ind2.incident.incident_id
        assert ind1.notification_id != ind2.notification_id
        assert ind1.deduplicated is False and ind2.deduplicated is False

        # Resolve first incident cleanly, then simulate RESTART!
        svc1.resolve_interrupt(
            InterruptResolveRequest(
                incident_id=out1.incident.incident_id,
                expected_record_version=out2.incident.record_version,
                outcome="MITIGATED",
                resolution_note="Cleared transient scratch pressure.",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=h1.master,
        )
        notif_count_before_restart = len(h1.store.metadata.list_notifications(task_id=task_id))
    finally:
        h1.store.close()

    # Restart store on same SQLite DB: exact retry 'ev-2' still suppressed; new occurrence opens fresh incident!
    h2 = make_department_harness(tmp_path)
    try:
        svc2 = _new_interrupts(h2)
        replayed = svc2.submit_interrupt(
            InterruptSubmitRequest(
                source="telemetry",
                event_category="anomaly",
                summary="Latency spike continued past 5s window",
                task_id=task_id,
                run_id=run_id,
                event_dedup_key="ev-2",
                incident_key="disk-io-stall",
                timestamp=t0 + timedelta(seconds=7),
            ),
            auth_context=h2.alice,
        )
        assert replayed.deduplicated is True and replayed.dedup_reason == "EXACT_EVENT_RETRY"
        assert len(h2.store.metadata.list_notifications(task_id=task_id)) == notif_count_before_restart

        # New event 'ev-3' on previously RESOLVED incident_key opens a brand-new incident + notification!
        reopened = svc2.submit_interrupt(
            InterruptSubmitRequest(
                source="telemetry",
                event_category="resource_warning",
                summary="Disk stall recurred after mitigation",
                task_id=task_id,
                run_id=run_id,
                event_dedup_key="ev-3",
                incident_key="disk-io-stall",
                timestamp=t0 + timedelta(seconds=8),
            ),
            auth_context=h2.alice,
        )
        assert reopened.deduplicated is False
        assert reopened.dedup_reason == "POST_RESOLUTION_RECURRENCE"
        assert reopened.incident.incident_id != out1.incident.incident_id
        assert len(h2.store.metadata.list_notifications(task_id=task_id)) == notif_count_before_restart + 1
    finally:
        h2.store.close()


def test_p11_02_ack_escalation_resolution_lifecycle_concurrency_and_rollback(
    tmp_path: Path,
) -> None:
    """Verify ACK!=RESOLVED, immutable escalations, optimistic locking, Worker denials, and SQLite rollback."""
    t0 = utc(12, 0, 0)
    h = make_department_harness(tmp_path)
    try:
        register_worker_agent(
            h,
            agent_id="it.worker",
            department="it",
            skills=["code_execution"],
            timestamp=t0,
        )
        task_id, run_id = _seed_task_run_with_agents(h, timestamp=t0 + timedelta(seconds=1))
        svc = _new_interrupts(h)
        worker = worker_ctx("it.worker", assigned_task_ids=[task_id])

        out = svc.submit_interrupt(
            InterruptSubmitRequest(
                source="runtime",
                event_category="anomaly",
                summary="Intermittent timeout while reading staging endpoint",
                task_id=task_id,
                run_id=run_id,
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=h.master,
        )
        inc_id = out.incident.incident_id

        # 1. ACK moves status to ACKNOWLEDGED without resolving incident!
        acked = svc.acknowledge_interrupt(
            InterruptAckRequest(
                incident_id=inc_id,
                expected_record_version=1,
                acknowledgement_note="Investigating staging timeout root cause.",
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=worker,
        )
        assert acked.status == "ACKNOWLEDGED"
        assert acked.acknowledged_by == "it.worker"
        assert acked.resolved_at is None
        assert acked.resolution_outcome is None
        assert acked.record_version == 2
        assert len(svc.recover_open_interrupts()) == 1

        # 2. Worker cannot escalate or resolve incidents.
        with pytest.raises(DepartmentGovernanceError):
            svc.escalate_interrupt(
                InterruptEscalateRequest(
                    incident_id=inc_id,
                    expected_record_version=2,
                    reason="Worker trying to escalate",
                    timestamp=t0 + timedelta(seconds=4),
                ),
                auth_context=worker,
            )
        with pytest.raises(DepartmentGovernanceError):
            svc.resolve_interrupt(
                InterruptResolveRequest(
                    incident_id=inc_id,
                    expected_record_version=2,
                    outcome="FIXED",
                    resolution_note="Worker trying to self-resolve",
                    timestamp=t0 + timedelta(seconds=4),
                ),
                auth_context=worker,
            )

        # 3. Stale optimistic lock rejected.
        with pytest.raises(StaleInterruptVersionError):
            svc.escalate_interrupt(
                InterruptEscalateRequest(
                    incident_id=inc_id,
                    expected_record_version=1,
                    reason="Stale version escalation attempt",
                    timestamp=t0 + timedelta(seconds=5),
                ),
                auth_context=h.master,
            )

        # 4. Master escalates incident cleanly -> records immutable InterruptEscalationRecord!
        esc1 = svc.escalate_interrupt(
            InterruptEscalateRequest(
                incident_id=inc_id,
                expected_record_version=2,
                target_severity="CRITICAL",
                additional_target_department="it",
                reason="Timeout persisted across 3 retries; escalate to CRITICAL.",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=h.master,
        )
        assert esc1.from_severity == "WARNING" and esc1.to_severity == "CRITICAL"
        assert esc1.to_classification == "CRITICAL_EMERGENCY"
        after_esc = svc.get_interrupt(inc_id)
        assert after_esc is not None and after_esc.status == "ESCALATED"
        assert after_esc.escalation_count == 1
        assert after_esc.master_notified is True
        assert after_esc.record_version == 3
        assert len(svc.list_interrupt_escalations(inc_id)) == 1

        # 5. Master resolves incident -> cannot re-resolve or ACK afterwards.
        resolved = svc.resolve_interrupt(
            InterruptResolveRequest(
                incident_id=inc_id,
                expected_record_version=3,
                outcome="FIXED",
                resolution_note="Endpoint recovered and verified.",
                timestamp=t0 + timedelta(seconds=7),
            ),
            auth_context=h.master,
        )
        assert resolved.status == "RESOLVED"
        assert resolved.resolution_outcome == "FIXED"
        assert svc.recover_open_interrupts() == []
        with pytest.raises(InterruptStateError):
            svc.resolve_interrupt(
                InterruptResolveRequest(
                    incident_id=inc_id,
                    expected_record_version=resolved.record_version,
                    outcome="FIXED",
                    resolution_note="Duplicate resolve attempt",
                    timestamp=t0 + timedelta(seconds=8),
                ),
                auth_context=h.master,
            )

        # 6. Fault-injected mid-transaction failure rolls back incident, notification & classified event atomically!
        classified_before = len(h.store.events.read_entries(event_type="reception.interrupt_classified"))
        notifs_before = len(h.store.metadata.list_notifications())
        incidents_before = len(svc.list_interrupts())

        def _boom(conn: sqlite3.Connection) -> None:
            _ = conn
            raise sqlite3.OperationalError("Simulated disk write fault before commit")

        faulty_svc = _new_interrupts(h, repo=SQLiteInterruptRepository(h.store, before_commit_hook=_boom))
        with pytest.raises(InterruptPersistenceError):
            faulty_svc.submit_interrupt(
                InterruptSubmitRequest(
                    source="governance",
                    event_category="security_breach",
                    summary="Must roll back cleanly without dangling notification",
                    task_id=task_id,
                    timestamp=t0 + timedelta(seconds=9),
                ),
                auth_context=h.alice,
            )
        assert len(h.store.events.read_entries(event_type="reception.interrupt_classified")) == classified_before
        assert len(h.store.metadata.list_notifications()) == notifs_before
        assert len(svc.list_interrupts()) == incidents_before

        # 7. Concurrent multi-threaded storm of 6 submissions on same incident_key -> exactly 1 active incident!
        def _burst(i: int) -> str:
            res = svc.submit_interrupt(
                InterruptSubmitRequest(
                    source="telemetry",
                    event_category="resource_warning",
                    summary=f"Concurrent storm alert #{i}",
                    task_id=task_id,
                    incident_key="concurrent-cpu-burst",
                    event_dedup_key=f"cpu-burst-ev-{i}",
                    timestamp=t0 + timedelta(seconds=10 + i),
                ),
                auth_context=h.alice,
            )
            return res.incident.incident_id

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(_burst, range(6)))
        assert len(set(ids)) == 1
        burst_inc = svc.get_interrupt(ids[0])
        assert burst_inc is not None and burst_inc.occurrence_count == 6
    finally:
        h.store.close()
