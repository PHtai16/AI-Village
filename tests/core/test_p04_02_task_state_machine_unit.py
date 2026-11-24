"""Pure offline Python unit tests covering Phase 04 / P04.02 Task & Run State Machines."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from core.contracts import (
    Artifact,
    ChecklistItem,
    Run,
    RunErrorRef,
    RunTelemetryRef,
    Task,
    new_artifact_id,
    new_checklist_item_id,
    new_evidence_id,
    new_run_id,
    new_session_id,
    new_task_id,
)
from core.runtime import (
    AgentRuntimeResult,
    AgentRuntimeTelemetry,
)
from core.tasks import (
    KANBAN_COLUMN_LABELS,
    ExecutionEvidence,
    KanbanBoardProjection,
    PairedTransitionResult,
    QaVerificationEvidence,
    RunTransitionRequest,
    RunTransitionResult,
    TaskTimingProjection,
    TaskTransitionRequest,
    TaskTransitionResult,
    TransitionContext,
    cancel_task_run,
    complete_run_only,
    fail_task_run,
    project_checklist,
    project_kanban_board,
    project_kanban_column,
    project_task_timing,
    qa_evidence_from_engine_verification,
    qa_evidence_from_runtime_result,
    start_task_run,
    timeout_task_run,
    transition_run,
    transition_task,
)
from engines.verification import EngineArtifactVerificationResult

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SHA256 = "b" * 64
FAKE_GOOGLE_KEY = "AIza" + "FakeTestKeyMaterial1234567890A"


@pytest.fixture
def t0() -> datetime:
    """Deterministic unit-test fixture start clock (explicitly not live)."""
    return datetime(2026, 11, 24, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def queued_task(t0: datetime) -> Task:
    """Return a clean Task in QUEUED state."""
    return Task(
        task_id=new_task_id(),
        title="Build summary report",
        description="Analyze input docs and produce verified output.",
        requested_by="operator.local",
        status="QUEUED",
        created_at=t0,
    )


@pytest.fixture
def pending_run(queued_task: Task) -> Run:
    """Return a PENDING Run linked to ``queued_task``."""
    return Run(
        run_id=new_run_id(),
        task_id=queued_task.task_id,
        runtime_id="custom.master_worker_v1",
        invocation_id="inv-unit-001",
        correlation_id="corr-unit-001",
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
    checklist_items: list[ChecklistItem] | None = None,
    artifacts: list[Artifact] | None = None,
    expected_current_status: str | None = None,
    payload: dict | None = None,
) -> TransitionContext:
    """Helper building unit-test ``TransitionContext`` instances cleanly."""
    return TransitionContext(
        actor_id=actor_id,
        actor_kind=actor_kind,  # type: ignore[arg-type]
        timestamp=ts,
        reason=reason,
        assigned_agent_ids=assigned_agent_ids,
        run=run,
        execution_evidence=execution_evidence,
        qa_evidence=qa_evidence,
        checklist_items=checklist_items,
        artifacts=artifacts,
        expected_current_status=expected_current_status,
        payload=payload or {},
    )


def _qa_pass(ts: datetime, artifact_ids: list[str] | None = None) -> QaVerificationEvidence:
    """Build deterministic unit-test independent QA PASS evidence."""
    return QaVerificationEvidence(
        evidence_id=new_evidence_id(),
        verifier_source="unit_test_fixture",
        verifier_actor_id="qa.verifier",
        verification_status="VERIFIED_PASS",
        verified_at=ts,
        artifact_ids=artifact_ids or [],
        expected_sha256=SAMPLE_SHA256,
        actual_sha256=SAMPLE_SHA256,
        summary="Independent SHA-256 verified in offline test fixture.",
    )


def test_task_lifecycle_happy_path_wait_block_resume_and_completed(
    queued_task: Task,
    pending_run: Run,
    t0: datetime,
) -> None:
    """Cover Task QUEUED->ASSIGNED->RUNNING->WAITING_INPUT/APPROVAL->BLOCKED->COMPLETED."""
    # 1. QUEUED -> ASSIGNED
    res1 = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(
                t0 + timedelta(seconds=1),
                reason="Master assigned worker",
                assigned_agent_ids=["it.artifact_worker"],
            ),
        )
    )
    assert res1.accepted and res1.task is not None and res1.event is not None
    assert res1.task.status == "ASSIGNED"
    assert res1.task.assigned_agent_ids == ["it.artifact_worker"]
    assert res1.event.event_type == "task.status_changed"
    assert res1.event.source == "orchestrator"
    assert res1.event.redacted_payload["previous_status"] == "QUEUED"
    assert res1.event.redacted_payload["new_status"] == "ASSIGNED"
    assert queued_task.status == "QUEUED"

    # 2. ASSIGNED -> RUNNING paired with PENDING -> RUNNING
    paired_start = start_task_run(
        res1.task,
        pending_run,
        _ctx(
            t0 + timedelta(seconds=2),
            actor_kind="core_runtime",
            reason="Worker execution started",
            execution_evidence=ExecutionEvidence(
                source="unit_test_fixture",
                run_id=pending_run.run_id,
                reason="Started CLI subprocess",
            ),
        ),
    )
    assert paired_start.accepted and paired_start.task and paired_start.run
    assert paired_start.task.status == "RUNNING"
    assert paired_start.task.active_run_id == paired_start.run.run_id
    assert paired_start.run.status == "RUNNING"
    assert [ev.event_type for ev in paired_start.events] == ["run.started", "task.status_changed"]

    t_running = paired_start.task
    r_running = paired_start.run

    # 3. RUNNING -> WAITING_INPUT -> RUNNING
    res_wi = transition_task(
        TaskTransitionRequest(
            task=t_running,
            target_status="WAITING_INPUT",
            context=_ctx(t0 + timedelta(seconds=5), run=r_running, reason="Clarification requested"),
        )
    )
    assert res_wi.accepted and res_wi.task is not None
    res_resume_wi = transition_task(
        TaskTransitionRequest(
            task=res_wi.task,
            target_status="RUNNING",
            context=_ctx(t0 + timedelta(seconds=10), run=r_running, reason="Clarification supplied"),
        )
    )
    assert res_resume_wi.accepted and res_resume_wi.task is not None

    # 4. RUNNING -> WAITING_APPROVAL -> RUNNING
    res_wa = transition_task(
        TaskTransitionRequest(
            task=res_resume_wi.task,
            target_status="WAITING_APPROVAL",
            context=_ctx(t0 + timedelta(seconds=12), run=r_running, reason="Approval required"),
        )
    )
    assert res_wa.accepted and res_wa.task is not None
    res_resume_wa = transition_task(
        TaskTransitionRequest(
            task=res_wa.task,
            target_status="RUNNING",
            context=_ctx(
                t0 + timedelta(seconds=20),
                actor_kind="operator",
                run=r_running,
                reason="Operator approved",
            ),
        )
    )
    assert res_resume_wa.accepted and res_resume_wa.task is not None

    # 5. RUNNING -> BLOCKED -> RUNNING (requires execution_evidence to resume)
    res_blk = transition_task(
        TaskTransitionRequest(
            task=res_resume_wa.task,
            target_status="BLOCKED",
            context=_ctx(t0 + timedelta(seconds=22), run=r_running, reason="Missing input file"),
        )
    )
    assert res_blk.accepted and res_blk.task is not None
    blocked_no_ev = transition_task(
        TaskTransitionRequest(
            task=res_blk.task,
            target_status="RUNNING",
            context=_ctx(t0 + timedelta(seconds=24), run=r_running, reason="Resume without proof"),
        )
    )
    assert not blocked_no_ev.accepted
    assert blocked_no_ev.error is not None
    assert blocked_no_ev.error.error_code == "MISSING_EXECUTION_EVIDENCE"

    res_unblk = transition_task(
        TaskTransitionRequest(
            task=res_blk.task,
            target_status="RUNNING",
            context=_ctx(
                t0 + timedelta(seconds=26),
                actor_kind="core_runtime",
                run=r_running,
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=r_running.run_id,
                    reason="Missing input file supplied",
                ),
                reason="Blocker resolved",
            ),
        )
    )
    assert res_unblk.accepted and res_unblk.task is not None

    # 6. Finish Run first (SUCCEEDED), Task remains RUNNING until QA completes
    run_done = complete_run_only(
        res_unblk.task,
        r_running,
        _ctx(
            t0 + timedelta(seconds=30),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(
                source="unit_test_fixture",
                run_id=r_running.run_id,
                reason="Run process finished cleanly",
                telemetry=RunTelemetryRef(
                    total_duration_ms=28000,
                    provider_duration_ms=27000,
                    engine_call_count=3,
                    input_tokens=1000,
                    output_tokens=200,
                    total_tokens=1200,
                    telemetry_status="VERIFIED_MEASURED",
                    telemetry_source="provider_cli",
                ),
            ),
            reason="Worker finished artifact write",
        ),
    )
    assert run_done.accepted and run_done.run is not None and run_done.task is not None
    assert run_done.run.status == "SUCCEEDED"
    assert run_done.task.status == "RUNNING"
    assert run_done.task.active_run_id is None

    art_id = new_artifact_id()
    evd_id = new_evidence_id()
    chk_done = ChecklistItem(
        checklist_item_id=new_checklist_item_id(),
        task_id=run_done.task.task_id,
        title="Verify output SHA-256",
        order=1,
        status="COMPLETED",
        evidence_reference=evd_id,
        created_at=t0 + timedelta(seconds=1),
        completed_at=t0 + timedelta(seconds=31),
    )
    art_verified = Artifact(
        artifact_id=art_id,
        task_id=run_done.task.task_id,
        run_id=run_done.run.run_id,
        owner_agent_id="it.artifact_worker",
        relative_path="out/proof.txt",
        sha256=SAMPLE_SHA256,
        verification_status="VERIFIED_PASS",
        evidence_reference=evd_id,
    )
    qa = _qa_pass(t0 + timedelta(seconds=32), artifact_ids=[art_id])

    completed = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=33),
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                run=run_done.run,
                qa_evidence=qa,
                checklist_items=[chk_done],
                artifacts=[art_verified],
                reason="Independent QA verified deliverable",
            ),
        )
    )
    assert completed.accepted and completed.task is not None and completed.event is not None
    assert completed.task.status == "COMPLETED"
    assert completed.task.active_run_id is None
    assert art_id in completed.task.artifact_ids
    assert completed.event.source == "qa_verifier"
    assert completed.event.artifact_id == art_id
    assert completed.event.evidence_id == qa.evidence_id


def test_qa_guard_rejects_missing_evidence_self_claims_forbidden_jumps_and_terminal_mutation(
    queued_task: Task,
    pending_run: Run,
    t0: datetime,
) -> None:
    """Reject missing QA evidence, AI self-claims, unverified artifacts/checklist, forbidden jumps."""
    res_assign = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(t0 + timedelta(seconds=1), assigned_agent_ids=["it.worker"]),
        )
    )
    assert res_assign.task is not None
    started = start_task_run(
        res_assign.task,
        pending_run,
        _ctx(
            t0 + timedelta(seconds=2),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Start"),
        ),
    )
    assert started.task is not None and started.run is not None
    run_done = complete_run_only(
        started.task,
        started.run,
        _ctx(
            t0 + timedelta(seconds=5),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Done"),
        ),
    )
    assert run_done.task is not None and run_done.run is not None

    # 7. Missing QA evidence
    no_qa = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(t0 + timedelta(seconds=6), run=run_done.run),
        )
    )
    assert not no_qa.accepted and no_qa.error is not None
    assert no_qa.error.error_code == "MISSING_QA_EVIDENCE"

    # 8. AI/Worker self-claim rejected even with evidence
    qa = _qa_pass(t0 + timedelta(seconds=6))
    for untrusted_kind, expected_code in (
        ("llm_self_report", "UNTRUSTED_TRANSITION_SOURCE"),
        ("worker_agent", "UNVERIFIED_COMPLETION"),
    ):
        self_claim = transition_task(
            TaskTransitionRequest(
                task=run_done.task,
                target_status="COMPLETED",
                context=_ctx(
                    t0 + timedelta(seconds=6),
                    actor_id="it.worker",
                    actor_kind=untrusted_kind,
                    run=run_done.run,
                    qa_evidence=qa,
                ),
            )
        )
        assert not self_claim.accepted and self_claim.error is not None
        assert self_claim.error.error_code == expected_code

    # Producer trying to verify own artifact fails closed
    art_self = Artifact(
        artifact_id=new_artifact_id(),
        task_id=run_done.task.task_id,
        run_id=run_done.run.run_id,
        owner_agent_id="qa.verifier",
        relative_path="out/a.txt",
        sha256=SAMPLE_SHA256,
        verification_status="VERIFIED_PASS",
        evidence_reference=new_evidence_id(),
    )
    self_verified = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=6),
                actor_kind="qa_verifier",
                actor_id="qa.verifier",
                run=run_done.run,
                qa_evidence=qa,
                artifacts=[art_self],
            ),
        )
    )
    assert not self_verified.accepted and self_verified.error is not None
    assert self_verified.error.error_code == "UNVERIFIED_COMPLETION"

    # Checklist incomplete blocks COMPLETED
    incomplete_chk = ChecklistItem(
        task_id=run_done.task.task_id,
        title="Unfinished step",
        order=0,
        status="IN_PROGRESS",
    )
    incomplete_res = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=6),
                actor_kind="qa_verifier",
                run=run_done.run,
                qa_evidence=qa,
                checklist_items=[incomplete_chk],
            ),
        )
    )
    assert not incomplete_res.accepted and incomplete_res.error is not None
    assert incomplete_res.error.error_code == "UNVERIFIED_COMPLETION"

    # 9. Forbidden jumps (QUEUED -> COMPLETED, ASSIGNED -> COMPLETED, WAITING_INPUT -> COMPLETED)
    jump_res = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="COMPLETED",
            context=_ctx(t0 + timedelta(seconds=6), qa_evidence=qa),
        )
    )
    assert not jump_res.accepted and jump_res.error is not None
    assert jump_res.error.error_code == "INVALID_TRANSITION"

    # VERIFIED_FAIL QA and unverified Artifact both fail closed
    qa_fail = QaVerificationEvidence(
        evidence_id=new_evidence_id(),
        verifier_source="unit_test_fixture",
        verifier_actor_id="qa.verifier",
        verification_status="VERIFIED_FAIL",
        verified_at=t0 + timedelta(seconds=6),
    )
    fail_qa_res = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=6),
                actor_kind="qa_verifier",
                actor_id="qa.verifier",
                run=run_done.run,
                qa_evidence=qa_fail,
            ),
        )
    )
    assert not fail_qa_res.accepted and fail_qa_res.error is not None
    assert fail_qa_res.error.error_code == "UNVERIFIED_COMPLETION"

    art_unverified = Artifact(
        artifact_id=new_artifact_id(),
        task_id=run_done.task.task_id,
        run_id=run_done.run.run_id,
        owner_agent_id="it.worker",
        relative_path="out/unverified.txt",
        verification_status="UNVERIFIED",
    )
    unver_art_res = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=6),
                actor_kind="qa_verifier",
                actor_id="qa.verifier",
                run=run_done.run,
                qa_evidence=qa,
                artifacts=[art_unverified],
            ),
        )
    )
    assert not unver_art_res.accepted and unver_art_res.error is not None
    assert unver_art_res.error.error_code == "UNVERIFIED_COMPLETION"

    # Wrong active_run_id on Task blocks transition
    other_run = run_done.run.model_copy(update={"run_id": new_run_id(), "status": "SUCCEEDED"})
    task_with_active = run_done.task.model_copy(update={"active_run_id": started.run.run_id})
    wrong_active_res = transition_task(
        TaskTransitionRequest(
            task=task_with_active,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=6),
                actor_kind="qa_verifier",
                actor_id="qa.verifier",
                run=other_run,
                qa_evidence=qa,
            ),
        )
    )
    assert not wrong_active_res.accepted and wrong_active_res.error is not None
    assert wrong_active_res.error.error_code == "WRONG_TASK_RUN_RELATIONSHIP"

    # 10. Terminal COMPLETED cannot transition back to RUNNING
    done_ok = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=7),
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                run=run_done.run,
                qa_evidence=qa,
            ),
        )
    )
    assert done_ok.accepted and done_ok.task is not None
    reopen = transition_task(
        TaskTransitionRequest(
            task=done_ok.task,
            target_status="RUNNING",
            context=_ctx(t0 + timedelta(seconds=8), run=run_done.run),
        )
    )
    assert not reopen.accepted and reopen.error is not None
    assert reopen.error.error_code == "TERMINAL_STATE_MUTATION"


def test_run_lifecycle_and_independent_outcomes(
    queued_task: Task,
    pending_run: Run,
    t0: datetime,
) -> None:
    """Verify Run transitions SUCCEEDED/FAILED/CANCELLED/TIMED_OUT and wait event gap honesty."""
    # PENDING -> RUNNING -> run.started
    r_start = transition_run(
        RunTransitionRequest(
            run=pending_run,
            target_status="RUNNING",
            context=_ctx(
                t0 + timedelta(seconds=1),
                actor_kind="core_runtime",
                execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Launch"),
            ),
        )
    )
    assert r_start.accepted and r_start.run is not None and r_start.event is not None
    assert r_start.event.event_type == "run.started"

    # RUNNING -> WAITING_INPUT omits event honestly instead of faking run.completed/run.failed
    r_wait = transition_run(
        RunTransitionRequest(
            run=r_start.run,
            target_status="WAITING_INPUT",
            context=_ctx(t0 + timedelta(seconds=2), reason="Need input"),
        )
    )
    assert r_wait.accepted and r_wait.run is not None
    assert r_wait.event is None
    assert r_wait.event_omitted_reason is not None and "P04.03" in r_wait.event_omitted_reason

    # WAITING_INPUT -> RUNNING -> SUCCEEDED -> run.completed
    r_resume = transition_run(
        RunTransitionRequest(
            run=r_wait.run,
            target_status="RUNNING",
            context=_ctx(
                t0 + timedelta(seconds=3),
                execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Resumed"),
            ),
        )
    )
    assert r_resume.accepted and r_resume.run is not None
    r_succ = transition_run(
        RunTransitionRequest(
            run=r_resume.run,
            target_status="SUCCEEDED",
            context=_ctx(
                t0 + timedelta(seconds=4),
                execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Exit 0"),
            ),
        )
    )
    assert r_succ.accepted and r_succ.run is not None and r_succ.event is not None
    assert r_succ.event.event_type == "run.completed"
    # Run SUCCEEDED never auto-completes Task
    assert queued_task.status == "QUEUED"

    # FAILED via fail_task_run clears active_run_id and records error_reference
    t_assigned = queued_task.model_copy(
        update={"status": "ASSIGNED", "assigned_agent_ids": ["it.worker"]}
    )
    run2 = Run(run_id=new_run_id(), task_id=t_assigned.task_id, runtime_id="custom.master_worker_v1")
    pair2 = start_task_run(
        t_assigned,
        run2,
        _ctx(
            t0 + timedelta(seconds=5),
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Attempt 2"),
        ),
    )
    assert pair2.accepted and pair2.task and pair2.run
    failed_pair = fail_task_run(
        pair2.task,
        pair2.run,
        _ctx(
            t0 + timedelta(seconds=6),
            execution_evidence=ExecutionEvidence(
                source="engine_adapter",
                reason="Process exited non-zero",
                error_reference=RunErrorRef(
                    error_code="WORKER_EXECUTION_FAILED",
                    stage="worker_execute",
                    message="Crash",
                    retryable=True,
                ),
            ),
        ),
        target_task_status="BLOCKED",
    )
    assert failed_pair.accepted and failed_pair.task and failed_pair.run
    assert failed_pair.run.status == "FAILED"
    assert failed_pair.task.status == "BLOCKED"
    assert failed_pair.task.active_run_id is None
    assert [ev.event_type for ev in failed_pair.events] == ["run.failed", "task.status_changed"]

    # TIMED_OUT via timeout_task_run
    run3 = Run(run_id=new_run_id(), task_id=t_assigned.task_id, runtime_id="custom.master_worker_v1")
    pair3 = start_task_run(
        failed_pair.task,
        run3,
        _ctx(
            t0 + timedelta(seconds=7),
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Retry 3"),
        ),
    )
    assert pair3.accepted and pair3.task and pair3.run
    timed_out_pair = timeout_task_run(
        pair3.task,
        pair3.run,
        _ctx(
            t0 + timedelta(seconds=8),
            execution_evidence=ExecutionEvidence(source="process_manager", reason="Deadline exceeded"),
        ),
        target_task_status="FAILED",
    )
    assert timed_out_pair.accepted and timed_out_pair.task and timed_out_pair.run
    assert timed_out_pair.run.status == "TIMED_OUT"
    assert timed_out_pair.task.status == "FAILED"
    assert timed_out_pair.task.active_run_id is None
    assert [ev.event_type for ev in timed_out_pair.events] == ["run.timed_out", "task.status_changed"]

    # CANCELLED rejects unconfirmed cancel button press and succeeds when confirmed
    t_assigned2 = queued_task.model_copy(
        update={"task_id": new_task_id(), "status": "ASSIGNED", "assigned_agent_ids": ["it.worker"]}
    )
    run4 = Run(run_id=new_run_id(), task_id=t_assigned2.task_id, runtime_id="custom.master_worker_v1")
    pair4 = start_task_run(
        t_assigned2,
        run4,
        _ctx(
            t0 + timedelta(seconds=1),
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Launch"),
        ),
    )
    assert pair4.accepted and pair4.task and pair4.run
    unconfirmed_cancel = cancel_task_run(
        pair4.task,
        pair4.run,
        _ctx(
            t0 + timedelta(seconds=2),
            actor_kind="operator",
            execution_evidence=ExecutionEvidence(
                source="operator",
                reason="Clicked cancel button",
                confirmed_cancelled=False,
            ),
        ),
    )
    assert not unconfirmed_cancel.accepted and unconfirmed_cancel.error is not None
    assert unconfirmed_cancel.error.error_code == "CANCELLATION_NOT_CONFIRMED"
    # Unchanged on rejected cancel
    assert pair4.task.status == "RUNNING" and pair4.run.status == "RUNNING"

    confirmed_cancel = cancel_task_run(
        pair4.task,
        pair4.run,
        _ctx(
            t0 + timedelta(seconds=3),
            actor_kind="core_runtime",
            execution_evidence=ExecutionEvidence(
                source="process_manager",
                reason="Win32JobGuard closed subprocess tree",
                confirmed_cancelled=True,
                cancellation_source="process_manager",
            ),
        ),
    )
    assert confirmed_cancel.accepted and confirmed_cancel.task and confirmed_cancel.run
    assert confirmed_cancel.run.status == "CANCELLED"
    assert confirmed_cancel.task.status == "CANCELLED"
    assert confirmed_cancel.task.active_run_id is None
    assert [ev.event_type for ev in confirmed_cancel.events] == ["run.cancelled", "task.status_changed"]


def test_task_run_link_immutability_timestamps_errors_and_secret_safety(
    queued_task: Task,
    pending_run: Run,
    t0: datetime,
) -> None:
    """Cover Task/Run mismatch, stale/duplicate/unknown errors, naive/backwards timestamps, and secrets."""
    other_task = queued_task.model_copy(update={"task_id": new_task_id()})
    mismatch = start_task_run(
        other_task,
        pending_run,
        _ctx(
            t0 + timedelta(seconds=1),
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Wrong link"),
        ),
    )
    assert not mismatch.accepted and mismatch.error is not None
    assert mismatch.error.error_code == "WRONG_TASK_RUN_RELATIONSHIP"
    assert other_task.status == "QUEUED" and pending_run.status == "PENDING"

    # Naive timestamp rejected
    naive = datetime(2026, 11, 24, 14, 5, 0)
    naive_res = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(naive, assigned_agent_ids=["it.worker"]),
        )
    )
    assert not naive_res.accepted and naive_res.error is not None
    assert naive_res.error.error_code == "INVALID_TIMESTAMP"

    # Backwards timestamp rejected
    backwards_res = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(t0 - timedelta(seconds=5), assigned_agent_ids=["it.worker"]),
        )
    )
    assert not backwards_res.accepted and backwards_res.error is not None
    assert backwards_res.error.error_code == "INVALID_TIMESTAMP_ORDER"

    # Unsafe secret payload rejected fail-closed without crashing or mutating Task
    unsafe_res = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(
                t0 + timedelta(seconds=1),
                assigned_agent_ids=["it.worker"],
                payload={"debug": {"api_key": FAKE_GOOGLE_KEY}},
            ),
        )
    )
    assert not unsafe_res.accepted and unsafe_res.error is not None
    assert unsafe_res.error.error_code == "UNSAFE_EVENT_PAYLOAD"
    assert FAKE_GOOGLE_KEY not in unsafe_res.error.message
    assert queued_task.status == "QUEUED"

    # Duplicate, stale expected_current_status, and unknown status
    dup_res = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="QUEUED",
            context=_ctx(t0 + timedelta(seconds=1)),
        )
    )
    assert not dup_res.accepted and dup_res.error is not None
    assert dup_res.error.error_code == "DUPLICATE_TRANSITION"

    stale_res = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(
                t0 + timedelta(seconds=1),
                expected_current_status="RUNNING",
                assigned_agent_ids=["it.worker"],
            ),
        )
    )
    assert not stale_res.accepted and stale_res.error is not None
    assert stale_res.error.error_code == "STALE_EXPECTED_STATUS"

    unknown_task = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="SUCCEEDED",
            context=_ctx(t0 + timedelta(seconds=1)),
        )
    )
    assert not unknown_task.accepted and unknown_task.error is not None
    assert unknown_task.error.error_code == "UNKNOWN_STATUS"

    unknown_run = transition_run(
        RunTransitionRequest(
            run=pending_run,
            target_status="COMPLETED",
            context=_ctx(t0 + timedelta(seconds=1)),
        )
    )
    assert not unknown_run.accepted and unknown_run.error is not None
    assert unknown_run.error.error_code == "UNKNOWN_STATUS"


def test_projections_checklist_kanban_timing_and_json_roundtrip(
    queued_task: Task,
    pending_run: Run,
    t0: datetime,
) -> None:
    """Verify Checklist evidence counts, Kanban UX mapping, real Timing with no ETA, and JSON round-trip."""
    evd_id = new_evidence_id()
    items = [
        ChecklistItem(
            task_id=queued_task.task_id,
            title="Step 1 verified",
            order=0,
            status="COMPLETED",
            evidence_reference=evd_id,
            created_at=t0,
            completed_at=t0 + timedelta(seconds=10),
        ),
        ChecklistItem(
            task_id=queued_task.task_id,
            title="Step 2 active",
            order=1,
            status="IN_PROGRESS",
        ),
    ]
    chk_proj = project_checklist(queued_task.task_id, items)
    assert chk_proj.total_items == 2
    assert chk_proj.verified_completed_items == 1
    assert chk_proj.ratio_display == "1/2"
    assert chk_proj.all_required_items_verified is False
    assert chk_proj.implies_task_completion is False
    assert not hasattr(chk_proj, "progress_percent")

    # Before start: timing elapsed_ms stays None, zero ETA
    timing_queued = project_task_timing(queued_task)
    assert timing_queued.first_started_at is None
    assert timing_queued.elapsed_ms is None
    assert timing_queued.total_completed_duration_ms is None
    assert timing_queued.eta_available is False
    assert timing_queued.estimated_completion_at is None
    assert timing_queued == TaskTimingProjection.model_validate_json(timing_queued.model_dump_json())

    # Simulate RUNNING -> WAITING_APPROVAL -> RUNNING -> COMPLETED timing
    r_assign = transition_task(
        TaskTransitionRequest(
            task=queued_task,
            target_status="ASSIGNED",
            context=_ctx(t0 + timedelta(seconds=1), assigned_agent_ids=["it.worker"]),
        )
    )
    assert r_assign.task and r_assign.event
    started = start_task_run(
        r_assign.task,
        pending_run,
        _ctx(
            t0 + timedelta(seconds=2),
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Start"),
        ),
    )
    assert started.task and started.run
    wa = transition_task(
        TaskTransitionRequest(
            task=started.task,
            target_status="WAITING_APPROVAL",
            context=_ctx(t0 + timedelta(seconds=5), run=started.run, reason="Approval"),
        )
    )
    assert wa.task and wa.event
    resume = transition_task(
        TaskTransitionRequest(
            task=wa.task,
            target_status="RUNNING",
            context=_ctx(t0 + timedelta(seconds=15), actor_kind="operator", run=started.run, reason="Approved"),
        )
    )
    assert resume.task and resume.event
    run_done = complete_run_only(
        resume.task,
        started.run,
        _ctx(
            t0 + timedelta(seconds=20),
            execution_evidence=ExecutionEvidence(source="unit_test_fixture", reason="Finished"),
        ),
    )
    assert run_done.task and run_done.run
    done = transition_task(
        TaskTransitionRequest(
            task=run_done.task,
            target_status="COMPLETED",
            context=_ctx(
                t0 + timedelta(seconds=25),
                actor_id="qa.verifier",
                actor_kind="qa_verifier",
                run=run_done.run,
                qa_evidence=_qa_pass(t0 + timedelta(seconds=24)),
            ),
        )
    )
    assert done.task and done.event

    all_events = [
        r_assign.event,
        *started.events,
        wa.event,
        resume.event,
        *run_done.events,
        done.event,
    ]
    timing_done = project_task_timing(
        done.task,
        runs=[run_done.run],
        events=all_events,
    )
    assert timing_done.first_started_at == t0 + timedelta(seconds=2)
    assert timing_done.last_finished_at == t0 + timedelta(seconds=25)
    assert timing_done.waiting_approval_ms == 10000
    assert timing_done.total_completed_duration_ms == 23000
    assert timing_done.elapsed_ms == 23000
    assert timing_done.eta_available is False
    assert timing_done.estimated_completion_at is None

    # Wait phase active observation computes elapsed from observed_at without ETA
    timing_wait = project_task_timing(
        wa.task,
        runs=[started.run],
        events=[*started.events, wa.event],
        observed_at=t0 + timedelta(seconds=10),
    )
    assert timing_wait.elapsed_ms == 8000
    assert timing_wait.waiting_approval_ms == 5000
    assert timing_wait.total_completed_duration_ms is None
    assert timing_wait.eta_available is False

    # Kanban projection keeps exact 6 Mission Control columns without conflating statuses
    t_failed = queued_task.model_copy(update={"task_id": new_task_id(), "status": "FAILED"})
    t_cancel = queued_task.model_copy(update={"task_id": new_task_id(), "status": "CANCELLED"})
    board = project_kanban_board([queued_task, wa.task, t_failed, t_cancel, done.task])
    assert [col.label for col in board.columns] == list(KANBAN_COLUMN_LABELS.values())
    assert project_kanban_column("CANCELLED") == "failed"
    assert board.counts_by_status == {"QUEUED": 1, "WAITING_APPROVAL": 1, "FAILED": 1, "CANCELLED": 1, "COMPLETED": 1}
    assert board == KanbanBoardProjection.model_validate_json(board.model_dump_json())
    assert done == TaskTransitionResult.model_validate_json(done.model_dump_json())
    assert run_done == PairedTransitionResult.model_validate_json(run_done.model_dump_json())


def test_architecture_boundaries_and_phase03_compatibility(t0: datetime) -> None:
    """Verify zero SQLite/CrewAI/UI deps in core/tasks/ and Phase 03 AgentRuntime bridge."""
    proc = subprocess.run(
        [sys.executable, "-c", "import core.tasks; print('OK')"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"

    forbidden_prefixes = (
        "crewai",
        "sqlite3",
        "sqlalchemy",
        "storage",
        "api",
        "ui",
        "httpx",
        "urllib",
        "subprocess",
    )
    for py_path in sorted((REPO_ROOT / "core" / "tasks").glob("*.py")):
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes)

    # Phase 03 EngineArtifactVerificationResult & AgentRuntimeResult compatibility
    ver = EngineArtifactVerificationResult(
        status="VERIFIED_PASS",
        engine_execution_status="SUCCEEDED",
        artifact_relpath="proof.txt",
        artifact_abs_path=None,
        engine_succeeded=True,
        artifact_exists=True,
        within_workspace=True,
        content_verified=True,
        hash_verified=True,
        source_integrity_verified=True,
        business_verified=True,
        file_size_bytes=12,
        expected_sha256=SAMPLE_SHA256,
        actual_sha256=SAMPLE_SHA256,
    )
    art_id = new_artifact_id()
    ev_from_ver = qa_evidence_from_engine_verification(
        ver,
        evidence_id=new_evidence_id(),
        verifier_actor_id="qa.verifier",
        verified_at=t0,
        artifact_ids=[art_id],
    )
    assert ev_from_ver.verification_status == "VERIFIED_PASS"

    # Simulated AgentRuntimeResult carrying mismatched business_verified=False fails closed
    bad_rt_res = AgentRuntimeResult(
        runtime_id="custom.master_worker_v1",
        invocation_id="inv-sim",
        correlation_id="corr-sim",
        status="REJECTED_BY_QA",
        technical_execution_succeeded=True,
        business_verified=False,
        qa_verification=ver.model_copy(update={"status": "VERIFIED_PASS"}),
        telemetry=AgentRuntimeTelemetry(
            total_duration_ms=10,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        ),
        started_at=t0,
        finished_at=t0 + timedelta(milliseconds=10),
        duration_ms=10,
    )
    with pytest.raises(ValueError):
        qa_evidence_from_runtime_result(
            bad_rt_res,
            evidence_id=new_evidence_id(),
            verifier_actor_id="qa.verifier",
            verified_at=t0 + timedelta(milliseconds=15),
        )

    good_rt_res = bad_rt_res.model_copy(
        update={
            "status": "DELIVERED",
            "business_verified": True,
            "qa_verification": ver,
        }
    )
    qa_rt = qa_evidence_from_runtime_result(
        good_rt_res,
        evidence_id=new_evidence_id(),
        verifier_actor_id="qa.verifier",
        verified_at=t0 + timedelta(milliseconds=15),
        artifact_ids=[art_id],
    )
    assert qa_rt.verification_status == "VERIFIED_PASS"
