"""Comprehensive P10.02 tests: cross-task pattern detection, suggested improvements, persistence, and security."""

from __future__ import annotations

from datetime import timedelta
import sqlite3
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts import Event, new_event_id
from core.departments import (
    CompileTaskRetrospectiveRequest,
    PatternAnalysisRequest,
    ProcessDepartmentService,
    ProcessOwnershipError,
)
from core.governance import ApprovalService, PermissionManager, PolicyEvaluator
from core.tools.models import ToolExecutionRecord
from storage.repositories import SQLiteRepositoryStore
from storage.repositories.errors import RepositoryError
from tests.departments.conftest import (
    DepartmentTestHarness,
    create_task_and_run,
    utc,
    worker_ctx,
)


def _new_process_service(harness: DepartmentTestHarness) -> ProcessDepartmentService:
    return ProcessDepartmentService(
        permission_manager=harness.permissions,
        journal=harness.store.events,
        retrospective_repo=harness.store.process,
    )


def _seed_task_with_tool_sequence(
    h: DepartmentTestHarness,
    *,
    title: str,
    executions: list[ToolExecutionRecord],
    pattern_tag: str | None = None,
    skill_invoked: str | None = None,
    offset_s: int = 0,
) -> tuple[str, str]:
    t_now = utc(9, 0, 0) + timedelta(seconds=offset_s)
    task_id, run_id = create_task_and_run(
        h,
        title=title,
        description=f"Scenario seed for {title}",
        timestamp=t_now,
    )
    for idx, rec in enumerate(executions, start=1):
        h.store.tools.record_execution(
            rec.model_copy(
                update={
                    "task_id": task_id,
                    "run_id": run_id,
                    "executed_at": t_now + timedelta(seconds=idx),
                }
            )
        )
    if skill_invoked is not None:
        h.store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t_now + timedelta(seconds=len(executions) + 1),
                event_type="skill.usage_observed",
                source="agent_runtime",
                task_id=task_id,
                run_id=run_id,
                actor_id="it.coder",
                redacted_payload={
                    "skill_id": skill_invoked,
                    "outcome": "SUCCEEDED",
                    "participated": True,
                },
            )
        )
    proc = _new_process_service(h)
    proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(
            task_id=task_id,
            pattern_tag=pattern_tag,
            timestamp=t_now + timedelta(seconds=len(executions) + 2),
        ),
        auth_context=h.master,
    )
    return task_id, run_id


def test_p10_02_min_occurrences_threshold_and_three_pattern_categories(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Detect repeated_error, redundant_operations, and missing_skill_gap only at >= min_occurrences."""
    proc = _new_process_service(dept_harness)
    t0 = utc(10, 0, 0)

    # Seed first 2 failing Tasks -> default min_occurrences=3 must NOT emit premature pattern!
    for i in range(2):
        _seed_task_with_tool_sequence(
            dept_harness,
            title=f"CSV failure task {i + 1}",
            offset_s=i * 20,
            executions=[
                ToolExecutionRecord(
                    execution_id=f"tex_err_fail_{i}",
                    tool_id="tools.files.read",
                    actor_id="it.coder",
                    action="files.read",
                    status="FAILED",
                    error_code="CSV_ENCODING_ERR",
                    error_message="UTF-8 BOM error with secret sk-1234567890abcdef1234567890abcdef",
                    duration_ms=40,
                    executed_at=t0,
                ),
                ToolExecutionRecord(
                    execution_id=f"tex_err_fix_{i}",
                    tool_id="tools.files.read",
                    actor_id="it.coder",
                    action="files.read",
                    status="SUCCESS",
                    duration_ms=30,
                    executed_at=t0,
                ),
                ToolExecutionRecord(
                    execution_id=f"tex_extra_list_{i}",
                    tool_id="tools.files.list",
                    actor_id="it.coder",
                    action="files.read",
                    status="SUCCESS",
                    duration_ms=15,
                    executed_at=t0,
                ),
                ToolExecutionRecord(
                    execution_id=f"tex_extra_list_b_{i}",
                    tool_id="tools.files.list",
                    actor_id="it.coder",
                    action="files.read",
                    status="SUCCESS",
                    duration_ms=15,
                    executed_at=t0,
                ),
            ],
        )

    under_threshold = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(timestamp=t0 + timedelta(minutes=2)),
        auth_context=dept_harness.master,
    )
    assert under_threshold.scanned_retrospective_count == 2
    assert under_threshold.created_improvements == []
    assert under_threshold.all_improvements == []

    # Add 3rd Task hitting the same repeated error, redundant calls/retries, and unassisted multi-tool flow!
    _seed_task_with_tool_sequence(
        dept_harness,
        title="CSV failure task 3",
        offset_s=60,
        executions=[
            ToolExecutionRecord(
                execution_id="tex_err_fail_2",
                tool_id="tools.files.read",
                actor_id="it.coder",
                action="files.read",
                status="FAILED",
                error_code="CSV_ENCODING_ERR",
                error_message="UTF-8 BOM error",
                duration_ms=50,
                executed_at=t0,
            ),
            ToolExecutionRecord(
                execution_id="tex_err_fix_2",
                tool_id="tools.files.read",
                actor_id="it.coder",
                action="files.read",
                status="SUCCESS",
                duration_ms=35,
                executed_at=t0,
            ),
            ToolExecutionRecord(
                execution_id="tex_extra_list_2",
                tool_id="tools.files.list",
                actor_id="it.coder",
                action="files.read",
                status="SUCCESS",
                duration_ms=20,
                executed_at=t0,
            ),
            ToolExecutionRecord(
                execution_id="tex_extra_list_b_2",
                tool_id="tools.files.list",
                actor_id="it.coder",
                action="files.read",
                status="SUCCESS",
                duration_ms=20,
                executed_at=t0,
            ),
        ],
    )

    # Also seed 3 Tasks that DID invoke an approved skill -> must NOT count toward missing_skill_gap!
    for j in range(3):
        _seed_task_with_tool_sequence(
            dept_harness,
            title=f"Assisted skill task {j}",
            pattern_tag="assisted_skill_workflow",
            skill_invoked="assisted_skill_workflow",
            offset_s=100 + j * 20,
            executions=[
                ToolExecutionRecord(
                    execution_id=f"tex_assisted_{j}",
                    tool_id="tools.files.write",
                    actor_id="it.coder",
                    action="files.write",
                    status="SUCCESS",
                    duration_ms=25,
                    executed_at=t0,
                )
            ],
        )

    outcome = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(timestamp=t0 + timedelta(minutes=5)),
        auth_context=dept_harness.master,
    )
    assert outcome.scanned_retrospective_count == 6
    by_type = {imp.pattern_type: imp for imp in outcome.created_improvements}
    assert {"repeated_error", "redundant_operations", "missing_skill_gap"} <= set(by_type)
    assert all(imp.pattern_key != "gap_assisted_skill_workflow" for imp in outcome.all_improvements)

    rep_err = by_type["repeated_error"]
    assert rep_err.metrics.occurrence_count == 3
    assert rep_err.metrics.scanned_task_count == 6
    assert rep_err.metrics.affected_task_ratio == pytest.approx(0.5)
    assert rep_err.metrics.error_occurrences == 3
    assert rep_err.metrics.cost_usd_total is None  # missing provider cost must stay None
    assert rep_err.observed_fixes  # genuine tool_retry_recovery extracted cleanly
    assert "sk-1234567890" not in rep_err.model_dump_json()

    red_ops = by_type["redundant_operations"]
    assert red_ops.metrics.occurrence_count == 3
    assert red_ops.metrics.redundant_extra_calls >= 3


def test_p10_02_deduplication_version_advance_status_and_restart_recovery(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify idempotent rescans, version advancement on new evidence, status retention, and SQLite restart."""
    proc = _new_process_service(dept_harness)
    t0 = utc(11, 0, 0)

    for i in range(3):
        _seed_task_with_tool_sequence(
            dept_harness,
            title=f"Tagged pattern task {i}",
            pattern_tag="pdf_table_cleanup",
            offset_s=i * 15,
            executions=[
                ToolExecutionRecord(
                    execution_id=f"tex_pdf_{i}",
                    tool_id="tools.office.pdf_extract",
                    actor_id="it.coder",
                    action="office.read",
                    status="SUCCESS",
                    duration_ms=100,
                    executed_at=t0,
                ),
                ToolExecutionRecord(
                    execution_id=f"tex_pdf_w_{i}",
                    tool_id="tools.files.write",
                    actor_id="it.coder",
                    action="files.write",
                    status="SUCCESS",
                    duration_ms=40,
                    executed_at=t0,
                ),
            ],
        )

    first = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(timestamp=t0 + timedelta(minutes=2)),
        auth_context=dept_harness.master,
    )
    assert len(first.created_improvements) == 1
    imp = first.created_improvements[0]
    assert imp.pattern_type == "missing_skill_gap"
    assert imp.suggested_skill_id == "pdf_table_cleanup"
    assert imp.record_version == 1
    evt_count_before_rescan = len(
        dept_harness.store.events.read_entries(event_type="process.pattern_detected")
    )

    # Repeat scan with identical retrospectives -> deduplicated, zero extra pattern events!
    rescan = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(timestamp=t0 + timedelta(minutes=3)),
        auth_context=dept_harness.master,
    )
    assert rescan.created_improvements == []
    assert rescan.updated_improvements == []
    assert len(rescan.unchanged_improvements) == 1
    assert rescan.unchanged_improvements[0].improvement_id == imp.improvement_id
    assert (
        len(dept_harness.store.events.read_entries(event_type="process.pattern_detected"))
        == evt_count_before_rescan
    )

    # Promote status to CANDIDATE_CREATED, then add a 4th Task matching the pattern.
    promoted = proc.update_improvement_status(
        imp.improvement_id,
        status="CANDIDATE_CREATED",
        auth_context=dept_harness.master,
        timestamp=t0 + timedelta(minutes=4),
    )
    assert promoted.status == "CANDIDATE_CREATED"
    assert promoted.record_version == 2

    _seed_task_with_tool_sequence(
        dept_harness,
        title="Tagged pattern task 4",
        pattern_tag="pdf_table_cleanup",
        offset_s=90,
        executions=[
            ToolExecutionRecord(
                execution_id="tex_pdf_3",
                tool_id="tools.office.pdf_extract",
                actor_id="it.coder",
                action="office.read",
                status="SUCCESS",
                duration_ms=100,
                executed_at=t0,
            ),
            ToolExecutionRecord(
                execution_id="tex_pdf_w_3",
                tool_id="tools.files.write",
                actor_id="it.coder",
                action="files.write",
                status="SUCCESS",
                duration_ms=40,
                executed_at=t0,
            ),
        ],
    )

    scan_v4 = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(timestamp=t0 + timedelta(minutes=6)),
        auth_context=dept_harness.master,
    )
    assert len(scan_v4.updated_improvements) == 1
    updated = scan_v4.updated_improvements[0]
    assert updated.improvement_id == imp.improvement_id
    assert updated.metrics.occurrence_count == 4
    assert updated.status == "CANDIDATE_CREATED"  # preserved across metrics update!
    assert updated.record_version == 3

    # Restart recovery check on reopened store.
    db_path = dept_harness.db_path
    dept_harness.store.close()
    reopened = SQLiteRepositoryStore(db_path)
    try:
        re_pm = PermissionManager(
            evaluator=PolicyEvaluator(),
            journal=reopened.events,
            approval_service=ApprovalService(reopened.approvals, reopened.events),
        )
        re_proc = ProcessDepartmentService(
            permission_manager=re_pm,
            journal=reopened.events,
            retrospective_repo=reopened.process,
        )
        recovered = re_proc.get_suggested_improvement(
            imp.improvement_id, auth_context=dept_harness.master
        )
        assert recovered is not None
        assert recovered.metrics.occurrence_count == 4
        assert recovered.status == "CANDIDATE_CREATED"
    finally:
        reopened.close()


def test_p10_02_security_ownership_and_transaction_rollback(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Block unauthorized worker cross-task scans and roll back SQLite writes atomically on error."""
    proc = _new_process_service(dept_harness)
    t0 = utc(12, 0, 0)

    t1, _ = _seed_task_with_tool_sequence(
        dept_harness,
        title="Worker isolation task 1",
        pattern_tag="shared_report",
        offset_s=0,
        executions=[],
    )
    t2, _ = _seed_task_with_tool_sequence(
        dept_harness,
        title="Worker isolation task 2",
        pattern_tag="shared_report",
        offset_s=10,
        executions=[],
    )

    rogue_worker = worker_ctx("it.coder", assigned_task_ids=[t1])
    with pytest.raises(ProcessOwnershipError):
        proc.analyze_patterns_and_suggest_improvements(
            PatternAnalysisRequest(min_occurrences=2, timestamp=t0 + timedelta(minutes=1)),
            auth_context=rogue_worker,
        )
    with pytest.raises(ProcessOwnershipError):
        proc.analyze_patterns_and_suggest_improvements(
            PatternAnalysisRequest(
                task_ids=[t1, t2],
                min_occurrences=2,
                timestamp=t0 + timedelta(minutes=1),
            ),
            auth_context=rogue_worker,
        )

    with pytest.raises(ValidationError):
        PatternAnalysisRequest(min_occurrences=1, timestamp=t0)  # min_occurrences < 2 rejected

    # Rollback atomicity when _sync_improvement_task_links raises SQLite error mid-transaction.
    orig_links = dept_harness.store.process._sync_improvement_task_links

    def _boom_links(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("Simulated improvement link write failure")

    dept_harness.store.process._sync_improvement_task_links = _boom_links  # type: ignore[method-assign]
    before_events = len(
        dept_harness.store.events.read_entries(event_type="process.pattern_detected")
    )
    try:
        with pytest.raises(RepositoryError, match="Simulated improvement link write failure"):
            proc.analyze_patterns_and_suggest_improvements(
                PatternAnalysisRequest(min_occurrences=2, timestamp=t0 + timedelta(minutes=2)),
                auth_context=dept_harness.master,
            )
    finally:
        dept_harness.store.process._sync_improvement_task_links = orig_links  # type: ignore[method-assign]

    assert dept_harness.store.process.list_improvements() == []
    assert (
        len(dept_harness.store.events.read_entries(event_type="process.pattern_detected"))
        == before_events
    )
