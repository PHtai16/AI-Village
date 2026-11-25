"""Granular tests for Phase 04 / P04.05 persistent repositories, SQLiteEventJournal, concurrency, and recovery."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from pydantic import ValidationError
import pytest

from core.contracts import (
    Artifact,
    ChecklistItem,
    ConnectionRef,
    Event,
    NotificationRef,
    ProjectRef,
    Run,
    RunErrorRef,
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
from core.events import (
    EventJournal,
    project_activity_timeline,
    record_runtime_event,
    replay_task_history,
)
from core.runtime import AgentRuntimeEvent
from core.tasks import ExecutionEvidence, QaVerificationEvidence, TransitionContext
from engines.contract import EngineEvent
from storage.db import apply_migrations, sqlite_connection, sqlite_transaction
from storage.repositories import (
    DuplicateEntityError,
    EntityNotFoundError,
    MetadataRepository,
    OwnershipMismatchError,
    PersistentWorkflowService,
    RepositoryError,
    RunRepository,
    SQLiteEventJournal,
    SQLiteRepositoryStore,
    SchemaCompatibilityError,
    SecretViolationError,
    SessionRepository,
    StaleStateError,
    TaskRepository,
    recover_run,
    recover_task,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_GOOGLE_KEY = "AIza" + "FakeRepoSecretKey1234567890"
FAKE_OPENAI_KEY = "sk-" + "FakeRepoOpenAIKey1234567890"
SAMPLE_SHA = "a" * 64


@pytest.fixture
def t0() -> datetime:
    """Return deterministic UTC start timestamp."""
    return datetime(2026, 11, 25, 12, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create and migrate a fresh SQLite database inside ``tmp_path``."""
    target = tmp_path / "village_p0405.db"
    with sqlite_connection(target) as conn:
        apply_migrations(conn)
    return target


@pytest.fixture
def store(db_path: Path) -> SQLiteRepositoryStore:
    """Return an open ``SQLiteRepositoryStore`` bound to a migrated test DB."""
    repo_store = SQLiteRepositoryStore(db_path)
    try:
        yield repo_store
    finally:
        repo_store.close()


def _make_task(t0: datetime, *, project_id: str | None = None, metadata: dict | None = None) -> Task:
    return Task(
        task_id=new_task_id(),
        project_id=project_id,
        title="Build persistent storage",
        description="Persist Task, Run, Session, and Events cleanly.",
        requested_by="operator.local",
        status="QUEUED",
        priority="HIGH",
        created_at=t0,
        acceptance_criteria=["Restart preserves Task and Event Journal"],
        metadata=metadata or {"stage": "p0405", "retries": 0, "optional_note": None},
    )


def _make_run(task: Task, *, runtime_id: str = "custom.master_worker_v1") -> Run:
    return Run(
        run_id=new_run_id(),
        task_id=task.task_id,
        runtime_id=runtime_id,
        invocation_id="inv-p0405-001",
        correlation_id="corr-p0405-001",
        provider_id="antigravity",
        connection_id="default_antigravity",
        model_id="gemini-2.5-pro",
        status="PENDING",
    )


def test_sqlite_repositories_satisfy_protocol_interfaces(store: SQLiteRepositoryStore) -> None:
    """SQLite implementations satisfy Core/Repository Protocols without leaking sqlite3."""
    assert isinstance(store.tasks, TaskRepository)
    assert isinstance(store.runs, RunRepository)
    assert isinstance(store.sessions, SessionRepository)
    assert isinstance(store.metadata, MetadataRepository)
    assert isinstance(store.events, EventJournal)


def test_architecture_boundary_core_does_not_import_sqlite_or_storage() -> None:
    """Core modules must remain completely independent from sqlite3 and storage/."""
    core_dir = REPO_ROOT / "core"
    for py_file in core_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("sqlite3", "storage", "sqlalchemy")), (
                        f"Forbidden import '{alias.name}' in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("sqlite3", "storage", "sqlalchemy")), (
                    f"Forbidden import from '{node.module}' in {py_file}"
                )


def test_controlled_task_crud_multi_run_and_ownership_guards(
    store: SQLiteRepositoryStore,
    t0: datetime,
) -> None:
    """Task supports controlled creation/state saving, multiple Runs, and rejects foreign Run links."""
    task1 = _make_task(t0)
    task2 = _make_task(t0)
    stored1 = store.tasks.create_task(task1)
    store.tasks.create_task(task2)
    assert stored1.version == 1
    assert stored1.baseline == task1

    with pytest.raises(DuplicateEntityError):
        store.tasks.create_task(task1)

    run1a = _make_run(task1)
    run1b = _make_run(task1)
    run2 = _make_run(task2)
    store.runs.create_run(run1a)
    store.runs.create_run(run1b)
    store.runs.create_run(run2)

    runs_for_t1 = store.runs.list_runs_for_task(task1.task_id)
    assert {r.run_id for r in runs_for_t1} == {run1a.run_id, run1b.run_id}

    # Task 1 cannot reference Run 2 owned by Task 2.
    bad_task = task1.model_copy(update={"status": "RUNNING", "active_run_id": run2.run_id})
    with pytest.raises(OwnershipMismatchError):
        store.tasks.save_task_state(bad_task, expected_version=1)

    # Run cannot be reassigned to a different Task.
    reparented_run = run1a.model_copy(update={"task_id": task2.task_id})
    with pytest.raises(OwnershipMismatchError):
        store.runs.save_run_state(reparented_run, expected_version=1)

    # Nonexistent parent Task rejects Run creation.
    orphan_run = Run(
        run_id=new_run_id(),
        task_id=new_task_id(),
        runtime_id="custom.master_worker_v1",
        status="PENDING",
    )
    with pytest.raises(EntityNotFoundError):
        store.runs.create_run(orphan_run)


def test_session_checklist_artifact_notification_connection_json_roundtrip(
    store: SQLiteRepositoryStore,
    t0: datetime,
) -> None:
    """All domain entities round-trip cleanly with JSON metadata, timestamps, and NULL telemetry."""
    project = ProjectRef(project_id=new_project_id(), name="Village Core", description="Grouping only")
    assert store.metadata.save_project(project) == project

    task = _make_task(t0, project_id=project.project_id)
    store.tasks.create_task(task)
    run = _make_run(task)
    store.runs.create_run(run)

    # Update Run with unavailable telemetry preserving all None counters.
    running_run = run.model_copy(
        update={
            "status": "RUNNING",
            "started_at": t0 + timedelta(seconds=1),
            "telemetry": RunTelemetryRef(
                total_duration_ms=250,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                actual_cost_usd=None,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        }
    )
    stored_run = store.runs.save_run_state(running_run, expected_version=1)
    assert stored_run.run.telemetry is not None
    assert stored_run.run.telemetry.input_tokens is None
    assert stored_run.run.telemetry.total_tokens is None
    assert stored_run.run.telemetry.actual_cost_usd is None
    assert stored_run.run.telemetry.total_duration_ms == 250

    session = Session(
        session_id=new_session_id(),
        legacy_session_ref="sess-spike-001",
        agent_id="it.artifact_worker",
        task_id=task.task_id,
        run_id=run.run_id,
        invocation_id="inv-p0405-001",
        provider_id="antigravity",
        connection_id="default_antigravity",
        provider_conversation_id="agy-conv-9988",
        workspace_reference="workspace/task_alpha",
        status="ACTIVE",
        started_at=t0 + timedelta(seconds=1),
    )
    saved_session = store.sessions.save_session(session)
    assert saved_session == session
    assert saved_session.session_id != saved_session.provider_conversation_id

    chk = ChecklistItem(
        checklist_item_id=new_checklist_item_id(),
        task_id=task.task_id,
        title="Verify artifact hash",
        assigned_agent_id="qa.verifier",
        status="COMPLETED",
        order=0,
        evidence_reference=new_evidence_id(),
        created_at=t0,
        completed_at=t0 + timedelta(seconds=5),
    )
    assert store.metadata.save_checklist_item(chk) == chk
    assert store.metadata.list_checklist_items(task.task_id) == [chk]

    art = Artifact(
        artifact_id=new_artifact_id(),
        task_id=task.task_id,
        run_id=run.run_id,
        owner_agent_id="it.artifact_worker",
        relative_path="output/proof.txt",
        content_type="text/plain",
        size_bytes=64,
        sha256=SAMPLE_SHA,
        version="1.0.0",
        verification_status="UNVERIFIED",
        created_at=t0 + timedelta(seconds=4),
    )
    assert store.metadata.save_artifact(art) == art
    loaded_art = store.metadata.get_artifact(art.artifact_id)
    assert loaded_art is not None
    assert loaded_art.verification_status == "UNVERIFIED"
    assert loaded_art.sha256 == SAMPLE_SHA

    conn_ref = ConnectionRef(
        provider_id="google_gemini",
        connection_id="gemini_primary",
        model_id="gemini-2.5-pro",
        project_ref="prj_billing_01",
        credential_ref="env:GEMINI_API_KEY",
    )
    assert store.metadata.save_connection(conn_ref) == conn_ref
    assert store.metadata.get_connection("google_gemini", "gemini_primary") == conn_ref

    ev = Event(
        event_id=new_event_id(),
        timestamp=t0 + timedelta(seconds=6),
        event_type="notification.created",
        source="core_runtime",
        task_id=task.task_id,
        run_id=run.run_id,
        redacted_payload={"notice": "artifact recorded"},
    )
    assert store.events.append(ev).appended is True
    notif = NotificationRef(
        notification_id=new_notification_id(),
        task_id=task.task_id,
        run_id=run.run_id,
        event_id=ev.event_id,
        severity="INFO",
        notification_type="TASK_UPDATE",
        created_at=t0 + timedelta(seconds=6),
    )
    assert store.metadata.save_notification(notif) == notif
    assert store.metadata.list_notifications(task_id=task.task_id) == [notif]


def test_sqlite_event_journal_ordering_duplicates_across_restart_and_batch_atomicity(
    db_path: Path,
    t0: datetime,
) -> None:
    """SQLiteEventJournal enforces ordering, restart-safe idempotency, conflict rejection, and atomic batches."""
    tid = new_task_id()
    ev1 = Event(
        event_id=new_event_id(),
        timestamp=t0,
        event_type="task.created",
        source="core_runtime",
        task_id=tid,
        redacted_payload={"initial_status": "QUEUED"},
    )
    ev2 = Event(
        event_id=new_event_id(),
        timestamp=t0 + timedelta(seconds=1),
        event_type="task.status_changed",
        source="orchestrator",
        task_id=tid,
        redacted_payload={"previous_status": "QUEUED", "new_status": "ASSIGNED"},
    )
    ev3 = Event(
        event_id=new_event_id(),
        timestamp=t0 + timedelta(seconds=2),
        event_type="task.status_changed",
        source="core_runtime",
        task_id=tid,
        redacted_payload={"previous_status": "ASSIGNED", "new_status": "RUNNING"},
    )

    j1 = SQLiteEventJournal(db_path)
    res1 = j1.append(ev1)
    assert res1.status == "APPENDED" and res1.entry is not None
    assert res1.entry.sequence_number == 1

    batch_res = j1.append_batch([ev2, ev3])
    assert batch_res.status == "APPENDED" and batch_res.appended is True
    assert [e.sequence_number for e in batch_res.entries] == [2, 3]
    j1.close()

    # Reopen brand-new SQLiteEventJournal instance on same DB file (no RAM cache).
    j2 = SQLiteEventJournal(db_path)
    try:
        dup_same = j2.append(ev1)
        assert dup_same.status == "IDEMPOTENT_DUPLICATE"
        assert dup_same.appended is False
        assert dup_same.is_duplicate is True
        assert dup_same.entry is not None and dup_same.entry.sequence_number == 1

        ev1_mutated = ev1.model_copy(update={"redacted_payload": {"initial_status": "COMPLETED"}})
        dup_conflict = j2.append(ev1_mutated)
        assert dup_conflict.status == "REJECTED"
        assert dup_conflict.error is not None
        assert dup_conflict.error.error_code == "DUPLICATE_EVENT_CONFLICT"

        # Atomic batch fails completely if second item collides with existing event_id.
        ev4 = Event(
            event_id=new_event_id(),
            timestamp=t0 + timedelta(seconds=3),
            event_type="runtime.error",
            source="core_runtime",
            task_id=tid,
            redacted_payload={"msg": "new"},
        )
        bad_batch = j2.append_batch([ev4, ev2])
        assert bad_batch.status == "REJECTED"
        assert bad_batch.appended is False
        assert bad_batch.error is not None
        assert bad_batch.error.error_code == "DUPLICATE_EVENT_CONFLICT"
        assert j2.get_by_event_id(ev4.event_id) is None
        assert j2.count() == 3

        # Subsequent valid append continues seamlessly at sequence_number=4.
        after_res = j2.append(ev4)
        assert after_res.entry is not None and after_res.entry.sequence_number == 4
    finally:
        j2.close()


def test_single_transaction_rollback_when_event_or_task_write_fails(
    db_path: Path,
    t0: datetime,
) -> None:
    """Task/Run/Event updates commit atomically or roll back completely with zero partial state."""
    store = SQLiteRepositoryStore(db_path)
    try:
        task = _make_task(t0)
        run = _make_run(task)
        svc = PersistentWorkflowService(store)
        created = svc.create_task(task)
        assert created.accepted is True
        svc.create_run(run)

        def fail_before_event(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("Simulated crash before event append")

        faulty_svc = PersistentWorkflowService(store, before_event_append_hook=fail_before_event)
        ctx = TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=t0 + timedelta(seconds=1),
            reason="Assign worker",
            assigned_agent_ids=["it.artifact_worker"],
        )
        with pytest.raises(RuntimeError, match="Simulated crash"):
            faulty_svc.apply_task_transition(task.task_id, "ASSIGNED", ctx)

        # Task must remain QUEUED v1, and no ASSIGNED event may exist.
        stored_after = store.tasks.get_stored_task(task.task_id)
        assert stored_after is not None
        assert stored_after.task.status == "QUEUED"
        assert stored_after.version == 1
        assert len(store.events.read_entries(task_id=task.task_id)) == 1

        # Move Task to ASSIGNED cleanly, then force paired start rollback via hook.
        assert svc.apply_task_transition(task.task_id, "ASSIGNED", ctx).accepted is True
        with pytest.raises(RuntimeError, match="Simulated crash"):
            faulty_svc.apply_paired_transition(
                "start",
                task.task_id,
                run.run_id,
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="core_runtime",
                    timestamp=t0 + timedelta(seconds=2),
                    reason="Start run",
                    execution_evidence=ExecutionEvidence(
                        source="unit_test_fixture",
                        run_id=run.run_id,
                        reason="Start run fixture",
                    ),
                ),
            )

        reloaded_task = store.tasks.get_task(task.task_id)
        reloaded_run = store.runs.get_run(run.run_id)
        assert reloaded_task is not None and reloaded_task.status == "ASSIGNED"
        assert reloaded_task.active_run_id is None
        assert reloaded_run is not None and reloaded_run.status == "PENDING"
        assert reloaded_run.started_at is None
    finally:
        store.close()


def test_stale_state_update_and_concurrent_writes(
    db_path: Path,
    t0: datetime,
) -> None:
    """Optimistic versioning rejects stale snapshots and concurrent journal/task writers never lose updates."""
    store = SQLiteRepositoryStore(db_path)
    try:
        task = _make_task(t0)
        svc = PersistentWorkflowService(store)
        svc.create_task(task)

        # Two workers read version 1; Worker A assigns; Worker B attempts stale update with expected_version=1.
        ctx1 = TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=t0 + timedelta(seconds=1),
            reason="Assign by worker A",
            assigned_agent_ids=["it.worker_a"],
        )
        ok = svc.apply_task_transition(task.task_id, "ASSIGNED", ctx1, expected_task_version=1)
        assert ok.accepted is True and ok.task_version == 2

        with pytest.raises(StaleStateError):
            svc.apply_task_transition(
                task.task_id,
                "BLOCKED",
                TransitionContext(
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=2),
                    reason="Stale worker B block",
                ),
                expected_task_version=1,
            )

        # Two concurrent workers race to complete the same Run; only one succeeds, the other gets StaleStateError or rejection.
        run = _make_run(task)
        svc.create_run(run)
        start_res = svc.apply_paired_transition(
            "start",
            task.task_id,
            run.run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=3),
                reason="Start execution",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run.run_id,
                    reason="Start concurrent run",
                ),
            ),
        )
        assert start_res.accepted is True
        running_run_ver = start_res.run_version
        running_task_ver = start_res.task_version
        assert running_run_ver is not None and running_task_ver is not None

        def finish_worker(idx: int) -> str:
            local_store = SQLiteRepositoryStore(db_path)
            try:
                local_svc = PersistentWorkflowService(local_store)
                local_svc.apply_paired_transition(
                    "complete_run_only",
                    task.task_id,
                    run.run_id,
                    TransitionContext(
                        actor_id="core.master",
                        actor_kind="core_runtime",
                        timestamp=t0 + timedelta(seconds=4 + idx),
                        reason=f"Complete worker {idx}",
                        execution_evidence=ExecutionEvidence(
                            source="unit_test_fixture",
                            run_id=run.run_id,
                            reason=f"Complete worker {idx}",
                        ),
                    ),
                    expected_task_version=running_task_ver,
                    expected_run_version=running_run_ver,
                )
                return "COMMITTED"
            except StaleStateError:
                return "STALE_REJECTED"
            finally:
                local_store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(finish_worker, [1, 2]))
        assert sorted(outcomes) == ["COMMITTED", "STALE_REJECTED"]

        # Concurrent Event appends across 12 threads produce strictly monotonic unique sequence numbers.
        def append_worker(i: int) -> int:
            local_journal = SQLiteEventJournal(db_path)
            try:
                ev = Event(
                    event_id=new_event_id(),
                    timestamp=t0 + timedelta(seconds=10 + i),
                    event_type="tool.call_observed",
                    source="tool_runtime",
                    task_id=task.task_id,
                    run_id=run.run_id,
                    redacted_payload={"tool_name": "read_file", "worker_idx": i},
                )
                res = local_journal.append(ev)
                assert res.appended and res.entry is not None
                return res.entry.sequence_number
            finally:
                local_journal.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            seqs = list(pool.map(append_worker, range(12)))
        assert len(set(seqs)) == 12
        assert sorted(seqs) == list(range(min(seqs), max(seqs) + 1))
    finally:
        store.close()


def test_restart_recovery_timeline_and_interrupted_running_work(
    db_path: Path,
    t0: datetime,
) -> None:
    """After closing DB and reopening fresh store, RUNNING work is flagged NEEDS_RECONCILIATION, never COMPLETED/FAILED."""
    store1 = SQLiteRepositoryStore(db_path)
    svc1 = PersistentWorkflowService(store1)
    task = _make_task(t0)
    run = _make_run(task)
    svc1.create_task(task)
    svc1.create_run(run)
    svc1.apply_task_transition(
        task.task_id,
        "ASSIGNED",
        TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=t0 + timedelta(seconds=1),
            reason="Assign worker",
            assigned_agent_ids=["it.artifact_worker"],
        ),
    )
    start_outcome = svc1.apply_paired_transition(
        "start",
        task.task_id,
        run.run_id,
        TransitionContext(
            actor_id="core.master",
            actor_kind="core_runtime",
            timestamp=t0 + timedelta(seconds=2),
            reason="Start worker",
            execution_evidence=ExecutionEvidence(
                source="unit_test_fixture",
                run_id=run.run_id,
                reason="Start worker fixture",
            ),
        ),
    )
    assert start_outcome.accepted is True
    session = Session(
        session_id=new_session_id(),
        agent_id="it.artifact_worker",
        task_id=task.task_id,
        run_id=run.run_id,
        provider_id="antigravity",
        connection_id="default_antigravity",
        provider_conversation_id="agy-live-thread-7",
        status="ACTIVE",
        started_at=t0 + timedelta(seconds=2),
    )
    store1.sessions.save_session(session)
    events_before = store1.events.count()
    store1.close()

    # Reopen from disk in a brand-new repository store.
    with SQLiteRepositoryStore(db_path) as store2:
        assert store2.events.count() == events_before
        report = recover_task(store2, task.task_id)
        assert report.recovery_status == "NEEDS_RECONCILIATION"
        assert report.snapshot_matches_replay is True
        assert report.snapshot_task is not None
        assert report.snapshot_task.status == "RUNNING"
        assert report.snapshot_task.active_run_id == run.run_id
        assert len(report.interrupted_work) == 2
        assert all(
            item.observable_process_status == "UNOBSERVABLE_AFTER_RESTART"
            and item.reconciliation_required is True
            for item in report.interrupted_work
        )
        assert report.sessions == [session]

        run_report = recover_run(store2, run.run_id)
        assert run_report.recovery_status == "NEEDS_RECONCILIATION"
        assert run_report.snapshot_run is not None
        assert run_report.snapshot_run.status == "RUNNING"

        timeline = project_activity_timeline(store2.events, task_id=task.task_id)
        assert timeline.total_returned == events_before
        assert [row.sequence_number for row in timeline.entries] == sorted(
            row.sequence_number for row in timeline.entries
        )


def test_incomplete_missing_event_corrupted_history_and_agent_runtime_compatibility(
    db_path: Path,
    t0: datetime,
) -> None:
    """Detect missing baseline, missing/corrupted events, secret leaks, schema mismatch, and AgentRuntime bridge."""
    # 1. AgentRuntimeEvent bridge persists cleanly via SQLiteEventJournal.
    with SQLiteRepositoryStore(db_path) as store:
        task = _make_task(t0)
        run = _make_run(task)
        svc = PersistentWorkflowService(store)
        svc.create_task(task)
        svc.create_run(run)

        rt_event = AgentRuntimeEvent(
            invocation_id="inv-p0405-001",
            runtime_id="custom.master_worker_v1",
            sequence=1,
            stage="worker_execute",
            actor_id="it.artifact_worker",
            actor_role="worker",
            logical_session_id="sess-legacy-p03",
            provider_id="antigravity",
            provider_conversation_id="agy-conv-1",
            engine_event=EngineEvent(
                event_id="eng-evt-1",
                sequence_number=1,
                provider_id="antigravity",
                connection_id="default_antigravity",
                event_type="tool_execution_observed",
                timestamp=t0 + timedelta(seconds=1),
                status="IN_PROGRESS",
                tool_name="write_file",
                source="provider",
                redacted_payload={"path": "proof.txt"},
            ),
        )
        rec = record_runtime_event(
            store.events,
            rt_event,
            task_id=task.task_id,
            run_id=run.run_id,
            correlation_id="corr-p0405-001",
        )
        assert rec.appended is True
        tl = project_activity_timeline(store.events, task_id=task.task_id)
        assert any(row.tool_name == "write_file" and row.legacy_session_ref == "sess-legacy-p03" for row in tl.entries)

        # 2. Secret payloads and raw API keys in ConnectionRef / Event / Task are rejected fail-closed.
        with pytest.raises(SecretViolationError):
            store.metadata.save_connection(
                ConnectionRef.model_construct(
                    provider_id="google_gemini",
                    connection_id="bad_conn",
                    credential_ref=FAKE_GOOGLE_KEY,
                )
            )
        unsafe_ev = Event.model_construct(
            event_id=new_event_id(),
            timestamp=t0 + timedelta(seconds=2),
            event_type="runtime.error",
            source="core_runtime",
            task_id=task.task_id,
            redacted_payload={"api_key": FAKE_OPENAI_KEY},
        )
        rej = store.events.append(unsafe_ev)
        assert rej.status == "REJECTED"
        assert rej.error is not None and rej.error.error_code == "UNSAFE_PAYLOAD"

        # 3. Fabricated QA PASS without evidence or sha256 fails Pydantic validation.
        with pytest.raises(ValidationError):
            Artifact(
                artifact_id=new_artifact_id(),
                task_id=task.task_id,
                run_id=run.run_id,
                owner_agent_id="it.artifact_worker",
                relative_path="output/proof.txt",
                verification_status="VERIFIED_PASS",
                sha256=None,
                evidence_reference=None,
            )

        # 4. Missing Task baseline (events exist without Task row) -> INCOMPLETE_HISTORY.
        orphan_tid = new_task_id()
        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0,
                event_type="task.status_changed",
                source="core_runtime",
                task_id=orphan_tid,
                redacted_payload={"previous_status": "QUEUED", "new_status": "ASSIGNED"},
            )
        )
        orphan_report = recover_task(store, orphan_tid)
        assert orphan_report.recovery_status == "INCOMPLETE_HISTORY"
        assert orphan_report.replay_result.can_reconstruct_full_task is False

        # 5. Missing Event / invalid transition sequence -> INVALID_HISTORY.
        broken_task = _make_task(t0)
        svc.create_task(broken_task)
        # Write a forbidden jump directly to event journal (QUEUED -> COMPLETED without ASSIGNED/RUNNING/QA).
        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=1),
                event_type="task.status_changed",
                source="provider",
                task_id=broken_task.task_id,
                redacted_payload={"previous_status": "QUEUED", "new_status": "COMPLETED"},
            )
        )
        broken_report = recover_task(store, broken_task.task_id)
        assert broken_report.recovery_status == "INVALID_HISTORY"
        assert broken_report.replay_result.status == "INVALID_HISTORY"

    # 6. Schema version mismatch handling.
    with sqlite_connection(db_path) as conn:
        with sqlite_transaction(conn):
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?);",
                (99, "future_unrecognized", "b" * 64, t0.isoformat()),
            )
    with pytest.raises(SchemaCompatibilityError):
        SQLiteRepositoryStore(db_path)
    with pytest.raises(SchemaCompatibilityError):
        SQLiteEventJournal(db_path)
