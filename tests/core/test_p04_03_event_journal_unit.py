"""Granular offline unit tests for Phase 04 / P04.03 Event Journal, Replay, and Timeline."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from core.contracts import (
    Event,
    Run,
    RunErrorRef,
    Task,
    new_artifact_id,
    new_event_id,
    new_evidence_id,
    new_run_id,
    new_session_id,
    new_task_id,
    sanitize_event_payload,
)
from core.events import (
    ActivityTimelineProjection,
    AppendResult,
    EventJournal,
    InMemoryEventJournal,
    JournalEntry,
    RunReplayResult,
    TaskReplayResult,
    project_activity_timeline,
    record_paired_transition,
    record_run_transition,
    record_runtime_event,
    record_task_transition,
    replay_run_history,
    replay_task_history,
)
from core.runtime import AgentRuntimeEvent
from core.tasks import (
    ExecutionEvidence,
    QaVerificationEvidence,
    RunTransitionRequest,
    TaskTransitionRequest,
    TransitionContext,
    complete_run_only,
    start_task_run,
    transition_run,
    transition_task,
)
from engines.contract import EngineEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_GOOGLE_KEY = "AIza" + "FakeJournalSecretKey1234567890"
FAKE_OPENAI_KEY = "sk-" + "FakeJournalOpenAIKey1234567890"
SAMPLE_SHA256 = "c" * 64


@pytest.fixture
def t0() -> datetime:
    """Return a deterministic timezone-aware fixture timestamp."""
    return datetime(2026, 11, 24, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def journal() -> InMemoryEventJournal:
    """Return a fresh empty InMemoryEventJournal instance."""
    return InMemoryEventJournal()


@pytest.fixture
def sample_task(t0: datetime) -> Task:
    """Return a valid baseline Task in QUEUED state."""
    return Task(
        task_id=new_task_id(),
        title="Verify Event Journal flow",
        description="Exercise append-only logging and deterministic replay.",
        requested_by="operator.local",
        status="QUEUED",
        created_at=t0,
    )


@pytest.fixture
def sample_run(sample_task: Task) -> Run:
    """Return a valid PENDING Run linked to ``sample_task``."""
    return Run(
        run_id=new_run_id(),
        task_id=sample_task.task_id,
        runtime_id="custom.master_worker_v1",
        invocation_id="inv-p0403-001",
        correlation_id="corr-p0403-001",
        provider_id="antigravity",
        connection_id="default_antigravity",
        status="PENDING",
    )


def _ctx(
    ts: datetime,
    *,
    reason: str = "Unit test transition",
    actor_id: str = "core.master",
    actor_kind: str = "orchestrator",
    run: Run | None = None,
    execution_evidence: ExecutionEvidence | None = None,
    qa_evidence: QaVerificationEvidence | None = None,
    assigned_agent_ids: list[str] | None = None,
    correlation_id: str | None = None,
) -> TransitionContext:
    """Build deterministic ``TransitionContext`` fixture."""
    return TransitionContext(
        actor_id=actor_id,
        actor_kind=actor_kind,  # type: ignore[arg-type]
        timestamp=ts,
        reason=reason,
        assigned_agent_ids=assigned_agent_ids,
        run=run,
        execution_evidence=execution_evidence,
        qa_evidence=qa_evidence,
        correlation_id=correlation_id,
    )


def _make_event(
    ts: datetime,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    event_type: str = "task.created",
    source: str = "core_runtime",
    actor_id: str | None = "core.master",
    event_id: str | None = None,
    payload: dict | None = None,
) -> Event:
    """Construct a valid canonical Event fixture."""
    return Event(
        event_id=event_id or new_event_id(),
        timestamp=ts,
        event_type=event_type,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        redacted_payload=payload or {},
    )


# ---------------------------------------------------------------------------
# 1–10. Append, Monotonic Ordering, Duplicates, Batches, and Immutability
# ---------------------------------------------------------------------------


def test_append_valid_event_and_monotonic_sequence_independent_of_timestamp(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Verify valid appends assign monotonic sequence numbers even with identical timestamps."""
    assert isinstance(journal, EventJournal)
    task_id = new_task_id()

    # Deliberately record later timestamp first or identical timestamps; sequence defines append order.
    ev1 = _make_event(t0, task_id=task_id, payload={"step": 1})
    ev2 = _make_event(t0, task_id=task_id, payload={"step": 2})

    res1 = journal.append(ev1)
    res2 = journal.append(ev2)

    assert res1.appended and res1.status == "APPENDED" and res1.entry is not None
    assert res2.appended and res2.status == "APPENDED" and res2.entry is not None
    assert res1.entry.sequence_number == 1
    assert res2.entry.sequence_number == 2

    entries = journal.read_entries(task_id=task_id)
    assert [entry.sequence_number for entry in entries] == [1, 2]
    assert [entry.event.redacted_payload["step"] for entry in entries] == [1, 2]


def test_append_duplicate_idempotent_vs_conflicting_content(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Verify identical duplicates return non-appended idempotency and conflicts are rejected."""
    task_id = new_task_id()
    fixed_id = new_event_id()
    ev_orig = _make_event(t0, task_id=task_id, event_id=fixed_id, payload={"note": "original"})
    ev_same = ev_orig.model_copy(deep=True)
    ev_diff = _make_event(
        t0,
        task_id=task_id,
        event_id=fixed_id,
        payload={"note": "tampered"},
    )

    first = journal.append(ev_orig)
    second = journal.append(ev_same)
    conflict = journal.append(ev_diff)

    assert first.appended is True and first.status == "APPENDED"
    assert second.appended is False
    assert second.is_duplicate is True
    assert second.status == "IDEMPOTENT_DUPLICATE"
    assert second.entry is not None and second.entry.sequence_number == 1

    assert conflict.appended is False
    assert conflict.is_duplicate is True
    assert conflict.status == "REJECTED"
    assert conflict.entry is None
    assert conflict.error is not None
    assert conflict.error.error_code == "DUPLICATE_EVENT_CONFLICT"
    assert journal.count() == 1


def test_append_rejects_invalid_schema_missing_anchor_naive_ts_and_secrets(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Verify journal refuses corrupted Event instances constructed via model_construct."""
    bad_anchor = Event.model_construct(
        event_id=new_event_id(),
        timestamp=t0,
        event_type="task.created",
        source="core_runtime",
        redacted_payload={},
    )
    bad_ts = Event.model_construct(
        event_id=new_event_id(),
        timestamp=datetime(2026, 11, 24, 15, 0, 0),
        event_type="task.created",
        source="core_runtime",
        task_id=new_task_id(),
        redacted_payload={},
    )
    bad_secret = Event.model_construct(
        event_id=new_event_id(),
        timestamp=t0,
        event_type="runtime.error",
        source="core_runtime",
        task_id=new_task_id(),
        redacted_payload={"api_key": FAKE_GOOGLE_KEY},
    )

    r_anchor = journal.append(bad_anchor)
    r_ts = journal.append(bad_ts)
    r_secret = journal.append(bad_secret)
    r_type = journal.append({"not": "an event"})  # type: ignore[arg-type]

    assert not r_anchor.appended and r_anchor.error and r_anchor.error.error_code == "MISSING_ATTRIBUTION_ANCHOR"
    assert not r_ts.appended and r_ts.error and r_ts.error.error_code == "INVALID_TIMESTAMP"
    assert not r_secret.appended and r_secret.error and r_secret.error.error_code == "UNSAFE_PAYLOAD"
    assert FAKE_GOOGLE_KEY not in r_secret.error.message
    assert not r_type.appended and r_type.error and r_type.error.error_code == "INVALID_EVENT"
    assert journal.count() == 0


def test_batch_append_success_and_atomic_rollback_preserves_sequence(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Verify valid batches commit together while any failure leaves zero partial writes or sequence skips."""
    task_id = new_task_id()
    ev1 = _make_event(t0, task_id=task_id, payload={"seq": "one"})
    ev2 = _make_event(t0 + timedelta(seconds=1), task_id=task_id, payload={"seq": "two"})
    ok_batch = journal.append_batch([ev1, ev2])

    assert ok_batch.appended and ok_batch.status == "APPENDED"
    assert [e.sequence_number for e in ok_batch.entries] == [1, 2]
    assert ok_batch.batch_id is not None

    # Failing batch: second event has secret payload -> zero entries appended and next sequence stays 3
    ev3_good = _make_event(t0 + timedelta(seconds=2), task_id=task_id)
    ev4_bad = Event.model_construct(
        event_id=new_event_id(),
        timestamp=t0 + timedelta(seconds=3),
        event_type="runtime.error",
        source="core_runtime",
        task_id=task_id,
        redacted_payload={"secret": FAKE_OPENAI_KEY},
    )
    bad_batch = journal.append_batch([ev3_good, ev4_bad])
    assert not bad_batch.appended and bad_batch.status == "REJECTED"
    assert bad_batch.error is not None and bad_batch.error.batch_index == 1
    assert journal.count() == 2

    # Failing within-batch duplicate also leaves sequence unchanged
    ev_dup = _make_event(t0 + timedelta(seconds=2), task_id=task_id)
    dup_batch = journal.append_batch([ev_dup, ev_dup])
    assert not dup_batch.appended and dup_batch.error and dup_batch.error.error_code == "DUPLICATE_EVENT_CONFLICT"

    # Next single append strictly receives sequence_number=3
    ev_next = _make_event(t0 + timedelta(seconds=4), task_id=task_id)
    res_next = journal.append(ev_next)
    assert res_next.appended and res_next.entry is not None
    assert res_next.entry.sequence_number == 3


def test_journal_is_append_only_and_defensively_copies_snapshots(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Verify journal exposes no delete/edit APIs and prevents mutating stored payloads via caller dicts."""
    for forbidden_method in ("delete", "remove", "clear", "pop", "update_event", "edit"):
        assert not hasattr(journal, forbidden_method)

    payload = {"items": ["a", "b"]}
    ev = _make_event(t0, task_id=new_task_id(), payload=payload)
    journal.append(ev)

    # Caller mutates original event payload dict in memory after append
    ev.redacted_payload["items"].append("mutated_after_append")

    snapshot = journal.read_entries()
    assert len(snapshot) == 1
    assert snapshot[0].event.redacted_payload["items"] == ["a", "b"]

    # Caller mutates snapshot returned by read_entries()
    snapshot[0].event.redacted_payload["items"].append("mutated_on_read")
    second_snapshot = journal.read_entries()
    assert second_snapshot[0].event.redacted_payload["items"] == ["a", "b"]


def test_concurrent_appends_preserve_unique_monotonic_sequences(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Verify thread safety of concurrent in-memory append calls."""
    task_id = new_task_id()

    def _worker(idx: int) -> AppendResult:
        ev = _make_event(t0 + timedelta(milliseconds=idx), task_id=task_id, payload={"idx": idx})
        return journal.append(ev)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_worker, range(40)))

    assert all(res.appended and res.entry is not None for res in results)
    entries = journal.read_entries()
    assert [e.sequence_number for e in entries] == list(range(1, 41))
    assert len({e.event.event_id for e in entries}) == 40


# ---------------------------------------------------------------------------
# 11–14. Query Filtering & Sequence-Based Cursor Pagination
# ---------------------------------------------------------------------------


def test_query_filters_and_sequence_cursor_pagination(
    journal: InMemoryEventJournal,
    t0: datetime,
) -> None:
    """Filter strictly by Task, Run, Session, Correlation ID and paginate using sequence cursor."""
    task_a, task_b = new_task_id(), new_task_id()
    run_a, run_b = new_run_id(), new_run_id()
    sess_a = new_session_id()

    events = [
        _make_event(t0, task_id=task_a, run_id=run_a, session_id=sess_a, correlation_id="corr-A"),
        _make_event(t0 + timedelta(seconds=1), task_id=task_b, run_id=run_b, correlation_id="corr-B"),
        _make_event(t0 + timedelta(seconds=2), task_id=task_a, run_id=run_a, correlation_id="corr-A"),
        _make_event(t0 + timedelta(seconds=3), task_id=task_a, run_id=run_b, correlation_id="corr-A"),
    ]
    journal.append_batch(events)

    assert len(journal.read_entries(task_id=task_a)) == 3
    assert len(journal.read_entries(run_id=run_b)) == 2
    assert len(journal.read_entries(session_id=sess_a)) == 1
    assert len(journal.read_entries(correlation_id="corr-B")) == 1

    page1 = journal.read_entries(task_id=task_a, limit=2)
    assert [e.sequence_number for e in page1] == [1, 3]
    page2 = journal.read_entries(task_id=task_a, after_sequence=page1[-1].sequence_number, limit=2)
    assert [e.sequence_number for e in page2] == [4]

    with pytest.raises(ValueError):
        journal.read_entries(after_sequence=-1)
    with pytest.raises(ValueError):
        journal.read_entries(limit=0)


# ---------------------------------------------------------------------------
# 15–21. Task/Run State Machine Integration & run.status_changed Coverage
# ---------------------------------------------------------------------------


def test_state_machine_integration_covers_all_run_events_and_rejects_failed_transitions(
    journal: InMemoryEventJournal,
    sample_task: Task,
    sample_run: Run,
    t0: datetime,
) -> None:
    """Record Task/Run transitions including WAITING_INPUT, WAITING_APPROVAL, BLOCKED, resume."""
    # Task QUEUED -> ASSIGNED
    res_assign = transition_task(
        TaskTransitionRequest(
            task=sample_task,
            target_status="ASSIGNED",
            context=_ctx(t0 + timedelta(seconds=1), assigned_agent_ids=["it.artifact_worker"]),
        )
    )
    assert record_task_transition(journal, res_assign).appended

    # Failed guard transition must not emit or record any event
    bad_complete = transition_task(
        TaskTransitionRequest(
            task=res_assign.task,  # type: ignore[arg-type]
            target_status="COMPLETED",
            context=_ctx(t0 + timedelta(seconds=2)),
        )
    )
    assert not bad_complete.accepted
    rec_bad = record_task_transition(journal, bad_complete)
    assert not rec_bad.appended and rec_bad.error and rec_bad.error.error_code == "MISSING_TRANSITION_EVENT"
    assert journal.count() == 1

    # Paired start: Run PENDING->RUNNING (run.started) & Task ASSIGNED->RUNNING (task.status_changed)
    paired_start = start_task_run(
        res_assign.task,  # type: ignore[arg-type]
        sample_run,
        _ctx(
            t0 + timedelta(seconds=2),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Start"),
        ),
    )
    batch_start = record_paired_transition(journal, paired_start)
    assert batch_start.appended and len(batch_start.entries) == 2

    assert paired_start.run is not None and paired_start.task is not None
    r_cur = paired_start.run

    # Intermediate Run states: WAITING_INPUT -> RUNNING -> WAITING_APPROVAL -> RUNNING -> BLOCKED -> RUNNING -> SUCCEEDED
    steps = [
        ("WAITING_INPUT", t0 + timedelta(seconds=3), None),
        ("RUNNING", t0 + timedelta(seconds=4), ExecutionEvidence(source="unit_test_fixture", reason="Input received")),
        ("WAITING_APPROVAL", t0 + timedelta(seconds=5), None),
        ("RUNNING", t0 + timedelta(seconds=6), ExecutionEvidence(source="unit_test_fixture", reason="Approved")),
        ("BLOCKED", t0 + timedelta(seconds=7), None),
        ("RUNNING", t0 + timedelta(seconds=8), ExecutionEvidence(source="unit_test_fixture", reason="Unblocked")),
    ]
    for target, ts, ev_info in steps:
        res_r = transition_run(
            RunTransitionRequest(
                run=r_cur,
                target_status=target,
                context=_ctx(ts, actor_kind="core_runtime", execution_evidence=ev_info),
            )
        )
        assert res_r.accepted and res_r.run is not None and res_r.event is not None
        assert res_r.event.event_type == "run.status_changed"
        assert res_r.event.redacted_payload["previous_status"] == r_cur.status
        assert res_r.event.redacted_payload["new_status"] == target
        assert record_run_transition(journal, res_r).appended
        r_cur = res_r.run

    # Finish Run via complete_run_only -> run.completed (Task stays RUNNING!)
    paired_done = complete_run_only(
        paired_start.task,
        r_cur,
        _ctx(
            t0 + timedelta(seconds=9),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Finished"),
        ),
    )
    assert paired_done.accepted and paired_done.task is not None and paired_done.run is not None
    assert record_paired_transition(journal, paired_done).appended

    # Complete Task with independent QA evidence -> task.status_changed
    qa = QaVerificationEvidence(
        evidence_id=new_evidence_id(),
        verifier_source="unit_test_fixture",
        verifier_actor_id="qa.verifier",
        verification_status="VERIFIED_PASS",
        verified_at=t0 + timedelta(seconds=10),
        expected_sha256=SAMPLE_SHA256,
        actual_sha256=SAMPLE_SHA256,
    )
    res_complete = transition_task(
        TaskTransitionRequest(
            task=paired_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=11),
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                run=paired_done.run,
                qa_evidence=qa,
            ),
        )
    )
    assert res_complete.accepted and record_task_transition(journal, res_complete).appended

    entries = journal.read_entries(task_id=sample_task.task_id)
    assert [e.event.event_type for e in entries] == [
        "task.status_changed",
        "run.started",
        "task.status_changed",
        "run.status_changed",
        "run.status_changed",
        "run.status_changed",
        "run.status_changed",
        "run.status_changed",
        "run.status_changed",
        "run.completed",
        "task.status_changed",
    ]


# ---------------------------------------------------------------------------
# 22–28. Deterministic Replay & Invalid History Detection
# ---------------------------------------------------------------------------


def test_deterministic_replay_task_and_run_with_baseline(
    journal: InMemoryEventJournal,
    sample_task: Task,
    sample_run: Run,
    t0: datetime,
) -> None:
    """Replay full valid Task and Run history identically with baseline records."""
    res_assign = transition_task(
        TaskTransitionRequest(
            task=sample_task,
            target_status="ASSIGNED",
            context=_ctx(t0 + timedelta(seconds=1), assigned_agent_ids=["it.worker"]),
        )
    )
    record_task_transition(journal, res_assign)

    started = start_task_run(
        res_assign.task,  # type: ignore[arg-type]
        sample_run,
        _ctx(
            t0 + timedelta(seconds=2),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Launch"),
        ),
    )
    record_paired_transition(journal, started)

    r_wait = transition_run(
        RunTransitionRequest(
            run=started.run,  # type: ignore[arg-type]
            target_status="WAITING_APPROVAL",
            context=_ctx(t0 + timedelta(seconds=3), actor_kind="core_runtime", reason="Approval gate"),
        )
    )
    record_run_transition(journal, r_wait)

    r_resumed = transition_run(
        RunTransitionRequest(
            run=r_wait.run,  # type: ignore[arg-type]
            target_status="RUNNING",
            context=_ctx(
                t0 + timedelta(seconds=5),
                actor_kind="operator",
                execution_evidence=ExecutionEvidence(source="operator", reason="Approved"),
            ),
        )
    )
    record_run_transition(journal, r_resumed)

    r_done = complete_run_only(
        started.task,  # type: ignore[arg-type]
        r_resumed.run,  # type: ignore[arg-type]
        _ctx(
            t0 + timedelta(seconds=7),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Exit 0"),
        ),
    )
    record_paired_transition(journal, r_done)

    # Replaying Task right after run.completed still keeps Task RUNNING (Run SUCCEEDED != Task COMPLETED!)
    mid_task_replay = replay_task_history(
        journal.read_entries(task_id=sample_task.task_id),
        task_id=sample_task.task_id,
        baseline_task=sample_task,
    )
    assert mid_task_replay.status == "VALID"
    assert mid_task_replay.replayed_status == "RUNNING"
    assert mid_task_replay.run_statuses[sample_run.run_id] == "SUCCEEDED"
    assert mid_task_replay.qa_records == []

    # Now replay Run history cleanly
    run_replay = replay_run_history(
        journal.read_entries(run_id=sample_run.run_id),
        run_id=sample_run.run_id,
        baseline_run=sample_run,
    )
    assert run_replay.status == "VALID"
    assert run_replay.can_reconstruct_full_run is True
    assert run_replay.replayed_status == "SUCCEEDED"
    assert run_replay.reconstructed_run is not None
    assert run_replay.reconstructed_run.status == "SUCCEEDED"

    # Second deterministic replay produces exact equality without mutating journal
    count_before = journal.count()
    run_replay_2 = replay_run_history(
        journal.read_entries(run_id=sample_run.run_id),
        run_id=sample_run.run_id,
        baseline_run=sample_run,
    )
    assert run_replay == run_replay_2
    assert journal.count() == count_before


def test_replay_missing_baseline_and_inconsistency_failures(
    sample_task: Task,
    sample_run: Run,
    t0: datetime,
) -> None:
    """Detect missing baseline, sequence gap, wrong order, missing step, terminal mutation, and fake QA."""
    ev1 = _make_event(
        t0 + timedelta(seconds=1),
        task_id=sample_task.task_id,
        event_type="task.status_changed",
        payload={"previous_status": "QUEUED", "new_status": "ASSIGNED"},
    )
    entry1 = JournalEntry(sequence_number=1, event=ev1)

    # 25. Missing baseline returns INCOMPLETE_HISTORY with can_reconstruct_full_task=False
    no_base = replay_task_history([entry1], task_id=sample_task.task_id, baseline_task=None)
    assert no_base.status == "INCOMPLETE_HISTORY"
    assert no_base.can_reconstruct_full_task is False
    assert no_base.reconstructed_task is None
    assert any(issue.error_code == "MISSING_BASELINE" for issue in no_base.issues)

    # 24a. Sequence gap detected when require_contiguous_sequences=True
    ev2 = _make_event(
        t0 + timedelta(seconds=2),
        task_id=sample_task.task_id,
        run_id=sample_run.run_id,
        event_type="task.status_changed",
        payload={"previous_status": "ASSIGNED", "new_status": "RUNNING"},
    )
    gap_res = replay_task_history(
        [entry1, JournalEntry(sequence_number=3, event=ev2)],
        task_id=sample_task.task_id,
        baseline_task=sample_task,
        require_contiguous_sequences=True,
    )
    assert gap_res.status == "INVALID_HISTORY"
    assert any(i.error_code == "SEQUENCE_GAP" for i in gap_res.issues)

    # 24b. Non-monotonic sequence / backwards timestamp
    out_of_order = replay_task_history(
        [JournalEntry(sequence_number=2, event=ev2), entry1],
        task_id=sample_task.task_id,
        baseline_task=sample_task,
    )
    assert out_of_order.status == "INVALID_HISTORY"
    assert any(i.error_code == "NON_MONOTONIC_SEQUENCE" for i in out_of_order.issues)

    backwards_ts_ev = _make_event(
        t0,
        task_id=sample_task.task_id,
        run_id=sample_run.run_id,
        event_type="task.status_changed",
        payload={"previous_status": "ASSIGNED", "new_status": "RUNNING"},
    )
    ts_bad = replay_task_history(
        [entry1, JournalEntry(sequence_number=2, event=backwards_ts_ev)],
        task_id=sample_task.task_id,
        baseline_task=sample_task,
    )
    assert ts_bad.status == "INVALID_HISTORY"
    assert any(i.error_code == "TIMESTAMP_ORDER_ANOMALY" for i in ts_bad.issues)

    # 26. Missing intermediate Event (QUEUED -> RUNNING / previous_status mismatch)
    skipped_ev = _make_event(
        t0 + timedelta(seconds=2),
        task_id=sample_task.task_id,
        event_type="task.status_changed",
        payload={"previous_status": "ASSIGNED", "new_status": "RUNNING"},
    )
    missing_step = replay_task_history(
        [JournalEntry(sequence_number=1, event=skipped_ev)],
        task_id=sample_task.task_id,
        baseline_task=sample_task,
    )
    assert missing_step.status == "INVALID_HISTORY"
    assert any(i.error_code == "PREVIOUS_STATUS_MISMATCH" for i in missing_step.issues)

    # 27. Terminal Task mutation rejected
    done_base = sample_task.model_copy(update={"status": "COMPLETED", "updated_at": t0 + timedelta(seconds=5)})
    mutate_ev = _make_event(
        t0 + timedelta(seconds=6),
        task_id=done_base.task_id,
        event_type="task.status_changed",
        payload={"previous_status": "COMPLETED", "new_status": "RUNNING"},
    )
    term_res = replay_task_history(
        [JournalEntry(sequence_number=1, event=mutate_ev)],
        task_id=done_base.task_id,
        baseline_task=done_base,
    )
    assert term_res.status == "INVALID_HISTORY"
    assert any(i.error_code == "TERMINAL_STATE_MUTATION" for i in term_res.issues)

    # 29. Self-claimed QA PASS without evidence or from untrusted source rejected
    fake_qa_ev = Event(
        timestamp=t0 + timedelta(seconds=3),
        event_type="qa.verification_recorded",
        source="agent_runtime",
        task_id=sample_task.task_id,
        redacted_payload={"verification_status": "VERIFIED_PASS"},
    )
    fake_qa_replay = replay_task_history(
        [JournalEntry(sequence_number=1, event=fake_qa_ev)],
        task_id=sample_task.task_id,
        baseline_task=sample_task,
    )
    assert fake_qa_replay.status == "INVALID_HISTORY"
    codes = {i.error_code for i in fake_qa_replay.issues}
    assert {"UNTRUSTED_SOURCE_OR_ACTOR", "UNVERIFIED_QA_PASS"}.issubset(codes)


# ---------------------------------------------------------------------------
# 28–34. Activity Timeline, Tool Attribution, AgentRuntime & Session Isolation
# ---------------------------------------------------------------------------


def test_timeline_projection_tool_attribution_and_agent_runtime_bridge(
    journal: InMemoryEventJournal,
    sample_task: Task,
    sample_run: Run,
    t0: datetime,
) -> None:
    """Verify Activity Timeline reads from Journal, maps AgentRuntimeEvents, isolates sess-* & tools."""
    # Bridge a legacy P03.03 Worker AgentRuntimeEvent containing observed write_file tool
    eng_ev = EngineEvent(
        event_id="eng-1",
        sequence_number=1,
        timestamp=t0 + timedelta(seconds=2),
        event_type="tool_execution_observed",
        source="provider",
        provider_id="antigravity",
        connection_id="default_antigravity",
        status="IN_PROGRESS",
        tool_name="write_file",
        redacted_payload={"path": "proof.txt"},
    )
    rt_ev = AgentRuntimeEvent(
        invocation_id="inv-p0303",
        runtime_id="custom.master_worker_v1",
        sequence=1,
        stage="worker_execute",
        actor_id="it.artifact_worker",
        actor_role="worker",
        logical_session_id="sess-worker-abc123",
        provider_id="antigravity",
        provider_conversation_id="conv-provider-1",
        engine_event=eng_ev,
    )
    app_res = record_runtime_event(
        journal,
        rt_ev,
        task_id=sample_task.task_id,
        run_id=sample_run.run_id,
        correlation_id="corr-runtime-01",
    )
    assert app_res.appended

    # Missing timestamp fails closed when bridging AgentRuntimeEvent
    bad_rt = rt_ev.model_copy(update={"engine_event": None})
    bad_bridge = record_runtime_event(journal, bad_rt, task_id=sample_task.task_id)
    assert not bad_bridge.appended and bad_bridge.error and bad_bridge.error.error_code == "INVALID_TIMESTAMP"

    # Proposed tool call (not observed execution) and provider text claim never count as observed tools
    proposed_ev = _make_event(
        t0 + timedelta(seconds=3),
        task_id=sample_task.task_id,
        run_id=sample_run.run_id,
        event_type="tool.call_proposed",
        source="provider",
        payload={"tool_name": "proposed_only_tool"},
    )
    text_claim_ev = _make_event(
        t0 + timedelta(seconds=4),
        task_id=sample_task.task_id,
        run_id=sample_run.run_id,
        event_type="engine.event_forwarded",
        source="provider",
        payload={"text_delta": "I ran write_file and created everything."},
    )
    art_id = new_artifact_id()
    evd_id = new_evidence_id()
    qa_ev = _make_event(
        t0 + timedelta(seconds=5),
        task_id=sample_task.task_id,
        run_id=sample_run.run_id,
        event_type="qa.verification_recorded",
        source="qa_verifier",
        actor_id="qa.verifier",
        payload={"verification_status": "VERIFIED_PASS", "sha256": SAMPLE_SHA256},
    ).model_copy(update={"artifact_id": art_id, "evidence_id": evd_id})
    journal.append_batch([proposed_ev, text_claim_ev, qa_ev])

    timeline = project_activity_timeline(journal, task_id=sample_task.task_id)
    assert isinstance(timeline, ActivityTimelineProjection)
    assert timeline.total_returned == 4
    assert timeline.last_sequence == 4
    assert timeline.entries[0].tool_name == "write_file"
    assert timeline.entries[0].session_id is None
    assert timeline.entries[0].legacy_session_ref == "sess-worker-abc123"
    assert timeline.entries[1].tool_name is None
    assert timeline.entries[2].tool_name is None
    assert timeline.entries[3].evidence_id == evd_id

    # Replay confirms only observed tool_name appears
    replay_res = replay_task_history(
        journal.read_entries(task_id=sample_task.task_id),
        task_id=sample_task.task_id,
        baseline_task=sample_task,
    )
    assert replay_res.observed_tool_names == ["write_file"]
    assert len(replay_res.qa_records) == 1 and replay_res.qa_records[0].verification_status == "VERIFIED_PASS"

    # JSON round-trip of JournalEntry, Replay results, and ActivityTimelineProjection
    assert timeline == ActivityTimelineProjection.model_validate_json(timeline.model_dump_json())
    assert replay_res == TaskReplayResult.model_validate_json(replay_res.model_dump_json())
    single_entry = journal.read_entries(limit=1)[0]
    assert single_entry == JournalEntry.model_validate_json(single_entry.model_dump_json())
    run_rep = replay_run_history([], run_id=sample_run.run_id, baseline_run=sample_run)
    assert run_rep == RunReplayResult.model_validate_json(run_rep.model_dump_json())

    # sanitize_event_payload remains intact
    cleaned = sanitize_event_payload({"stdout": f"leak {FAKE_GOOGLE_KEY}", "token": "bad"})
    assert FAKE_GOOGLE_KEY not in str(cleaned)
    assert cleaned["token"] == "[REDACTED_KEY_VALUE]"


# ---------------------------------------------------------------------------
# 35–40. Architecture Boundaries: Zero SQLite/CrewAI/UI & Package Isolation
# ---------------------------------------------------------------------------


def test_core_events_boundaries_no_sqlite_crewai_or_circular_imports() -> None:
    """Verify clean import of core.events and absence of forbidden persistence/framework deps."""
    proc = subprocess.run(
        [sys.executable, "-c", "import core.events; print('OK')"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"

    forbidden_prefixes = (
        "sqlite3",
        "sqlalchemy",
        "crewai",
        "storage",
        "api",
        "ui",
        "httpx",
        "urllib",
        "subprocess",
    )
    for py_path in sorted((REPO_ROOT / "core" / "events").glob("*.py")):
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"Forbidden import {alias.name!r} in {py_path.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"Forbidden import {node.module!r} in {py_path.name}"
                )
