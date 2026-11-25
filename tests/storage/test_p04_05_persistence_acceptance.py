"""Offline End-to-End Persistence Acceptance & Fault-Injection Suite for Phase 04 / P04.05."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3

import pytest

from core.contracts import (
    Artifact,
    ChecklistItem,
    ConnectionRef,
    Event,
    NotificationRef,
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
from core.events import project_activity_timeline, replay_task_history
from core.tasks import ExecutionEvidence, QaVerificationEvidence, TransitionContext, qa_evidence_from_engine_verification
from engines.contract import EngineResult, EngineTelemetry
from engines.verification import verify_engine_artifact
from storage.db import apply_migrations, run_integrity_checks, sqlite_connection, sqlite_transaction
from storage.repositories import (
    PersistentWorkflowService,
    RepositoryError,
    SQLiteRepositoryStore,
    recover_task,
)


def test_offline_end_to_end_persistence_acceptance_across_restart(tmp_path: Path) -> None:
    """Exercise full QUEUED -> ASSIGNED -> RUNNING -> SUCCEEDED -> QA VERIFIED_PASS -> COMPLETED across DB restart."""
    db_path = tmp_path / "acceptance.db"
    workspace_dir = tmp_path / "fixture_ws"
    workspace_dir.mkdir()
    artifact_rel = "output/deliverable.txt"
    artifact_file = workspace_dir / artifact_rel
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_bytes = b"AI Village P04.05 deterministic offline deliverable\n"
    artifact_file.write_bytes(artifact_bytes)
    expected_sha = hashlib.sha256(artifact_bytes).hexdigest()

    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 14, 0, 0, tzinfo=timezone.utc)
    task_id = new_task_id()
    run_id = new_run_id()
    session_id = new_session_id()
    artifact_id = new_artifact_id()
    checklist_id = new_checklist_item_id()
    evidence_id = new_evidence_id()
    project_id = new_project_id()

    # Phase 1: Application session 1 executes workflow & persists atomically.
    with SQLiteRepositoryStore(db_path) as store1:
        svc1 = PersistentWorkflowService(store1)
        store1.metadata.save_project(ProjectRef(project_id=project_id, name="Persistence Acceptance"))
        store1.metadata.save_connection(
            ConnectionRef(
                provider_id="antigravity",
                connection_id="default_antigravity",
                model_id="default",
                credential_ref="env:ANTIGRAVITY_CLI_TOKEN",
            )
        )

        task = Task(
            task_id=task_id,
            project_id=project_id,
            title="Offline persistence acceptance",
            description="Run deterministic fixture workflow and recover after DB restart.",
            requested_by="operator.local",
            status="QUEUED",
            priority="NORMAL",
            created_at=t0,
            checklist_item_ids=[checklist_id],
        )
        assert svc1.create_task(task, correlation_id="corr-acc-01").accepted is True

        assign_res = svc1.apply_task_transition(
            task_id,
            "ASSIGNED",
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=1),
                reason="Assign IT worker",
                assigned_agent_ids=["it.artifact_worker"],
                correlation_id="corr-acc-01",
            ),
        )
        assert assign_res.accepted is True

        run = Run(
            run_id=run_id,
            task_id=task_id,
            runtime_id="custom.master_worker_v1",
            invocation_id="inv-acc-001",
            correlation_id="corr-acc-01",
            provider_id="antigravity",
            connection_id="default_antigravity",
            status="PENDING",
        )
        svc1.create_run(run)

        start_res = svc1.apply_paired_transition(
            "start",
            task_id,
            run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=2),
                reason="Start execution run",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_id,
                    reason="Start fixture run",
                ),
                correlation_id="corr-acc-01",
            ),
        )
        assert start_res.accepted is True

        session = Session(
            session_id=session_id,
            agent_id="it.artifact_worker",
            task_id=task_id,
            run_id=run_id,
            invocation_id="inv-acc-001",
            provider_id="antigravity",
            connection_id="default_antigravity",
            provider_conversation_id="agy-thread-acc-01",
            workspace_reference="workspace/acc_01",
            status="CLOSED",
            started_at=t0 + timedelta(seconds=2),
            finished_at=t0 + timedelta(seconds=5),
        )
        store1.sessions.save_session(session)

        # Checklist updated with fixture evidence.
        store1.metadata.save_checklist_item(
            ChecklistItem(
                checklist_item_id=checklist_id,
                task_id=task_id,
                title="Write deterministic deliverable",
                assigned_agent_id="it.artifact_worker",
                status="COMPLETED",
                order=0,
                evidence_reference=evidence_id,
                created_at=t0 + timedelta(seconds=1),
                completed_at=t0 + timedelta(seconds=4),
            )
        )

        # Independent disk SHA-256 verification produces VERIFIED_PASS evidence.
        artifact_text = artifact_bytes.decode("utf-8")
        fixture_engine_result = EngineResult(
            request_id="req-acc-fixture-01",
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id="default_antigravity",
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            observed_tool_names=["write_file"],
            started_at=t0 + timedelta(seconds=2),
            finished_at=t0 + timedelta(seconds=5),
            duration_ms=3000,
            telemetry=EngineTelemetry(
                duration_ms=3000,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        )
        disk_verification = verify_engine_artifact(
            workspace_dir=workspace_dir,
            artifact_relpath=artifact_rel,
            engine_result=fixture_engine_result,
            expected_content=artifact_text,
        )
        assert disk_verification.status == "VERIFIED_PASS"

        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.artifact_worker",
            relative_path=artifact_rel,
            content_type="text/plain",
            size_bytes=len(artifact_bytes),
            sha256=disk_verification.actual_sha256,
            version="1.0.0",
            verification_status="VERIFIED_PASS",
            evidence_reference=evidence_id,
            created_at=t0 + timedelta(seconds=4),
        )
        store1.metadata.save_artifact(artifact)

        # Complete Run only -> SUCCEEDED (Task remains non-terminal until QA gate passes).
        run_done = svc1.apply_paired_transition(
            "complete_run_only",
            task_id,
            run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=5),
                reason="Worker finished writing deliverable",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_id,
                    reason="Fixture workflow succeeded",
                    telemetry=RunTelemetryRef(
                        total_duration_ms=3000,
                        input_tokens=None,
                        output_tokens=None,
                        total_tokens=None,
                        telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                        telemetry_source="unavailable",
                    ),
                ),
            ),
        )
        assert run_done.accepted is True
        assert run_done.run is not None and run_done.run.status == "SUCCEEDED"
        assert run_done.task is not None and run_done.task.status == "RUNNING"

        # Record QA verification event + move Task -> COMPLETED via QA guard.
        qa_ev = qa_evidence_from_engine_verification(
            disk_verification,
            evidence_id=evidence_id,
            verifier_actor_id="qa.verifier",
            verified_at=t0 + timedelta(seconds=6),
            artifact_ids=[artifact_id],
        )
        store1.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=6),
                event_type="qa.verification_recorded",
                source="qa_verifier",
                task_id=task_id,
                run_id=run_id,
                actor_id="qa.verifier",
                artifact_id=artifact_id,
                evidence_id=evidence_id,
                redacted_payload={"verification_status": "VERIFIED_PASS"},
            )
        )
        checklist_items = store1.metadata.list_checklist_items(task_id)
        artifacts = store1.metadata.list_artifacts(task_id=task_id)
        complete_task_res = svc1.apply_task_transition(
            task_id,
            "COMPLETED",
            TransitionContext(
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                timestamp=t0 + timedelta(seconds=7),
                reason="QA verified artifact SHA-256 on disk",
                qa_evidence=qa_ev,
                checklist_items=checklist_items,
                artifacts=artifacts,
                correlation_id="corr-acc-01",
            ),
        )
        assert complete_task_res.accepted is True
        assert complete_task_res.task is not None and complete_task_res.task.status == "COMPLETED"

        store1.metadata.save_notification(
            NotificationRef(
                notification_id=new_notification_id(),
                task_id=task_id,
                run_id=run_id,
                event_id=complete_task_res.entries[0].event.event_id,
                severity="INFO",
                notification_type="QA_RESULT",
                created_at=t0 + timedelta(seconds=7),
            )
        )

        event_count_before = store1.events.count()
        last_seq_before = store1.events.read_entries(task_id=task_id)[-1].sequence_number

    # Phase 2: Reopen fresh repository store after restart and verify everything from disk.
    with SQLiteRepositoryStore(db_path) as store2:
        assert store2.events.count() == event_count_before
        report = recover_task(store2, task_id)
        assert report.recovery_status == "VERIFIED_CLEAN"
        assert report.snapshot_matches_replay is True
        assert report.interrupted_work == []
        assert report.snapshot_task is not None
        assert report.snapshot_task.status == "COMPLETED"
        assert report.snapshot_task.active_run_id is None
        assert report.runs[0].status == "SUCCEEDED"
        assert report.runs[0].telemetry is not None
        assert report.runs[0].telemetry.input_tokens is None
        assert report.sessions == [session]
        assert report.checklist_items == [checklist_items[0]]
        assert report.artifacts[0].sha256 == expected_sha
        assert report.artifacts[0].verification_status == "VERIFIED_PASS"
        assert report.timeline.total_returned == event_count_before
        assert report.timeline.last_sequence == last_seq_before

        # Disk tampering is still caught independently even though DB has VERIFIED_PASS metadata.
        artifact_file.write_bytes(b"tampered bytes")
        tamper_check = verify_engine_artifact(
            workspace_dir=workspace_dir,
            artifact_relpath=artifact_rel,
            engine_result=fixture_engine_result,
            expected_content=artifact_text,
        )
        assert tamper_check.status == "VERIFIED_FAIL"

    with sqlite_connection(db_path) as conn:
        integrity = run_integrity_checks(conn)
        assert integrity.integrity_ok is True
        assert integrity.foreign_key_ok is True


def test_fault_injection_a_crash_rollback_leaves_zero_partial_state(tmp_path: Path) -> None:
    """Fault A: Task SQL update succeeds, Event append fails before commit -> full rollback."""
    db_path = tmp_path / "fault_a.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 14, 10, 0, tzinfo=timezone.utc)
    task = Task(
        task_id=new_task_id(),
        title="Fault A rollback",
        description="Verify crash before event append rolls back Task SQL update.",
        requested_by="operator.local",
        status="QUEUED",
        created_at=t0,
    )

    with SQLiteRepositoryStore(db_path) as store:
        PersistentWorkflowService(store).create_task(task)

        def crash_hook(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("Injected disk fault before event insert")

        crashing_svc = PersistentWorkflowService(store, before_event_append_hook=crash_hook)
        with pytest.raises(sqlite3.OperationalError):
            crashing_svc.apply_task_transition(
                task.task_id,
                "ASSIGNED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=1),
                    reason="Should roll back",
                    assigned_agent_ids=["it.worker"],
                ),
            )

    with SQLiteRepositoryStore(db_path) as fresh_store:
        stored = fresh_store.tasks.get_stored_task(task.task_id)
        assert stored is not None
        assert stored.task.status == "QUEUED"
        assert stored.version == 1
        assert fresh_store.events.count() == 1


def test_fault_injection_b_duplicate_event_after_restart_is_idempotent(tmp_path: Path) -> None:
    """Fault B: Re-appending the same Event ID after restart never creates a duplicate row."""
    db_path = tmp_path / "fault_b.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 14, 20, 0, tzinfo=timezone.utc)
    ev = Event(
        event_id=new_event_id(),
        timestamp=t0,
        event_type="task.created",
        source="core_runtime",
        task_id=new_task_id(),
        redacted_payload={"initial_status": "QUEUED"},
    )

    with SQLiteRepositoryStore(db_path) as store1:
        first = store1.events.append(ev)
        assert first.appended is True and first.entry is not None
        seq1 = first.entry.sequence_number

    with SQLiteRepositoryStore(db_path) as store2:
        second = store2.events.append(ev)
        assert second.status == "IDEMPOTENT_DUPLICATE"
        assert second.appended is False
        assert second.is_duplicate is True
        assert second.entry is not None and second.entry.sequence_number == seq1
        assert store2.events.count() == 1


def test_fault_injection_c_broken_baseline_or_history_detected_by_replay(tmp_path: Path) -> None:
    """Fault C: Corrupted baseline_json or broken Event history never produces VERIFIED_CLEAN or COMPLETED."""
    db_path = tmp_path / "fault_c.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 14, 30, 0, tzinfo=timezone.utc)
    task = Task(
        task_id=new_task_id(),
        title="Fault C broken baseline",
        description="Replay must reject missing/corrupted baseline.",
        requested_by="operator.local",
        status="QUEUED",
        created_at=t0,
    )

    with SQLiteRepositoryStore(db_path) as store:
        svc = PersistentWorkflowService(store)
        svc.create_task(task)
        svc.apply_task_transition(
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

    # Simulate damaged baseline_json in isolated test fixture DB.
    with sqlite_connection(db_path) as conn:
        with sqlite_transaction(conn):
            conn.execute(
                "UPDATE tasks SET baseline_json = '{}' WHERE task_id = ?;",
                (task.task_id,),
            )

    with SQLiteRepositoryStore(db_path) as fresh_store:
        report = recover_task(fresh_store, task.task_id)
        assert report.recovery_status == "INCOMPLETE_HISTORY"
        assert report.baseline_task is None
        assert report.replay_result.can_reconstruct_full_task is False
        assert report.replay_result.reconstructed_task is None
        assert any(issue.error_code == "MISSING_BASELINE" for issue in report.replay_result.issues)
