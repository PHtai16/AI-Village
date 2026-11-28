"""Unit and Offline SQLite Integration tests for Phase 11 / P11.01 Concurrent Intake & Conversation Isolation."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import ValidationError
import pytest

from core.contracts import (
    Event,
    Run,
    Task,
    new_conversation_id,
    new_run_id,
    new_task_id,
)
from core.departments import (
    ClarificationResolutionRequest,
    ConversationCreateRequest,
    ConversationIsolationError,
    ConversationRecord,
    ConversationTurnRecord,
    DedupCounterRecord,
    DepartmentGovernanceError,
    IncomingSignalRequest,
    IntakeIdempotencyConflictError,
    IntakeRecord,
    IntakeRecoverySnapshot,
    MasterIntakeAckRequest,
    ReceptionDepartmentService,
    ReceptionPersistenceError,
    ReceptionReferenceError,
    ReceptionValidationError,
    RecoveredIntakeEntry,
    TaskRefinementRecord,
    UserRequestIntake,
    UserRequestIntakeOutcome,
)
from core.events import InMemoryEventJournal
from core.governance import PermissionManager, PolicyEvaluator
from core.orchestration import MasterPlanningService, StructuredUserBrief
from core.tasks import (
    ExecutionEvidence,
    HandleTerminationOutcome,
    TransitionContext,
    WorkflowLifecycleController,
)
from storage.db import sqlite_connection
from storage.repositories import SQLiteLifecyclePersistenceAdapter, SQLiteRepositoryStore, recover_task
from storage.repositories.reception_repo import SQLiteReceptionRepository
from tests.departments.conftest import (
    REPO_ROOT,
    DepartmentTestHarness,
    create_task_and_run,
    make_department_harness,
    register_worker_agent,
    utc,
    worker_ctx,
)


class _InMemoryIntakeRepo:
    """Deterministic in-memory fake ReceptionIntakeRepository strictly for pure unit checks."""

    def __init__(self) -> None:
        self.conversations: dict[str, ConversationRecord] = {}
        self.turns: list[ConversationTurnRecord] = []
        self.intakes: dict[str, IntakeRecord] = {}
        self.outcomes_by_idem: dict[tuple[str, str], tuple[str, UserRequestIntakeOutcome]] = {}
        self.tasks: dict[str, Task] = {}
        self.runs: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.refinements: list[TaskRefinementRecord] = []
        self.inbox: list[Any] = []
        self.dedup: dict[str, DedupCounterRecord] = {}
        self.events: list[Event] = []
        self.notifications: list[Any] = []

    def execute_transaction(self, processor: Any) -> Any:
        return processor(self)

    def find_idempotent_intake(self, scope: str, key: str) -> tuple[str, UserRequestIntakeOutcome] | None:
        return self.outcomes_by_idem.get((scope, key))

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        return self.conversations.get(conversation_id)

    def create_conversation(
        self,
        record: ConversationRecord,
        audit_event: Event | None = None,
    ) -> ConversationRecord:
        if audit_event is not None:
            self.events.append(audit_event)
        self.conversations[record.conversation_id] = record
        return record

    def list_conversations(self, *, owner_actor_id: str | None = None) -> list[ConversationRecord]:
        return [
            c
            for c in self.conversations.values()
            if owner_actor_id is None or c.owner_actor_id == owner_actor_id
        ]

    def list_conversation_turns(self, conversation_id: str) -> list[ConversationTurnRecord]:
        return [t for t in self.turns if t.conversation_id == conversation_id]

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def get_task_snapshot(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def get_run_task_id(self, run_id: str) -> str | None:
        return self.runs.get(run_id)

    def get_task_conversation_id(self, task_id: str) -> str | None:
        return self.links.get(task_id)

    def list_active_tasks_for_conversation(self, conversation_id: str) -> list[Task]:
        return [
            self.tasks[tid]
            for tid, cid in self.links.items()
            if cid == conversation_id
            and tid in self.tasks
            and self.tasks[tid].status not in {"COMPLETED", "FAILED", "CANCELLED"}
        ]

    def list_running_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == "RUNNING"]

    def create_task(self, task: Task, task_created_event: Event) -> Task:
        self.tasks[task.task_id] = task
        self.events.append(task_created_event)
        return task

    def append_event(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def insert_intake(self, record: IntakeRecord, outcome: UserRequestIntakeOutcome) -> IntakeRecord:
        self.intakes[record.intake_id] = record
        if record.idempotency_key is not None:
            self.outcomes_by_idem[(record.idempotency_scope, record.idempotency_key)] = (
                record.request_fingerprint,
                outcome,
            )
        return record

    def update_intake_outcome(self, intake_id: str, outcome: UserRequestIntakeOutcome) -> None:
        rec = self.intakes[intake_id]
        if rec.idempotency_key is not None:
            self.outcomes_by_idem[(rec.idempotency_scope, rec.idempotency_key)] = (
                rec.request_fingerprint,
                outcome,
            )

    def link_conversation_task(self, **kwargs: Any) -> None:
        self.links[kwargs["task_id"]] = kwargs["conversation_id"]

    def append_conversation_turn(self, turn: ConversationTurnRecord) -> ConversationTurnRecord:
        idx = len([t for t in self.turns if t.conversation_id == turn.conversation_id]) + 1
        out = turn.model_copy(update={"turn_index": idx})
        self.turns.append(out)
        return out

    def append_task_refinement(self, refinement: TaskRefinementRecord) -> TaskRefinementRecord:
        seq = len([r for r in self.refinements if r.task_id == refinement.task_id]) + 1
        out = refinement.model_copy(update={"sequence_in_task": seq})
        self.refinements.append(out)
        return out

    def insert_action_inbox_item(self, item: Any, *, audit_event_id: str) -> Any:
        _ = audit_event_id
        self.inbox.append(item)
        return item

    def save_notification(self, notification: Any) -> Any:
        self.notifications.append(notification)
        return notification

    def get_intake_for_update(self, intake_id: str) -> IntakeRecord | None:
        return self.intakes.get(intake_id)

    def get_intake_request(self, intake_id: str) -> IntakeRecord | None:
        return self.intakes.get(intake_id)

    def list_intake_requests(self, **kwargs: Any) -> list[IntakeRecord]:
        return list(self.intakes.values())

    def list_task_refinements(self, task_id: str) -> list[TaskRefinementRecord]:
        return [r for r in self.refinements if r.task_id == task_id]

    def list_action_inbox(self, **kwargs: Any) -> list[Any]:
        return list(self.inbox)

    def update_intake_master_ack(self, **kwargs: Any) -> IntakeRecord:
        rec = self.intakes[kwargs["intake_id"]]
        updated = rec.model_copy(
            update={
                "processing_status": "MASTER_ACKNOWLEDGED",
                "master_handoff_status": "ACKNOWLEDGED_BY_MASTER",
                "record_version": rec.record_version + 1,
            }
        )
        self.intakes[rec.intake_id] = updated
        return updated

    def update_intake_resolution(self, **kwargs: Any) -> IntakeRecord:
        rec = self.intakes[kwargs["intake_id"]]
        updated = rec.model_copy(
            update={
                "classification": kwargs["classification"],
                "decision": kwargs["decision"],
                "processing_status": kwargs["processing_status"],
                "master_handoff_status": kwargs["master_handoff_status"],
                "related_task_id": kwargs["related_task_id"],
                "created_task_id": kwargs["created_task_id"],
                "record_version": rec.record_version + 1,
            }
        )
        self.intakes[rec.intake_id] = updated
        return updated

    def get_dedup_state(self, dedup_scope_key: str) -> DedupCounterRecord | None:
        return self.dedup.get(dedup_scope_key)

    def upsert_dedup_state(self, record: DedupCounterRecord) -> DedupCounterRecord:
        self.dedup[record.dedup_scope_key] = record
        return record

    def recover_intake_state(self) -> IntakeRecoverySnapshot:
        return IntakeRecoverySnapshot(
            intakes=[RecoveredIntakeEntry(intake=r) for r in self.intakes.values()],
            conversations=list(self.conversations.values()),
            open_inbox_items=list(self.inbox),
        )


class _ObservableStopHandle:
    """Minimal observable OwnedExecutionHandle for real WorkflowLifecycleController cancellation tests."""

    def __init__(self, execution_id: str, owner_token: str, *, pid: int | None = 51500) -> None:
        self._execution_id = execution_id
        self._owner_token = owner_token
        self._pid = pid
        self._running = True

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def owner_token(self) -> str:
        return self._owner_token

    @property
    def pid(self) -> int | None:
        return self._pid

    def is_running(self) -> bool:
        return self._running

    def request_cancel(
        self,
        *,
        owner_token: str,
        wait: bool = True,
        timeout_seconds: float = 5.0,
    ) -> HandleTerminationOutcome:
        assert owner_token == self._owner_token
        self._running = False
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=True,
            already_terminal=False,
            terminal_status="CANCELLED",
            exit_code=1,
            mechanism="test_observable_stop",
        )

    def wait_for_exit(self, *, timeout_seconds: float = 5.0) -> HandleTerminationOutcome:
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=not self._running,
            already_terminal=not self._running,
            terminal_status="STILL_RUNNING" if self._running else "CANCELLED",
        )


def _new_reception(
    h: DepartmentTestHarness,
    *,
    intake_repo: SQLiteReceptionRepository | None = None,
) -> ReceptionDepartmentService:
    return ReceptionDepartmentService(
        permission_manager=h.permissions,
        journal=h.store.events,
        notification_store=h.store.metadata,
        intake_repository=intake_repo or h.store.reception,
    )


def _seed_running_task_in_conversation(
    h: DepartmentTestHarness,
    reception: ReceptionDepartmentService,
    *,
    title: str = "Primary analytics pipeline",
    description: str = "Compile verified regional sales dataset into output CSV.",
    timestamp: datetime,
    assigned_agent_ids: list[str] | None = None,
) -> tuple[str, str, str]:
    """Create a RUNNING Task A via Reception + WorkflowLifecycle so Conversation & Task history match reality."""
    conv = reception.open_conversation(
        ConversationCreateRequest(title="Conversation Alpha", timestamp=timestamp),
        auth_context=h.alice,
    )
    intake_a = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv.conversation_id,
            title=title,
            summary=description,
            intent_mode="NEW_TASK",
            timestamp=timestamp + timedelta(seconds=1),
        ),
        auth_context=h.alice,
    )
    assert intake_a.created_task is not None
    task_a = intake_a.created_task.task_id
    run_a = new_run_id()
    agents = assigned_agent_ids or ["it.worker"]

    h.workflows.apply_task_transition(
        task_a,
        "ASSIGNED",
        TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=timestamp + timedelta(seconds=2),
            reason="Assigned worker to Task A",
            assigned_agent_ids=agents,
        ),
    )
    h.workflows.create_run(
        Run(
            run_id=run_a,
            task_id=task_a,
            runtime_id="custom.master_worker_v1",
            status="PENDING",
        )
    )
    start_res = h.workflows.apply_paired_transition(
        "start",
        task_a,
        run_a,
        TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=timestamp + timedelta(seconds=3),
            reason="Task A actively running",
            execution_evidence=ExecutionEvidence(
                source="core_runtime",
                run_id=run_a,
                reason="Worker session running",
            ),
        ),
    )
    assert start_res.accepted is True
    return conv.conversation_id, task_a, run_a


# =============================================================================
# 1. Pure Unit Verification & Architecture / DB Trigger Invariants
# =============================================================================


def test_p11_01_00_ast_purity_and_migration_0016_immutability(tmp_path: Path) -> None:
    """Verify AST isolation for core/departments/reception* and append-only triggers on 0016 tables."""
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
    for rel in ("core/departments/reception_models.py", "core/departments/reception.py"):
        py_path = REPO_ROOT / rel
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), f"Forbidden import '{alias.name}' in {rel}"
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden), f"Forbidden import-from '{node.module}' in {rel}"

    # Pure unit test with in-memory repository (zero SQLite dependency).
    t0 = utc(9, 0, 0)
    journal = InMemoryEventJournal()
    pm = PermissionManager(evaluator=PolicyEvaluator(), journal=journal)
    fake_repo = _InMemoryIntakeRepo()
    alice = make_department_harness(tmp_path).alice  # only borrows TrustedAuthContext shape; close next line
    reception = ReceptionDepartmentService(permission_manager=pm, journal=journal, intake_repository=fake_repo)
    out = reception.receive_user_request(
        UserRequestIntake(
            title="Unit test standalone task",
            summary="Verify in-memory unit classification without database",
            intent_mode="AUTO",
            timestamp=t0,
        ),
        auth_context=alice,
    )
    assert out.classification == "QUEUED"
    assert out.created_task is not None
    assert out.created_task.status == "QUEUED"

    # Verify SQLite append-only triggers reject UPDATE/DELETE on conversation_turns and task_refinements.
    h = make_department_harness(tmp_path / "db_triggers")
    try:
        svc = _new_reception(h)
        conv_id, task_a, run_a = _seed_running_task_in_conversation(h, svc, timestamp=t0)
        ref_out = svc.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_id,
                intent_mode="REFINEMENT",
                target_task_id=task_a,
                target_run_id=run_a,
                summary="Include currency normalization column in CSV export",
                timestamp=t0 + timedelta(seconds=10),
            ),
            auth_context=h.alice,
        )
        assert ref_out.task_refinement is not None
        with sqlite_connection(h.db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "UPDATE conversation_turns SET message_text = 'tampered' WHERE turn_id = ?;",
                    (ref_out.conversation_turn.turn_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "DELETE FROM conversation_turns WHERE turn_id = ?;",
                    (ref_out.conversation_turn.turn_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "UPDATE task_refinements SET refinement_text = 'tampered' WHERE refinement_id = ?;",
                    (ref_out.task_refinement.refinement_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "DELETE FROM task_refinements WHERE refinement_id = ?;",
                    (ref_out.task_refinement.refinement_id,),
                )
    finally:
        h.store.close()


# =============================================================================
# 2. Case A, Case B, Case C — Concurrent Intake, Refinement, and Clarification Hold
# =============================================================================


def test_p11_01_01_to_06_concurrent_independent_refinement_and_ambiguous_hold(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Requirements #1, #2, #3, #4, #5, #6: Case A, Case B, Case C, and conversation isolation."""
    t0 = utc(10, 0, 0)
    reception = _new_reception(dept_harness)
    conv_a, task_a, run_a = _seed_running_task_in_conversation(
        dept_harness,
        reception,
        timestamp=t0,
        assigned_agent_ids=["it.worker", "qa.verifier"],
    )
    snap_a_before = dept_harness.store.tasks.get_stored_task(task_a)
    assert snap_a_before is not None and snap_a_before.task.status == "RUNNING"
    turns_a_before = reception.list_conversation_turns(conv_a, auth_context=dept_harness.alice)

    # [Case A — #1, #2, #3, #4] New independent Task B in a separate conversation while A is RUNNING.
    conv_b_rec = reception.open_conversation(
        ConversationCreateRequest(title="Conversation Beta", timestamp=t0 + timedelta(seconds=5)),
        auth_context=dept_harness.alice,
    )
    conv_b = conv_b_rec.conversation_id
    out_b = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_b,
            summary="Draft legal compliance checklist for European launch",
            target_department="service",
            priority="HIGH",
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=dept_harness.alice,
    )
    assert out_b.classification == "QUEUED"
    assert out_b.decision == "CREATED_INDEPENDENT_TASK"
    assert out_b.processing_status == "QUEUED_FOR_MASTER"
    assert out_b.master_handoff_status == "PENDING_MASTER_REVIEW"
    assert out_b.created_task is not None
    task_b = out_b.created_task.task_id
    assert task_b != task_a
    assert out_b.created_task.status == "QUEUED"
    assert out_b.created_task.active_run_id is None
    assert out_b.observed_running_task_id == task_a
    assert out_b.escalated_to_master is True

    # Task A and Conversation Alpha remain 100% untouched after receiving B!
    snap_a_after_b = dept_harness.store.tasks.get_stored_task(task_a)
    assert snap_a_after_b is not None
    assert snap_a_after_b.version == snap_a_before.version
    assert snap_a_after_b.task == snap_a_before.task
    assert snap_a_after_b.baseline == snap_a_before.baseline
    turns_a_after_b = reception.list_conversation_turns(conv_a, auth_context=dept_harness.alice)
    assert turns_a_after_b == turns_a_before
    turns_b = reception.list_conversation_turns(conv_b, auth_context=dept_harness.alice)
    assert len(turns_b) == 1 and turns_b[0].task_id == task_b

    # [Case A same conversation with explicit NEW_TASK]
    out_c = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="NEW_TASK",
            title="Parallel cost forecast",
            summary="Build separate Q4 budget forecast report in parallel",
            timestamp=t0 + timedelta(seconds=7),
        ),
        auth_context=dept_harness.alice,
    )
    assert out_c.created_task is not None
    assert out_c.created_task.task_id not in {task_a, task_b}
    assert out_c.created_task.status == "QUEUED"
    assert dept_harness.store.tasks.get_task(task_a).status == "RUNNING"  # type: ignore[union-attr]

    # [Case B — #5] Refinement targeting running Task A attaches strictly to A without overwriting history.
    ref1 = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="REFINEMENT",
            target_task_id=task_a,
            target_run_id=run_a,
            summary="Also include EMEA currency rates in the output CSV",
            timestamp=t0 + timedelta(seconds=8),
        ),
        auth_context=dept_harness.alice,
    )
    ref2 = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="REFINEMENT",
            target_task_id=task_a,
            summary="Sort output rows descending by net revenue",
            timestamp=t0 + timedelta(seconds=9),
        ),
        auth_context=dept_harness.alice,
    )
    assert ref1.classification == "APPENDED"
    assert ref1.decision == "APPENDED_REFINEMENT"
    assert ref1.created_task is None
    assert ref1.related_task_id == task_a
    assert ref1.task_refinement is not None and ref1.task_refinement.sequence_in_task == 1
    assert ref2.task_refinement is not None and ref2.task_refinement.sequence_in_task == 2
    refinements = reception.list_task_refinements(task_a, auth_context=dept_harness.alice)
    assert [r.sequence_in_task for r in refinements] == [1, 2]
    assert refinements[0].submitted_by == "operator.alice"
    assert refinements[0].source_channel == "user"
    # Original Task A title, description, baseline_json, active_run_id, and RUNNING status stay preserved!
    snap_a_after_ref = dept_harness.store.tasks.get_stored_task(task_a)
    assert snap_a_after_ref is not None
    assert snap_a_after_ref.task.status == "RUNNING"
    assert snap_a_after_ref.task.description == snap_a_before.task.description
    assert snap_a_after_ref.baseline == snap_a_before.baseline
    assert snap_a_after_ref.task.active_run_id == run_a

    # [Case C — #6] Ambiguous AUTO message inside conversation with active tasks is HELD, never guessed!
    tasks_count_before = len(dept_harness.store.tasks.list_tasks())
    ambig1 = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="AUTO",
            summary="Please adjust the tax bracket formatting as well",
            timestamp=t0 + timedelta(seconds=10),
        ),
        auth_context=dept_harness.alice,
    )
    assert ambig1.classification == "CLARIFICATION_REQUIRED"
    assert ambig1.decision == "HELD_FOR_CLARIFICATION"
    assert ambig1.processing_status == "NEEDS_CLARIFICATION"
    assert ambig1.reason_code == "ACTIVE_TASK_IN_SAME_CONVERSATION"
    assert ambig1.created_task is None
    assert ambig1.related_task_id is None
    assert ambig1.task_refinement is None
    assert len(dept_harness.store.tasks.list_tasks()) == tasks_count_before
    assert len(reception.list_task_refinements(task_a)) == 2

    # Vague fragment in fresh conversation also holds cleanly without creating garbage Task.
    ambig2 = reception.receive_user_request(
        UserRequestIntake(
            intent_mode="AUTO",
            summary="bổ sung thêm phần đó",
            timestamp=t0 + timedelta(seconds=11),
        ),
        auth_context=dept_harness.alice,
    )
    assert ambig2.classification == "CLARIFICATION_REQUIRED"
    assert ambig2.reason_code == "UNDERSPECIFIED_OR_DANGLING_REQUEST"
    assert len(dept_harness.store.tasks.list_tasks()) == tasks_count_before

    # Master / operator can explicitly resolve ambig1 into a REFINEMENT on Task A under optimistic lock.
    with pytest.raises(Exception) as stale_exc:
        reception.resolve_intake_clarification(
            ClarificationResolutionRequest(
                intake_id=ambig1.intake_id,
                expected_record_version=99,
                resolution_mode="REFINEMENT",
                target_task_id=task_a,
                decision_note="Operator confirmed this belongs to Task A",
                timestamp=t0 + timedelta(seconds=12),
            ),
            auth_context=dept_harness.master,
        )
    assert getattr(stale_exc.value, "error_code", None) == "STALE_INTAKE_VERSION"

    resolved = reception.resolve_intake_clarification(
        ClarificationResolutionRequest(
            intake_id=ambig1.intake_id,
            expected_record_version=1,
            resolution_mode="REFINEMENT",
            target_task_id=task_a,
            decision_note="Operator confirmed this belongs to Task A",
            timestamp=t0 + timedelta(seconds=13),
        ),
        auth_context=dept_harness.master,
    )
    assert resolved.processing_status == "RESOLVED"
    assert resolved.related_task_id == task_a
    assert len(reception.list_task_refinements(task_a)) == 3

    # And resolve ambig2 as DISMISS cleanly.
    dismissed = reception.resolve_intake_clarification(
        ClarificationResolutionRequest(
            intake_id=ambig2.intake_id,
            expected_record_version=1,
            resolution_mode="DISMISS",
            decision_note="User retracted vague follow-up",
            timestamp=t0 + timedelta(seconds=14),
        ),
        auth_context=dept_harness.master,
    )
    assert dismissed.master_handoff_status == "DISMISSED_BY_MASTER"
    assert dismissed.processing_status == "REJECTED"


# =============================================================================
# 3. Case D — Idempotency, Identical-Content Distinct Identity, and Concurrency
# =============================================================================


def test_p11_01_07_08_09_idempotency_identical_text_and_concurrent_requests(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Requirements #7, #8, #9: idempotent retries, same-text non-merging, and thread concurrency."""
    t0 = utc(11, 0, 0)
    reception = _new_reception(dept_harness)
    conv_id = reception.open_conversation(
        ConversationCreateRequest(title="Idem & Concurrency Conv", timestamp=t0),
        auth_context=dept_harness.alice,
    ).conversation_id

    # 7. Exact retry with same idempotency_key replays without duplicating Tasks, Inbox, or Events.
    req1 = UserRequestIntake(
        conversation_id=conv_id,
        intent_mode="NEW_TASK",
        summary="Generate quarterly audit verification pack",
        idempotency_key="idem-pack-001",
        timestamp=t0 + timedelta(seconds=1),
    )
    first = reception.receive_user_request(req1, auth_context=dept_harness.alice)
    assert first.idempotent_replay is False
    assert first.created_task is not None
    events_after_first = dept_harness.store.events.count()
    inbox_after_first = len(reception.list_action_inbox(conversation_id=conv_id))

    replay = reception.receive_user_request(
        req1.model_copy(
            update={
                "intake_id": f"intk_retry_{t0.second:04d}aa",
                "timestamp": t0 + timedelta(seconds=2),
            }
        ),
        auth_context=dept_harness.alice,
    )
    assert replay.idempotent_replay is True
    assert replay.intake_id == first.intake_id
    assert replay.created_task is not None
    assert replay.created_task.task_id == first.created_task.task_id
    assert len(reception.list_action_inbox(conversation_id=conv_id)) == inbox_after_first
    # Only the governance authorization read check may be appended; ZERO duplicate intake/task/notification events.
    assert [
        e.event.event_id
        for e in dept_harness.store.events.read_entries(event_type="task.created")
    ] == [e.event.event_id for e in dept_harness.store.events.read_entries(event_type="task.created")[:1]]
    assert len(dept_harness.store.tasks.list_tasks()) == 1

    # Conflicting payload under same idempotency key fails closed!
    with pytest.raises(IntakeIdempotencyConflictError) as conflict_exc:
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_id,
                intent_mode="NEW_TASK",
                summary="Different summary trying to reuse idem-pack-001",
                idempotency_key="idem-pack-001",
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=dept_harness.alice,
        )
    assert conflict_exc.value.error_code == "IDEMPOTENCY_CONFLICT"

    # 8. Two distinct requests with IDENTICAL text content stay two separate Tasks & Intakes!
    dup_text = "Export monthly revenue breakdown report"
    distinct_a = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_id,
            intent_mode="NEW_TASK",
            summary=dup_text,
            idempotency_key="monthly-rev-jan",
            timestamp=t0 + timedelta(seconds=4),
        ),
        auth_context=dept_harness.alice,
    )
    distinct_b = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_id,
            intent_mode="NEW_TASK",
            summary=dup_text,
            idempotency_key="monthly-rev-feb",
            timestamp=t0 + timedelta(seconds=5),
        ),
        auth_context=dept_harness.alice,
    )
    distinct_c = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_id,
            intent_mode="NEW_TASK",
            summary=dup_text,
            idempotency_key=None,
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=dept_harness.alice,
    )
    created_ids = {
        distinct_a.created_task.task_id,  # type: ignore[union-attr]
        distinct_b.created_task.task_id,  # type: ignore[union-attr]
        distinct_c.created_task.task_id,  # type: ignore[union-attr]
    }
    assert len(created_ids) == 3

    # Verify triage_incoming_signal also keeps two distinct QUEUED signals with identical summary separate.
    sig1 = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="user",
            summary="Identical signal triage message",
            timestamp=t0 + timedelta(seconds=7),
        ),
        auth_context=dept_harness.alice,
    )
    sig2 = reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="user",
            summary="Identical signal triage message",
            timestamp=t0 + timedelta(seconds=8),
        ),
        auth_context=dept_harness.alice,
    )
    assert sig1.deduplicated is False and sig2.deduplicated is False

    # 9. Concurrent multi-threaded intake: 4 distinct requests + 4 concurrent retries on one key.
    _ = events_after_first
    base_tasks_before = len(dept_harness.store.tasks.list_tasks())

    def _submit_worker(idx: int) -> UserRequestIntakeOutcome:
        if idx < 4:
            return reception.receive_user_request(
                UserRequestIntake(
                    conversation_id=conv_id,
                    intent_mode="NEW_TASK",
                    summary=f"Concurrent distinct batch item {idx} for verification",
                    idempotency_key=f"concurrent-distinct-{idx}",
                    timestamp=t0 + timedelta(seconds=20 + idx),
                ),
                auth_context=dept_harness.alice,
            )
        return reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_id,
                intent_mode="NEW_TASK",
                summary="Shared retry storm concurrent task request",
                idempotency_key="concurrent-shared-key",
                timestamp=t0 + timedelta(seconds=30),
            ),
            auth_context=dept_harness.alice,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_submit_worker, range(8)))

    assert len(outcomes) == 8
    distinct_outcomes = outcomes[:4]
    retry_outcomes = outcomes[4:]
    assert len({o.created_task.task_id for o in distinct_outcomes if o.created_task}) == 4
    shared_task_ids = {o.created_task.task_id for o in retry_outcomes if o.created_task}
    assert len(shared_task_ids) == 1
    assert sum(1 for o in retry_outcomes if not o.idempotent_replay) == 1
    assert sum(1 for o in retry_outcomes if o.idempotent_replay) == 3
    assert len(dept_harness.store.tasks.list_tasks()) == base_tasks_before + 5


# =============================================================================
# 4. Terminal Task & Concurrent Cancellation Scenarios
# =============================================================================


def test_p11_01_10_11_terminal_task_and_concurrent_cancellation(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Requirements #10, #11: terminal Task A never accepts refinements; concurrent cancel isolates B."""
    t0 = utc(12, 0, 0)
    reception = _new_reception(dept_harness)
    conv_a, task_a, run_a = _seed_running_task_in_conversation(
        dept_harness,
        reception,
        timestamp=t0,
    )

    # Cancel Task A cleanly BEFORE Request B arrives.
    lifecycle = WorkflowLifecycleController(SQLiteLifecyclePersistenceAdapter(dept_harness.store))
    lifecycle.register_owned_execution(
        task_a,
        run_a,
        _ObservableStopHandle("exec-task-a", "owner-tok-a"),
        timestamp=t0 + timedelta(seconds=4),
    )
    cancel_res = lifecycle.request_cancel(
        task_a,
        run_a,
        actor_id="operator.alice",
        actor_kind="operator",
        reason="User cancelled Task A",
        timestamp=t0 + timedelta(seconds=5),
        wait=True,
    )
    assert cancel_res.cancel_confirmed is True
    assert dept_harness.store.tasks.get_task(task_a).status == "CANCELLED"  # type: ignore[union-attr]

    # 10a. Independent Request B succeeds cleanly even though Task A became terminal.
    out_b = reception.receive_user_request(
        UserRequestIntake(
            intent_mode="NEW_TASK",
            summary="Fresh task created after Task A cancelled",
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=dept_harness.alice,
    )
    assert out_b.classification == "QUEUED"
    assert out_b.created_task is not None and out_b.created_task.status == "QUEUED"

    # 10b. Refinement targeting already-terminal Task A does NOT mutate/attach to A; held for clarification!
    held = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="REFINEMENT",
            target_task_id=task_a,
            summary="Forgot to add extra filter to cancelled Task A",
            timestamp=t0 + timedelta(seconds=7),
        ),
        auth_context=dept_harness.alice,
    )
    assert held.classification == "CLARIFICATION_REQUIRED"
    assert held.reason_code == "TARGET_TASK_ALREADY_TERMINAL"
    assert held.observed_target_task_status == "CANCELLED"
    assert held.task_refinement is None
    assert held.related_task_id is None
    assert reception.list_task_refinements(task_a) == []
    assert dept_harness.store.tasks.get_task(task_a).status == "CANCELLED"  # type: ignore[union-attr]

    # Signal-level refinement toward terminal Task A also fails closed cleanly.
    with pytest.raises(ReceptionValidationError) as sig_err:
        reception.triage_incoming_signal(
            IncomingSignalRequest(
                source_channel="user",
                summary="Refinement signal for already cancelled task",
                task_id=task_a,
                is_refinement_for_running_task=True,
                timestamp=t0 + timedelta(seconds=8),
            ),
            auth_context=dept_harness.master,
        )
    assert sig_err.value.error_code == "TARGET_TASK_ALREADY_TERMINAL"

    # 11. Concurrent cancellation race on Task A2 while receiving independent B2 & racing refinement R2.
    conv_a2, task_a2, run_a2 = _seed_running_task_in_conversation(
        dept_harness,
        reception,
        title="Concurrent cancel race task",
        timestamp=t0 + timedelta(seconds=20),
    )
    lifecycle.register_owned_execution(
        task_a2,
        run_a2,
        _ObservableStopHandle("exec-task-a2", "owner-tok-a2"),
        timestamp=t0 + timedelta(seconds=24),
    )

    def _cancel_a2() -> str:
        res = lifecycle.request_cancel(
            task_a2,
            run_a2,
            actor_id="operator.alice",
            actor_kind="operator",
            reason="Concurrent cancel during intake",
            timestamp=t0 + timedelta(seconds=25),
            wait=True,
        )
        return res.status

    def _send_b2() -> UserRequestIntakeOutcome:
        return reception.receive_user_request(
            UserRequestIntake(
                intent_mode="NEW_TASK",
                summary="Independent Task B2 submitted during Task A2 cancellation",
                timestamp=t0 + timedelta(seconds=26),
            ),
            auth_context=dept_harness.alice,
        )

    def _send_race_ref() -> UserRequestIntakeOutcome:
        return reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_a2,
                intent_mode="REFINEMENT",
                target_task_id=task_a2,
                summary="Racing addendum while Task A2 may be cancelling",
                timestamp=t0 + timedelta(seconds=25),
            ),
            auth_context=dept_harness.alice,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_cancel = pool.submit(_cancel_a2)
        f_b2 = pool.submit(_send_b2)
        f_race = pool.submit(_send_race_ref)
        cancel_status = f_cancel.result()
        out_b2 = f_b2.result()
        out_race = f_race.result()

    assert cancel_status == "CANCEL_CONFIRMED"
    assert dept_harness.store.tasks.get_task(task_a2).status == "CANCELLED"  # type: ignore[union-attr]
    assert out_b2.classification == "QUEUED" and out_b2.created_task is not None
    assert out_race.classification in {"APPENDED", "CLARIFICATION_REQUIRED"}
    # Replay verification on Task A2 remains valid with no corrupt post-terminal events!
    rec_a2 = recover_task(dept_harness.store, task_a2)
    assert rec_a2.recovery_status == "VERIFIED_CLEAN"
    assert rec_a2.snapshot_matches_replay is True


# =============================================================================
# 5. Persistence Failure, Rollback, Event Consistency & Restart Recovery
# =============================================================================


def test_p11_01_12_13_14_15_failure_rollback_events_and_restart_recovery(
    tmp_path: Path,
) -> None:
    """Covers Requirements #12, #13, #14, #15: fail-closed persistence, rollback, replay, and restart."""
    t0 = utc(13, 0, 0)
    h1 = make_department_harness(tmp_path)

    # 12. Missing durable repository fails closed on P11.01 intake operations.
    volatile_reception = ReceptionDepartmentService(
        permission_manager=h1.permissions,
        journal=h1.store.events,
        notification_store=h1.store.metadata,
        intake_repository=None,
    )
    with pytest.raises(ReceptionPersistenceError) as repo_err:
        volatile_reception.receive_user_request(
            UserRequestIntake(
                intent_mode="NEW_TASK",
                summary="Should fail closed when intake repository is absent",
                timestamp=t0,
            ),
            auth_context=h1.alice,
        )
    assert repo_err.value.error_code == "INTAKE_REPOSITORY_UNAVAILABLE"

    # Seed Task A running and run fault injection rollback test.
    durable_reception = _new_reception(h1)
    conv_a, task_a, run_a = _seed_running_task_in_conversation(h1, durable_reception, timestamp=t0)

    def _failing_hook(conn: sqlite3.Connection) -> None:
        _ = conn
        raise sqlite3.OperationalError("Simulated disk write interruption before commit")

    faulty_repo = SQLiteReceptionRepository(h1.store, before_commit_hook=_failing_hook)
    faulty_reception = _new_reception(h1, intake_repo=faulty_repo)

    tasks_before = len(h1.store.tasks.list_tasks())
    intakes_before = len(durable_reception.list_intake_requests())
    inbox_before = len(durable_reception.list_action_inbox())
    notifs_before = len(h1.store.metadata.list_notifications())
    task_created_ev_before = len(h1.store.events.read_entries(event_type="task.created"))
    rx_ev_before = len(h1.store.events.read_entries(event_type="reception.request_received"))
    notif_ev_before = len(h1.store.events.read_entries(event_type="notification.created"))

    # 13. Transaction rollback: zero partial Tasks, Intakes, Turns, Inbox, Notifications, or domain Events!
    with pytest.raises(ReceptionPersistenceError, match="rolled back"):
        faulty_reception.receive_user_request(
            UserRequestIntake(
                intent_mode="NEW_TASK",
                summary="Task must completely roll back when commit fails",
                idempotency_key="rollback-key-01",
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=h1.alice,
        )

    assert len(h1.store.tasks.list_tasks()) == tasks_before
    assert len(durable_reception.list_intake_requests()) == intakes_before
    assert len(durable_reception.list_action_inbox()) == inbox_before
    assert len(h1.store.metadata.list_notifications()) == notifs_before
    assert len(h1.store.events.read_entries(event_type="task.created")) == task_created_ev_before
    assert len(h1.store.events.read_entries(event_type="reception.request_received")) == rx_ev_before
    assert len(h1.store.events.read_entries(event_type="notification.created")) == notif_ev_before

    # Submit valid Task B, Refinement on A, Ambiguous C, and Alert Storm prior to restart.
    conv_b = durable_reception.open_conversation(
        ConversationCreateRequest(title="Beta Survives Restart", timestamp=t0 + timedelta(seconds=10)),
        auth_context=h1.alice,
    ).conversation_id
    out_b = durable_reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_b,
            intent_mode="NEW_TASK",
            summary="Persist Task B across process restart",
            idempotency_key="restart-b-001",
            correlation_id="corr-restart-b",
            timestamp=t0 + timedelta(seconds=11),
        ),
        auth_context=h1.alice,
    )
    assert out_b.created_task is not None
    task_b = out_b.created_task.task_id

    ref_a = durable_reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="REFINEMENT",
            target_task_id=task_a,
            target_run_id=run_a,
            summary="Persisted refinement on running Task A",
            timestamp=t0 + timedelta(seconds=12),
        ),
        auth_context=h1.alice,
    )
    ambig_c = durable_reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_a,
            intent_mode="AMBIGUOUS",
            summary="Could be refinement or new analysis",
            timestamp=t0 + timedelta(seconds=13),
        ),
        auth_context=h1.alice,
    )
    anom1 = durable_reception.triage_incoming_signal(
        IncomingSignalRequest(
            source_channel="telemetry",
            summary="Latency spike alert",
            task_id=task_a,
            run_id=run_a,
            is_anomaly=True,
            dedup_key="latency-spike-a",
            timestamp=t0 + timedelta(seconds=14),
        ),
        auth_context=h1.alice,
    )
    assert anom1.deduplicated is False and anom1.occurrence_count == 1

    # 14. Verify full Event traceability and Task replay integrity.
    rx_entries = h1.store.events.read_entries(task_id=task_b, event_type="reception.request_received")
    assert len(rx_entries) == 1
    rx_ev = rx_entries[0].event
    assert rx_ev.correlation_id == "corr-restart-b"
    assert rx_ev.redacted_payload["intake_id"] == out_b.intake_id
    assert rx_ev.redacted_payload["conversation_id"] == conv_b
    assert rx_ev.redacted_payload["decision"] == "CREATED_INDEPENDENT_TASK"
    assert rx_ev.redacted_payload["processing_status"] == "QUEUED_FOR_MASTER"

    rec_a = recover_task(h1.store, task_a)
    rec_b = recover_task(h1.store, task_b)
    assert rec_a.recovery_status == "NEEDS_RECONCILIATION"
    assert rec_a.snapshot_matches_replay is True
    assert rec_b.recovery_status == "VERIFIED_CLEAN"
    assert rec_b.snapshot_matches_replay is True

    db_path = h1.db_path
    h1.store.close()

    # 15. Reopen fresh SQLiteRepositoryStore and ReceptionDepartmentService simulating restart!
    store2 = SQLiteRepositoryStore(db_path)
    try:
        pm2 = PermissionManager(evaluator=PolicyEvaluator(), journal=store2.events)
        rx2 = ReceptionDepartmentService(
            permission_manager=pm2,
            journal=store2.events,
            notification_store=store2.metadata,
            intake_repository=store2.reception,
        )
        # Rehydrated conversations, turns, refinements, and inbox match cleanly!
        assert len(rx2.list_conversation_turns(conv_a, auth_context=h1.alice)) == 3
        assert len(rx2.list_conversation_turns(conv_b, auth_context=h1.alice)) == 1
        assert len(rx2.list_task_refinements(task_a, auth_context=h1.alice)) == 1
        assert len(rx2.list_action_inbox()) >= 4

        # Retrying prior idempotency_key AFTER restart STILL replays cleanly with zero new Tasks!
        post_restart_retry = rx2.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_b,
                intent_mode="NEW_TASK",
                summary="Persist Task B across process restart",
                idempotency_key="restart-b-001",
                timestamp=t0 + timedelta(seconds=30),
            ),
            auth_context=h1.alice,
        )
        assert post_restart_retry.idempotent_replay is True
        assert post_restart_retry.intake_id == out_b.intake_id
        assert post_restart_retry.created_task is not None
        assert post_restart_retry.created_task.task_id == task_b

        # Alert deduplication counter also survived restart cleanly!
        anom2 = rx2.triage_incoming_signal(
            IncomingSignalRequest(
                source_channel="telemetry",
                summary="Latency spike alert",
                task_id=task_a,
                run_id=run_a,
                is_anomaly=True,
                dedup_key="latency-spike-a",
                timestamp=t0 + timedelta(seconds=31),
            ),
            auth_context=h1.alice,
        )
        assert anom2.deduplicated is True
        assert anom2.occurrence_count == 2
        assert anom2.notification_id == anom1.notification_id

        # Recovery snapshot separates pending/awaiting-Master vs running vs needs-clarification,
        # and NEVER auto-starts queued Task B after restart!
        snap = rx2.recover_intake_state()
        assert store2.tasks.get_task(task_b).status == "QUEUED"  # type: ignore[union-attr]
        assert snap.queued_tasks == 1
        assert snap.running_tasks == 1
        assert snap.needs_clarification == 1
        assert snap.awaiting_master_review >= 2
        _ = (ref_a, ambig_c)
    finally:
        store2.close()


# =============================================================================
# 6. Security, Governance, Invalid References & Secret Redaction
# =============================================================================


def test_p11_01_16_17_18_security_invalid_refs_and_secret_redaction(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Requirements #16, #17, #18: Governance permissions, invalid references, and redaction."""
    t0 = utc(14, 0, 0)
    reception = _new_reception(dept_harness)
    conv_a, task_a, run_a = _seed_running_task_in_conversation(
        dept_harness,
        reception,
        timestamp=t0,
        assigned_agent_ids=["it.worker"],
    )
    conv_b = reception.open_conversation(
        ConversationCreateRequest(title="Alice Second Conv", timestamp=t0 + timedelta(seconds=4)),
        auth_context=dept_harness.alice,
    ).conversation_id

    # 16a. Worker denied from user intake, conversation creation, conversation reads, and Master actions.
    assigned_worker = worker_ctx("it.worker", assigned_task_ids=[task_a])
    other_worker = worker_ctx("qa.other", assigned_task_ids=[])

    with pytest.raises(DepartmentGovernanceError):
        reception.open_conversation(
            ConversationCreateRequest(title="Worker hijack", timestamp=t0 + timedelta(seconds=5)),
            auth_context=assigned_worker,
        )
    with pytest.raises(DepartmentGovernanceError):
        reception.receive_user_request(
            UserRequestIntake(
                intent_mode="NEW_TASK",
                summary="Worker forging user intake",
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=assigned_worker,
        )
    with pytest.raises(DepartmentGovernanceError):
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_a,
                intent_mode="REFINEMENT",
                target_task_id=task_a,
                summary="Worker trying to inject refinement",
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=assigned_worker,
        )
    with pytest.raises(DepartmentGovernanceError):
        reception.list_conversation_turns(conv_a, auth_context=assigned_worker)
    with pytest.raises(DepartmentGovernanceError) as unassigned_err:
        reception.list_task_refinements(task_a, auth_context=other_worker)
    assert unassigned_err.value.error_code == "TASK_OWNERSHIP_DENIED"
    with pytest.raises(DepartmentGovernanceError) as sig_unassigned:
        reception.triage_incoming_signal(
            IncomingSignalRequest(
                source_channel="runtime",
                summary="Foreign worker probing Task A",
                task_id=task_a,
                timestamp=t0 + timedelta(seconds=5),
            ),
            auth_context=other_worker,
        )
    assert sig_unassigned.value.error_code == "TASK_OWNERSHIP_DENIED"

    # 16b. Spoofed actor claim and cross-operator access denied.
    with pytest.raises(DepartmentGovernanceError) as spoof_err:
        reception.receive_user_request(
            UserRequestIntake(
                intent_mode="NEW_TASK",
                summary="Trying to claim Master identity",
                claimed_actor_id="core.master",
                timestamp=t0 + timedelta(seconds=6),
            ),
            auth_context=dept_harness.alice,
        )
    assert spoof_err.value.error_code == "ACTOR_IDENTITY_MISMATCH"

    with pytest.raises(DepartmentGovernanceError) as bob_conv_err:
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_a,
                intent_mode="NEW_TASK",
                summary="Bob intruding on Alice conversation",
                timestamp=t0 + timedelta(seconds=7),
            ),
            auth_context=dept_harness.bob,
        )
    assert bob_conv_err.value.error_code == "CONVERSATION_OWNERSHIP_DENIED"

    # Cross-conversation task refinement blocked fail-closed!
    with pytest.raises(ConversationIsolationError) as cross_conv_err:
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_b,
                intent_mode="REFINEMENT",
                target_task_id=task_a,
                summary="Trying to refine Task A from Conversation B",
                timestamp=t0 + timedelta(seconds=8),
            ),
            auth_context=dept_harness.alice,
        )
    assert cross_conv_err.value.error_code == "CROSS_CONVERSATION_TASK_CONFLICT"

    # 17. Invalid conversation/task/run references rejected cleanly.
    with pytest.raises(ValidationError):
        UserRequestIntake(
            conversation_id="invalid_id_format",
            summary="Invalid conv syntax",
            timestamp=t0 + timedelta(seconds=9),
        )
    with pytest.raises(ReceptionReferenceError) as missing_conv:
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=new_conversation_id(),
                summary="Missing conversation id reference",
                timestamp=t0 + timedelta(seconds=9),
            ),
            auth_context=dept_harness.alice,
        )
    assert missing_conv.value.error_code == "CONVERSATION_NOT_FOUND"

    with pytest.raises(ReceptionReferenceError) as missing_task:
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_a,
                intent_mode="REFINEMENT",
                target_task_id=new_task_id(),
                summary="Refining non-existent task",
                timestamp=t0 + timedelta(seconds=9),
            ),
            auth_context=dept_harness.alice,
        )
    assert missing_task.value.error_code == "TARGET_TASK_NOT_FOUND"

    with pytest.raises(ReceptionReferenceError) as wrong_run:
        reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_a,
                intent_mode="REFINEMENT",
                target_task_id=task_a,
                target_run_id=new_run_id(),
                summary="Refining with invalid run_id",
                timestamp=t0 + timedelta(seconds=9),
            ),
            auth_context=dept_harness.alice,
        )
    assert wrong_run.value.error_code == "TARGET_RUN_MISMATCH_OR_NOT_FOUND"

    # Worker denied from Master acknowledgment too; blocked/invalid attempts left no phantom intakes.
    pending_intakes = reception.list_master_pending_intakes()
    assert [rec.created_task_id for rec in pending_intakes] == [task_a]
    out_q = reception.receive_user_request(
        UserRequestIntake(
            conversation_id=conv_b,
            intent_mode="NEW_TASK",
            title="Check webhook timeout telemetry",
            summary=(
                "Debug integration failure with Bearer super_secret_token_987654 and "
                "sk-1234567890abcdef1234567890abcdef and api_key=raw_secret_value_abc"
            ),
            timestamp=t0 + timedelta(seconds=10),
        ),
        auth_context=dept_harness.alice,
    )
    with pytest.raises(DepartmentGovernanceError):
        reception.acknowledge_intake_by_master(
            MasterIntakeAckRequest(
                intake_id=out_q.intake_id,
                expected_record_version=1,
                decision_note="Worker attempting Master ack",
                timestamp=t0 + timedelta(seconds=11),
            ),
            auth_context=assigned_worker,
        )

    # 18. Secret rejection on structured metadata + secret redaction in free-text & DB/Events!
    with pytest.raises(ValidationError, match="sensitive|secret|forbidden"):
        UserRequestIntake(
            intent_mode="NEW_TASK",
            summary="Metadata smuggling secret",
            metadata={"api_key": "sk-1234567890abcdef1234567890abcdef"},
            timestamp=t0 + timedelta(seconds=12),
        )

    assert out_q.created_task is not None
    serialized_all = "".join(
        [
            out_q.model_dump_json(),
            out_q.created_task.model_dump_json(),
            *[e.event.model_dump_json() for e in dept_harness.store.events.read_entries(task_id=out_q.created_task.task_id)],
        ]
    )
    for leaked in (
        "super_secret_token_987654",
        "1234567890abcdef1234567890abcdef",
        "raw_secret_value_abc",
    ):
        assert leaked not in serialized_all
    assert "[REDACTED" in out_q.created_task.description
    _ = run_a


# =============================================================================
# 7. Gate G End-to-End Integration Scenario (Offline Verified)
# =============================================================================


def test_p11_01_gate_g_concurrent_task_handoff_to_master_without_overwriting_active_task(
    tmp_path: Path,
) -> None:
    """Gate G offline integration: Task A runs -> User sends B -> Reception hands off to Master -> A stays intact."""
    t0 = utc(15, 0, 0)
    h = make_department_harness(tmp_path)
    try:
        register_worker_agent(
            h,
            agent_id="it.python_dev",
            department="it",
            skills=["workspace_artifact_authoring", "automated_testing"],
            tools_allowed=["tools.files.read", "tools.files.write"],
            timestamp=t0,
        )
        reception = _new_reception(h)
        planner = MasterPlanningService(
            plan_repo=h.store.plans,
            agent_registry=h.agent_registry,
            permission_manager=h.permissions,
            workspace_root=h.tmp_path,
            registered_tools=sorted(m.id for m in h.tool_registry.list_manifests()),
        )

        # 1. Task A is actively RUNNING with Worker assigned in Conversation A.
        conv_a, task_a, run_a = _seed_running_task_in_conversation(
            h,
            reception,
            title="Active Financial ETL",
            description="Process Q3 financial ledger inside workspace.",
            timestamp=t0 + timedelta(seconds=1),
            assigned_agent_ids=["it.python_dev"],
        )
        stored_a_initial = h.store.tasks.get_stored_task(task_a)
        assert stored_a_initial is not None and stored_a_initial.task.status == "RUNNING"

        # 2. User opens Conversation B and sends concurrent Task B while A is still RUNNING.
        conv_b = reception.open_conversation(
            ConversationCreateRequest(title="Urgent Security Review", timestamp=t0 + timedelta(seconds=10)),
            auth_context=h.alice,
        ).conversation_id
        intake_b = reception.receive_user_request(
            UserRequestIntake(
                conversation_id=conv_b,
                title="Audit export permissions",
                summary="Produce an export permission audit report under workspace/audit.md",
                priority="HIGH",
                acceptance_criteria=["Audit report lists all workspace export permissions"],
                idempotency_key="gate-g-req-b",
                timestamp=t0 + timedelta(seconds=11),
            ),
            auth_context=h.alice,
        )
        assert intake_b.classification == "QUEUED"
        assert intake_b.created_task is not None
        task_b = intake_b.created_task.task_id

        # 3. Master reads queued intakes from Reception and inspects/acknowledges B.
        pending = reception.list_master_pending_intakes()
        assert any(item.intake_id == intake_b.intake_id for item in pending)
        acked_b = reception.acknowledge_intake_by_master(
            MasterIntakeAckRequest(
                intake_id=intake_b.intake_id,
                expected_record_version=1,
                decision_note="Master queued Task B after priority triage without interrupting Task A",
                timestamp=t0 + timedelta(seconds=15),
            ),
            auth_context=h.master,
        )
        assert acked_b.master_handoff_status == "ACKNOWLEDGED_BY_MASTER"
        assert acked_b.processing_status == "MASTER_ACKNOWLEDGED"

        # 4. Master creates a plan for Task B while Task A continues RUNNING unchanged!
        plan_b = planner.create_plan(
            brief=StructuredUserBrief(
                task_id=task_b,
                title="Audit export permissions",
                objective="Produce verified export permission audit report",
                requested_by="operator.alice",
                required_outputs=[f"workspace/{task_b}/audit.md"],
                acceptance_criteria=["Audit report lists all workspace export permissions"],
            ),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=16),
        )
        assert plan_b.outcome_status == "PLAN_READY"

        # 5. Task A retains exact RUNNING state, Run A, and initial content; B has independent state.
        stored_a_final = h.store.tasks.get_stored_task(task_a)
        stored_b_final = h.store.tasks.get_stored_task(task_b)
        assert stored_a_final is not None and stored_b_final is not None
        assert stored_a_final.task == stored_a_initial.task
        assert stored_a_final.version == stored_a_initial.version
        assert stored_a_final.task.active_run_id == run_a
        assert stored_b_final.task.status == "QUEUED"
        assert stored_b_final.task.active_run_id is None
        assert conv_a != conv_b

        db_path = h.db_path
        h.store.close()

        # 6. Restart check: both requests, conversations, and Task snapshots remain independently retrievable.
        reopened = SQLiteRepositoryStore(db_path)
        try:
            rx_reopened = ReceptionDepartmentService(
                permission_manager=PermissionManager(
                    evaluator=PolicyEvaluator(),
                    journal=reopened.events,
                ),
                journal=reopened.events,
                notification_store=reopened.metadata,
                intake_repository=reopened.reception,
            )
            fetched_a_turn = rx_reopened.list_conversation_turns(conv_a, auth_context=h.alice)
            fetched_b_turn = rx_reopened.list_conversation_turns(conv_b, auth_context=h.alice)
            assert len(fetched_a_turn) == 1 and fetched_a_turn[0].task_id == task_a
            assert len(fetched_b_turn) == 1 and fetched_b_turn[0].task_id == task_b
            assert rx_reopened.get_intake_request(intake_b.intake_id, auth_context=h.master) is not None
            assert reopened.tasks.get_task(task_a).status == "RUNNING"  # type: ignore[union-attr]
            assert reopened.tasks.get_task(task_b).status == "QUEUED"  # type: ignore[union-attr]
        finally:
            reopened.close()
    finally:
        h.store.close()
