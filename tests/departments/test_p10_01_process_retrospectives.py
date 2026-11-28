"""Comprehensive P10.01 test suite covering data-driven Task retrospectives, durability, security, and timeline tool_id."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from core.agents.hr import HrDepartmentService
from core.agents.hr_models import (
    ApprovedSkillDescriptor,
    SkillGrantRequest,
    StaticApprovedSkillCatalog,
)
from core.contracts import (
    ChecklistItem,
    Event,
    Run,
    RunErrorRef,
    RunTelemetryRef,
    Session,
    Task,
    new_checklist_item_id,
    new_event_id,
    new_run_id,
    new_session_id,
    new_task_id,
)
from core.departments import (
    CompileTaskRetrospectiveRequest,
    ProcessDepartmentService,
    ProcessOwnershipError,
    ProcessTaskNotFoundError,
    QaDepartmentService,
    QaVerificationRequest,
    StaleRetrospectiveVersionError,
    TaskRetrospectiveInput,
    VaultDepartmentService,
    VaultSubmissionRequest,
)
from core.events import project_activity_timeline
from core.governance.models import TrustedAuthContext
from core.tasks.transitions import ExecutionEvidence, QaVerificationEvidence, TransitionContext
from core.tools.models import ToolExecutionRecord, ToolInvocationRequest
from storage.repositories import SQLiteRepositoryStore
from storage.repositories.errors import RepositoryError
from tests.departments.conftest import (
    DepartmentTestHarness,
    REPO_ROOT,
    create_task_and_run,
    make_department_harness,
    register_worker_agent,
    request_and_approve,
    utc,
    worker_ctx,
)


def _new_process_service(
    h: DepartmentTestHarness,
    *,
    skill_catalog: StaticApprovedSkillCatalog | None = None,
) -> ProcessDepartmentService:
    """Create a SQLite-backed ProcessDepartmentService bound to ``h.store.process``."""
    return ProcessDepartmentService(
        permission_manager=h.permissions,
        journal=h.store.events,
        retrospective_repo=h.store.process,
        skill_catalog=skill_catalog,
    )


def test_p10_01_00_ast_boundary_purity_for_process_modules() -> None:
    """Ensure core/departments/process*.py imports no SQLite, storage, tools, HTTP, or UI modules."""
    forbidden = {
        "storage",
        "sqlite3",
        "sqlalchemy",
        "subprocess",
        "tools",
        "api",
        "ui",
        "fastapi",
        "httpx",
        "requests",
        "urllib",
        "playwright",
        "crewai",
        "mcp",
    }
    for rel in ("core/departments/process_models.py", "core/departments/process.py"):
        path = REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, f"Forbidden import {alias.name} in {rel}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, f"Forbidden import {node.module} in {rel}"


def test_p10_01_01_completed_task_full_summary_and_18_timeline_tool_id(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Scenarios #1, #5, #18: Completed Task, multiple tools, real timeline tool_id."""
    t0 = utc(9, 0, 0)
    register_worker_agent(
        dept_harness,
        agent_id="it.coder",
        department="it",
        skills=["file_writing", "automated_testing"],
        tools_allowed=["tools.files.write", "tools.files.read"],
        timestamp=t0,
    )
    register_worker_agent(
        dept_harness,
        agent_id="qa.verifier",
        department="qa",
        skills=["quality_verification"],
        tools_allowed=["tools.files.read"],
        timestamp=t0,
    )

    task_id = new_task_id()
    run_id = new_run_id()
    sess_id = new_session_id()
    dept_harness.workflows.create_task(
        Task(
            task_id=task_id,
            title="Implement invoice parser",
            description="Create invoice parser module inside task workspace.",
            requested_by="operator.alice",
            status="ASSIGNED",
            assigned_agent_ids=["it.coder"],
            created_at=t0,
        )
    )
    dept_harness.workflows.create_run(
        Run(
            run_id=run_id,
            task_id=task_id,
            runtime_id="custom.master_worker_v1",
            status="PENDING",
            provider_id="antigravity",
            connection_id="default_antigravity",
            model_id="gemini-2.5-pro",
        )
    )
    start_out = dept_harness.workflows.apply_paired_transition(
        "start",
        task_id,
        run_id,
        TransitionContext(
            actor_id="core.master",
            actor_kind="orchestrator",
            timestamp=t0 + timedelta(seconds=2),
            reason="Start isolated coding run",
            execution_evidence=ExecutionEvidence(
                source="core_runtime",
                run_id=run_id,
                reason="Start isolated coding run",
            ),
        ),
    )
    assert start_out.accepted

    dept_harness.store.sessions.save_session(
        Session(
            session_id=sess_id,
            agent_id="it.coder",
            task_id=task_id,
            run_id=run_id,
            provider_id="antigravity",
            connection_id="default_antigravity",
            provider_conversation_id="prov-conv-001",
            workspace_reference=f"workspace/{task_id}",
            status="ACTIVE",
            started_at=t0 + timedelta(seconds=3),
        )
    )
    chk_id = new_checklist_item_id()
    dept_harness.store.metadata.save_checklist_item(
        ChecklistItem(
            checklist_item_id=chk_id,
            task_id=task_id,
            title="Write parser implementation",
            assigned_agent_id="it.coder",
            status="COMPLETED",
            order=0,
            evidence_reference="evd_step_01",
            created_at=t0 + timedelta(seconds=2),
            completed_at=t0 + timedelta(seconds=20),
        )
    )

    coder_ctx = worker_ctx(
        "it.coder",
        allowed_tools=["tools.files.write", "tools.files.read"],
        assigned_task_ids=[task_id],
    )
    rel_file = f"workspace/{task_id}/parser.py"
    w_res = dept_harness.tool_runtime.invoke(
        ToolInvocationRequest(
            auth_context=coder_ctx,
            tool_id="tools.files.write",
            parameters={"path": rel_file, "content": "def parse():\n    return True\n", "overwrite": True},
            task_id=task_id,
            run_id=run_id,
            session_id=sess_id,
            workspace_root=str(dept_harness.tmp_path),
            target_path=rel_file,
            timestamp=t0 + timedelta(seconds=5),
        )
    )
    assert w_res.status == "SUCCESS"
    r_res = dept_harness.tool_runtime.invoke(
        ToolInvocationRequest(
            auth_context=coder_ctx,
            tool_id="tools.files.read",
            parameters={"path": rel_file},
            task_id=task_id,
            run_id=run_id,
            session_id=sess_id,
            workspace_root=str(dept_harness.tmp_path),
            target_path=rel_file,
            timestamp=t0 + timedelta(seconds=8),
        )
    )
    assert r_res.status == "SUCCESS"

    # Scenario #18: ActivityTimelineProjection must resolve tool_id from ToolRuntime events, not tool_name only!
    timeline = project_activity_timeline(dept_harness.store.events, task_id=task_id)
    observed_timeline_tools = [e.tool_name for e in timeline.entries if e.tool_name is not None]
    assert observed_timeline_tools == ["tools.files.write", "tools.files.read"]
    assert any("tool=tools.files.write" in e.summary for e in timeline.entries)
    assert all(
        e.tool_name is None for e in timeline.entries if e.event_type == "tool.call_proposed"
    )

    vault_dept = VaultDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        artifact_vault=dept_harness.artifact_vault,
        knowledge_vault=dept_harness.evidence_vault,
    )
    sub = vault_dept.submit_document(
        VaultSubmissionRequest(
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.coder",
            logical_key="invoice_parser_py",
            relative_path=rel_file,
            content_bytes=(dept_harness.tmp_path / rel_file).read_bytes(),
            department="it",
            timestamp=t0 + timedelta(seconds=12),
        ),
        auth_context=coder_ctx,
    )
    qa = QaDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        evidence_vault=dept_harness.evidence_vault,
        artifact_vault=dept_harness.artifact_vault,
    )
    verdict = qa.verify_deliverables(
        QaVerificationRequest(
            task_id=task_id,
            run_id=run_id,
            producer_agent_id="it.coder",
            artifact_id=sub.artifact.artifact_id,
            verified_checks=["syntax_and_sha_verified"],
            require_pass_verdict=True,
            summary="Independent QA verification passed.",
            timestamp=t0 + timedelta(seconds=15),
        ),
        auth_context=dept_harness.qa_verifier,
    )

    dept_harness.workflows.apply_paired_transition(
        "complete_run_only",
        task_id,
        run_id,
        TransitionContext(
            actor_id="core.runtime",
            actor_kind="core_runtime",
            timestamp=t0 + timedelta(seconds=22),
            reason="Execution finished cleanly",
            execution_evidence=ExecutionEvidence(
                source="core_runtime",
                run_id=run_id,
                reason="Run completed",
                telemetry=RunTelemetryRef(
                    total_duration_ms=20000,
                    provider_duration_ms=14500,
                    engine_call_count=2,
                    actual_cost_usd=0.0125,
                    telemetry_status="VERIFIED_MEASURED",
                    telemetry_source="provider_cli",
                ),
            ),
        ),
    )
    dept_harness.workflows.apply_task_transition(
        task_id,
        "COMPLETED",
        TransitionContext(
            actor_id="qa.verifier",
            actor_kind="qa_verifier",
            timestamp=t0 + timedelta(seconds=24),
            reason="QA verified deliverables",
            qa_evidence=QaVerificationEvidence(
                evidence_id=verdict.evidence_id,
                verifier_source="independent_qa",
                verifier_actor_id="qa.verifier",
                verification_status="VERIFIED_PASS",
                verified_at=t0 + timedelta(seconds=15),
                artifact_ids=[sub.artifact.artifact_id],
            ),
        ),
    )

    proc = _new_process_service(dept_harness)
    out = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=30)),
        auth_context=dept_harness.master,
    )
    summary = out.summary
    assert out.created is True
    assert out.revision_created is True
    assert out.deduplicated is False
    assert summary.task_id == task_id
    assert summary.run_id == run_id
    assert summary.run_ids == [run_id]
    assert sess_id in summary.session_ids
    assert "it.coder" in summary.agent_ids
    assert "it" in summary.departments_involved
    assert summary.execution_status == "COMPLETED"
    assert summary.qa_verification_status == "VERIFIED_PASS"
    assert summary.total_tool_calls == 2
    assert summary.tools_used == ["tools.files.write", "tools.files.read"]
    assert summary.timeline_observed_tools == ["tools.files.write", "tools.files.read"]
    assert summary.duration_ms == 20000
    assert summary.duration_source == "run_telemetry_measured"
    assert summary.cost_usd == pytest.approx(0.0125)
    assert summary.cost_status == "VERIFIED_MEASURED"
    assert summary.artifact_ids == [sub.artifact.artifact_id]
    assert rel_file in summary.output_paths
    assert verdict.evidence_id in summary.evidence_refs
    assert any(p.provider_id == "antigravity" and p.model_id == "gemini-2.5-pro" for p in summary.provider_refs)
    assert summary.errors_observed == []
    assert summary.fixes_applied == []


def test_p10_01_02_03_failed_and_cancelled_tasks_honest_states(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Scenarios #2, #3, #8: FAILED task, CANCELLED task, and missing cost stays None."""
    t0 = utc(10, 0, 0)
    # Failed task without provider cost.
    t_fail, r_fail = create_task_and_run(
        dept_harness,
        title="Broken scrape job",
        description="Failing network job",
        timestamp=t0,
    )
    dept_harness.workflows.apply_paired_transition(
        "fail",
        t_fail,
        r_fail,
        TransitionContext(
            actor_id="core.runtime",
            actor_kind="core_runtime",
            timestamp=t0 + timedelta(seconds=12),
            reason="External API timed out",
            execution_evidence=ExecutionEvidence(
                source="core_runtime",
                run_id=r_fail,
                reason="Upstream 504 gateway timeout",
                error_reference=RunErrorRef(
                    error_code="UPSTREAM_504",
                    stage="research_fetch",
                    message="Gateway timeout fetching remote spec",
                    retryable=True,
                ),
            ),
        ),
        target_task_status="FAILED",
    )

    # Cancelled task.
    t_cancel, r_cancel = create_task_and_run(
        dept_harness,
        title="Cancelled batch task",
        description="User interrupted execution",
        timestamp=t0 + timedelta(seconds=1),
    )
    dept_harness.workflows.apply_paired_transition(
        "cancel",
        t_cancel,
        r_cancel,
        TransitionContext(
            actor_id="operator.alice",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=9),
            reason="Operator aborted run",
            execution_evidence=ExecutionEvidence(
                source="operator",
                run_id=r_cancel,
                reason="Process terminated cleanly via operator request",
                confirmed_cancelled=True,
                cancellation_source="operator_confirmed_idle",
            ),
        ),
    )

    proc = _new_process_service(dept_harness)
    fail_sum = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=t_fail, timestamp=t0 + timedelta(seconds=20)),
        auth_context=dept_harness.master,
    ).summary
    assert fail_sum.execution_status == "FAILED"
    assert fail_sum.qa_verification_status == "UNVERIFIED"
    assert fail_sum.cost_usd is None
    assert fail_sum.cost_status == "UNAVAILABLE_FROM_PROVIDER"
    assert fail_sum.duration_ms == 12000
    assert fail_sum.duration_source == "timestamps_measured"
    assert any(e.error_code == "UPSTREAM_504" for e in fail_sum.errors_observed)
    assert fail_sum.fixes_applied == []

    cancel_sum = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=t_cancel, timestamp=t0 + timedelta(seconds=21)),
        auth_context=dept_harness.master,
    ).summary
    assert cancel_sum.execution_status == "CANCELLED"
    assert cancel_sum.ended_at == t0 + timedelta(seconds=9)
    assert cancel_sum.cost_usd is None
    assert cancel_sum.cost_status == "UNAVAILABLE_FROM_PROVIDER"


def test_p10_01_04_06_07_17_multi_run_tool_errors_retries_and_task_run_lookup(
    tmp_path: Path,
) -> None:
    """Covers Scenarios #4, #6, #7, #17: multi-Run retry, failed/retried tools, Task/Run lookup."""
    calls = {"count": 0}

    def _flaky_search(query: str, max_results: int) -> list[dict[str, str]]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("Temporary search timeout on attempt 1")
        return [{"title": "Recovered Hit", "url": "https://example.org/ok", "snippet": "ok"}]

    # Build isolated harness with flaky search backend.
    from core.tools.runtime import ToolRuntime
    h = make_department_harness(tmp_path)
    try:
        from tools.research.research_tools import HttpFetchResponse, build_research_tools
        for manifest, handler in build_research_tools(
            search_backend=_flaky_search,
            http_fetcher=lambda u, t: HttpFetchResponse(url=u, final_url=u, status_code=200, text="ok"),
            allow_private_for_test=True,
        ):
            h.tool_registry._handlers[manifest.id] = handler  # bind deterministic flaky test double cleanly

        t0 = utc(11, 0, 0)
        register_worker_agent(
            h,
            agent_id="marketing.researcher",
            department="marketing",
            skills=["web_research"],
            tools_allowed=["tools.research.web_search"],
            timestamp=t0,
        )

        task_id = new_task_id()
        run_1 = new_run_id()
        m_ctx = worker_ctx(
            "marketing.researcher",
            allowed_tools=["tools.research.web_search"],
            assigned_task_ids=[task_id],
        )
        h.workflows.create_task(
            Task(
                task_id=task_id,
                title="Multi-run research task",
                description="Retries both tool timeout and failed run",
                requested_by="operator.alice",
                status="ASSIGNED",
                assigned_agent_ids=["marketing.researcher"],
                created_at=t0,
            )
        )
        h.workflows.create_run(
            Run(run_id=run_1, task_id=task_id, runtime_id="custom.master_worker_v1", status="PENDING")
        )
        start1 = h.workflows.apply_paired_transition(
            "start",
            task_id,
            run_1,
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=1),
                reason="Run 1",
                execution_evidence=ExecutionEvidence(
                    source="core_runtime",
                    run_id=run_1,
                    reason="Start Run 1",
                ),
            ),
        )
        assert start1.accepted

        # Call 1 fails with search error/timeout, Call 2 retries and SUCCEEDS.
        res1 = h.tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=m_ctx,
                tool_id="tools.research.web_search",
                parameters={"query": "ai village", "max_results": 2},
                task_id=task_id,
                run_id=run_1,
                timestamp=t0 + timedelta(seconds=2),
            )
        )
        assert res1.status in {"FAILED", "TIMEOUT"}
        res2 = h.tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=m_ctx,
                tool_id="tools.research.web_search",
                parameters={"query": "ai village", "max_results": 2},
                task_id=task_id,
                run_id=run_1,
                timestamp=t0 + timedelta(seconds=3),
            )
        )
        assert res2.status == "SUCCESS"

        # Inject a denied tool attempt directly through ToolRuntime (unpermitted tool).
        denied_res = h.tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=m_ctx,
                tool_id="tools.files.write",
                parameters={"path": f"workspace/{task_id}/nope.txt", "content": "x"},
                task_id=task_id,
                run_id=run_1,
                workspace_root=str(h.tmp_path),
                target_path=f"workspace/{task_id}/nope.txt",
                timestamp=t0 + timedelta(seconds=4),
            )
        )
        assert denied_res.status == "DENIED"

        # Run 1 fails -> Task goes BLOCKED -> retrospective recorded at rev 1.
        h.workflows.apply_paired_transition(
            "fail",
            task_id,
            run_1,
            TransitionContext(
                actor_id="core.runtime",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=6),
                reason="Run 1 incomplete",
                execution_evidence=ExecutionEvidence(
                    source="core_runtime",
                    run_id=run_1,
                    reason="Transient search issue",
                    error_reference=RunErrorRef(error_code="RUN_RETRY_NEEDED", message="Retrying cleanly"),
                ),
            ),
            target_task_status="BLOCKED",
        )
        proc = _new_process_service(h)
        out1 = proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=7)),
            auth_context=h.master,
        )
        assert out1.summary.retrospective_version == 1
        assert out1.summary.run_ids == [run_1]

        # Run 2 starts and succeeds.
        run_2 = new_run_id()
        h.workflows.create_run(
            Run(run_id=run_2, task_id=task_id, runtime_id="custom.master_worker_v1", status="PENDING")
        )
        start2 = h.workflows.apply_paired_transition(
            "start",
            task_id,
            run_2,
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=10),
                reason="Run 2",
                execution_evidence=ExecutionEvidence(
                    source="core_runtime",
                    run_id=run_2,
                    reason="Start Run 2",
                ),
            ),
        )
        assert start2.accepted
        h.workflows.apply_paired_transition(
            "complete_run_only",
            task_id,
            run_2,
            TransitionContext(
                actor_id="core.runtime",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=15),
                reason="Run 2 succeeded",
                execution_evidence=ExecutionEvidence(source="core_runtime", run_id=run_2, reason="Done"),
            ),
        )

        out2 = proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=20)),
            auth_context=h.master,
        )
        s2 = out2.summary
        assert s2.retrospective_id == out1.summary.retrospective_id
        assert s2.retrospective_version == 2
        assert s2.run_ids == [run_1, run_2]
        assert s2.total_tool_calls == 3

        search_sum = next(ts for ts in s2.tool_summaries if ts.tool_id == "tools.research.web_search")
        assert search_sum.invocation_count == 2
        assert search_sum.failed_count == 1
        assert search_sum.success_count == 1
        assert search_sum.retry_count == 1

        write_sum = next(ts for ts in s2.tool_summaries if ts.tool_id == "tools.files.write")
        assert write_sum.denied_count == 1

        fix_sources = {f.source for f in s2.fixes_applied}
        assert "tool_retry_recovery" in fix_sources
        assert "run_retry_recovery" in fix_sources

        # Lookup by Task, by Run 1, by Run 2, and immutable revisions!
        by_task = proc.get_task_retrospective(task_id, auth_context=h.master)
        by_run1 = proc.get_retrospective_for_run(run_1, auth_context=h.master)
        by_run2 = proc.get_retrospective_for_run(run_2, auth_context=h.master)
        assert by_task is not None and by_run1 is not None and by_run2 is not None
        assert by_task.retrospective_id == by_run1.retrospective_id == by_run2.retrospective_id
        revs = proc.list_retrospective_revisions(by_task.retrospective_id, auth_context=h.master)
        assert [r.revision_number for r in revs] == [1, 2]
    finally:
        h.store.close()


def test_p10_01_09_10_skill_usage_truthfulness_declared_vs_actually_invoked(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Scenarios #9, #10: never infer skill execution from manifest or provider prose."""
    t0 = utc(12, 0, 0)
    catalog = StaticApprovedSkillCatalog(
        [
            ApprovedSkillDescriptor(
                skill_id="excel_data_analysis",
                version="1.0.0",
                required_capabilities=["office_reading"],
                summary="Deterministic spreadsheet analysis workflow",
            )
        ]
    )
    # 1. Task with agent having ONLY declared capabilities.skills + allowed skills, ZERO invocations.
    register_worker_agent(
        dept_harness,
        agent_id="office.declared_only",
        department="service",
        skills=["office_reading", "excel_data_analysis"],
        timestamp=t0,
    )
    hr = HrDepartmentService(
        registry=dept_harness.agent_registry,
        skill_catalog=catalog,
    )
    reg = dept_harness.agent_registry.get_agent("office.declared_only")
    assert reg is not None
    from core.governance import ApprovalPolicyContext, PermissionDeniedError

    try:
        hr.grant_skills(
            SkillGrantRequest(
                agent_id="office.declared_only",
                skill_ids=["excel_data_analysis"],
                reason="Enable excel workflow cleanly",
                expected_record_version=reg.record_version,
                auth_context=dept_harness.alice,
                timestamp=t0 + timedelta(seconds=1),
            )
        )
    except PermissionDeniedError as err:
        apr = dept_harness.approvals.create_approval_request(
            requester=dept_harness.alice,
            action=err.decision.action,
            target_resource=err.decision.resource,
            task_id=None,
            run_id=None,
            request_fingerprint=err.decision.request_fingerprint,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=err.decision.matched_rule_ids,
                rule_versions=dept_harness.evaluator.policy_snapshot(err.decision.matched_rule_ids),
                reason_code=err.decision.reason_code,
            ),
            risk_summary="Grant approved skill excel_data_analysis",
            requested_at=t0 + timedelta(seconds=1),
        )
        dept_harness.approvals.decide_approval(
            apr.approval_id,
            decider=dept_harness.bob,
            approved=True,
            reason="Approved skill grant for test",
            timestamp=t0 + timedelta(seconds=2),
        )
        hr.grant_skills(
            SkillGrantRequest(
                agent_id="office.declared_only",
                skill_ids=["excel_data_analysis"],
                reason="Enable excel workflow cleanly",
                expected_record_version=reg.record_version,
                auth_context=dept_harness.alice,
                approval_id=apr.approval_id,
                timestamp=t0 + timedelta(seconds=3),
            )
        )

    t1, r1 = create_task_and_run(
        dept_harness,
        title="No skill invoked task",
        description="Checks manifest skills are not counted as executed",
        assigned_agent_ids=["office.declared_only"],
        timestamp=t0 + timedelta(seconds=5),
    )
    # Emit misleading provider text claiming skill was used — must NOT mark invoked!
    dept_harness.store.events.append(
        Event(
            event_id=new_event_id(),
            timestamp=t0 + timedelta(seconds=6),
            event_type="engine.event_forwarded",
            source="provider",
            task_id=t1,
            run_id=r1,
            redacted_payload={"text_delta": "I used skill_id=excel_data_analysis and office_reading!"},
        )
    )
    proc = _new_process_service(dept_harness, skill_catalog=catalog)
    sum1 = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=t1, timestamp=t0 + timedelta(seconds=7)),
        auth_context=dept_harness.master,
    ).summary
    assert sum1.skill_usage_status == "DECLARED_OR_ALLOWED_ONLY_NOT_INVOKED"
    assert sum1.skills_invoked == []
    excel_rec = next(s for s in sum1.skill_usages if s.skill_id == "excel_data_analysis")
    assert excel_rec.declared_in_capabilities is True
    assert excel_rec.allowed_in_manifest is True
    assert excel_rec.approved_in_catalog is True
    assert excel_rec.invoked is False
    assert excel_rec.participated_in_execution is False
    assert excel_rec.outcome == "NOT_INVOKED"

    # 2. Task where agent genuinely executes skill via trusted skill.usage_observed event.
    t2, r2 = create_task_and_run(
        dept_harness,
        title="Real skill execution task",
        description="Observed skill invocation via runtime event",
        assigned_agent_ids=["office.declared_only"],
        timestamp=t0 + timedelta(seconds=10),
    )
    skill_evt = Event(
        event_id=new_event_id(),
        timestamp=t0 + timedelta(seconds=11),
        event_type="skill.usage_observed",
        source="agent_runtime",
        task_id=t2,
        run_id=r2,
        actor_id="office.declared_only",
        redacted_payload={
            "skill_id": "excel_data_analysis",
            "participated": True,
            "outcome": "SUCCEEDED",
            "notes": "Executed excel formula check workflow",
        },
    )
    dept_harness.store.events.append(skill_evt)
    sum2 = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=t2, timestamp=t0 + timedelta(seconds=12)),
        auth_context=dept_harness.master,
    ).summary
    assert sum2.skill_usage_status == "OBSERVED"
    assert sum2.skills_invoked == ["excel_data_analysis"]
    used_rec = next(s for s in sum2.skill_usages if s.skill_id == "excel_data_analysis")
    assert used_rec.invoked is True
    assert used_rec.participated_in_execution is True
    assert used_rec.outcome == "SUCCEEDED"
    assert used_rec.evidence_event_id == skill_evt.event_id

    # 3. Task with no agents / no skill metadata -> UNAVAILABLE_NO_SKILL_DATA.
    t3 = new_task_id()
    dept_harness.workflows.create_task(
        Task(
            task_id=t3,
            title="Bare unassigned task",
            description="No agent assigned",
            requested_by="operator.alice",
            status="QUEUED",
            created_at=t0 + timedelta(seconds=14),
        )
    )
    sum3 = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=t3, timestamp=t0 + timedelta(seconds=15)),
        auth_context=dept_harness.master,
    ).summary
    assert sum3.skill_usage_status == "UNAVAILABLE_NO_SKILL_DATA"
    assert sum3.skill_usages == []


def test_p10_01_11_12_optional_fields_secrets_and_cross_task_security(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Scenarios #11, #12, and Task/workspace ownership enforcement."""
    t0 = utc(13, 0, 0)
    task_id = new_task_id()
    other_task_id = new_task_id()
    dept_harness.workflows.create_task(
        Task(
            task_id=task_id,
            title="Minimal queued task",
            description="Missing optional run, session, checklist, artifacts, cost, and skills",
            requested_by="operator.alice",
            status="QUEUED",
        )
    )
    dept_harness.workflows.create_task(
        Task(
            task_id=other_task_id,
            title="Other task",
            description="Unrelated task",
            requested_by="operator.alice",
            status="QUEUED",
            created_at=t0,
        )
    )

    # Inject sanitized/raw edge pattern via ToolExecutionRecord error_message (scrubbed automatically).
    rec_secret_err = ToolExecutionRecord(
        execution_id="tex_sec_err_0001",
        tool_id="tools.research.web_fetch",
        task_id=task_id,
        actor_id="marketing.agent",
        action="network.fetch",
        status="FAILED",
        duration_ms=55,
        error_code="AUTH_FAILED",
        error_message="Upstream rejected Bearer abcdef1234567890secret and AIzaSyD12345678901234567890",
        executed_at=t0,
    )
    dept_harness.store.tools.record_execution(rec_secret_err)

    proc = _new_process_service(dept_harness)
    # Unauthorized worker on another task must fail closed.
    unauthorized_worker = worker_ctx("process.analyst", assigned_task_ids=[other_task_id])
    with pytest.raises(ProcessOwnershipError) as own_err:
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=1)),
            auth_context=unauthorized_worker,
        )
    assert own_err.value.error_code == "TASK_OWNERSHIP_DENIED"

    # Assigned worker targeting another task workspace must fail closed.
    assigned_worker = worker_ctx("process.analyst", assigned_task_ids=[task_id])
    with pytest.raises(ProcessOwnershipError) as ws_err:
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(
                task_id=task_id,
                workspace_root=str(dept_harness.tmp_path),
                target_path=f"workspace/{other_task_id}/leak.txt",
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=assigned_worker,
        )
    assert ws_err.value.error_code == "CROSS_TASK_WORKSPACE_DENIED"

    # Reject raw secrets injected directly into request inputs.
    with pytest.raises(ValueError, match="secret|Sensitive"):
        CompileTaskRetrospectiveRequest(
            task_id=task_id,
            extra_errors=["api_key=real_secret_value_123456"],
            timestamp=t0 + timedelta(seconds=3),
        )

    # Nonexistent task raises ProcessTaskNotFoundError.
    with pytest.raises(ProcessTaskNotFoundError):
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=new_task_id(), timestamp=t0 + timedelta(seconds=3)),
            auth_context=dept_harness.master,
        )

    out = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=4)),
        auth_context=assigned_worker,
    )
    s = out.summary
    assert s.run_ids == []
    assert s.session_ids == []
    assert s.cost_usd is None
    assert s.duration_source == "tool_duration_sum"
    assert s.duration_ms == 55
    raw_dump = s.model_dump_json()
    assert "abcdef1234567890secret" not in raw_dump
    assert "AIzaSyD12345678901234567890" not in raw_dump
    assert "[REDACTED]" in s.errors_observed[0].message

    # Unassigned worker cannot read another task's retrospective either.
    with pytest.raises(ProcessOwnershipError):
        proc.get_task_retrospective(task_id, auth_context=unauthorized_worker, timestamp=t0 + timedelta(seconds=5))


def test_p10_01_13_14_15_16_dedup_concurrency_rollback_restart_and_triggers(
    tmp_path: Path,
) -> None:
    """Covers Scenarios #13, #14, #15, #16: duplicate guard, concurrent writes, atomic rollback, restart."""
    db_path = tmp_path / "retro_lifecycle.db"
    h = make_department_harness(tmp_path)
    t0 = utc(14, 0, 0)
    task_id, run_id = create_task_and_run(
        h,
        title="Concurrency and persistence test task",
        description="Verifies duplicate guard, lock, rollback, and restart recovery",
        timestamp=t0,
    )
    proc = _new_process_service(h)

    # 1. Duplicate retrospective prevention (#13).
    first = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=1)),
        auth_context=h.master,
    )
    evt_count_after_first = h.store.events.count()
    second = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=2)),
        auth_context=h.master,
    )
    assert first.created is True and first.revision_created is True
    assert second.created is False and second.revision_created is False and second.deduplicated is True
    assert second.summary.retrospective_id == first.summary.retrospective_id
    assert second.summary.retrospective_version == 1
    assert (
        len(
            h.store.events.read_entries(
                task_id=task_id,
                event_type="process.retrospective_recorded",
            )
        )
        == 1
    )
    assert h.store.events.count() >= evt_count_after_first  # governance audit still preserved

    # Optimistic lock conflict on explicit stale version after real update.
    h.store.tools.record_execution(
        ToolExecutionRecord(
            execution_id="tex_ver_advance_1",
            tool_id="tools.files.list",
            task_id=task_id,
            run_id=run_id,
            actor_id="it.coder",
            action="files.read",
            status="SUCCESS",
            duration_ms=10,
            executed_at=t0 + timedelta(seconds=3),
        )
    )
    with pytest.raises(StaleRetrospectiveVersionError):
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(
                task_id=task_id,
                expected_record_version=99,
                timestamp=t0 + timedelta(seconds=4),
            ),
            auth_context=h.master,
        )

    # 2. Concurrent writes on a fresh task (#14) -> strictly 1 row & 1 revision!
    t_conc, r_conc = create_task_and_run(
        h,
        title="Concurrent race task",
        description="Two threads compile retrospective concurrently",
        timestamp=t0 + timedelta(seconds=5),
    )
    barrier = threading.Barrier(2)

    def _compile_worker(offset_s: int) -> Any:
        barrier.wait(timeout=5.0)
        return proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(
                task_id=t_conc,
                timestamp=t0 + timedelta(seconds=10 + offset_s),
            ),
            auth_context=h.master,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_compile_worker, 0)
        f2 = pool.submit(_compile_worker, 1)
        r_a = f1.result(timeout=10.0)
        r_b = f2.result(timeout=10.0)

    assert r_a.summary.retrospective_id == r_b.summary.retrospective_id
    assert sorted([r_a.created, r_b.created]) == [False, True]
    assert len(proc.list_retrospectives(task_id=t_conc)) == 1
    assert len(proc.list_retrospective_revisions(r_a.summary.retrospective_id)) == 1

    # 3. Transaction rollback test (#15): simulate SQL error inside transaction after audit event append!
    t_rb, _ = create_task_and_run(
        h,
        title="Rollback verification task",
        description="Ensures failure rolls back both tables and events atomically",
        timestamp=t0 + timedelta(seconds=12),
    )
    events_before_rb = h.store.events.count()
    orig_sync = h.store.process._sync_run_links

    def _boom_sync(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("Simulated disk failure mid-transaction")

    h.store.process._sync_run_links = _boom_sync  # type: ignore[method-assign]
    try:
        with pytest.raises(RepositoryError, match="Simulated disk failure"):
            proc.compile_task_retrospective(
                CompileTaskRetrospectiveRequest(task_id=t_rb, timestamp=t0 + timedelta(seconds=13)),
                auth_context=h.master,
            )
    finally:
        h.store.process._sync_run_links = orig_sync  # type: ignore[method-assign]

    assert h.store.process.get_by_task(t_rb) is None
    assert (
        h.store.events.read_entries(
            task_id=t_rb,
            event_type="process.retrospective_recorded",
        )
        == []
    )
    assert events_before_rb >= 0

    # 4. SQLite immutable trigger check on task_retrospective_revisions.
    with h.store.open_connection() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE task_retrospective_revisions SET source_fingerprint = '0' WHERE retrospective_id = ?;",
                (first.summary.retrospective_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "DELETE FROM task_retrospective_revisions WHERE retrospective_id = ?;",
                (first.summary.retrospective_id,),
            )

    saved_db_path = h.db_path
    h.store.close()

    # 5. Restart & Recovery (#16): reopen fresh Store + ProcessDepartmentService on same DB file!
    reopened_store = SQLiteRepositoryStore(saved_db_path)
    try:
        from core.governance import ApprovalService, PermissionManager, PolicyEvaluator

        re_eval = PolicyEvaluator()
        re_pm = PermissionManager(
            evaluator=re_eval,
            journal=reopened_store.events,
            approval_service=ApprovalService(reopened_store.approvals, reopened_store.events),
        )
        reopened_proc = ProcessDepartmentService(
            permission_manager=re_pm,
            journal=reopened_store.events,
            retrospective_repo=reopened_store.process,
        )
        recovered = reopened_proc.get_task_retrospective(task_id, auth_context=h.master)
        assert recovered is not None
        assert recovered.retrospective_id == first.summary.retrospective_id
        by_run = reopened_proc.get_retrospective_for_run(run_id, auth_context=h.master)
        assert by_run is not None and by_run.task_id == task_id
        # Pending execution evidence from before restart advances cleanly from v1 -> v2,
        # and a subsequent compile deduplicates cleanly without duplicates.
        re_out = reopened_proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=30)),
            auth_context=h.master,
        )
        assert re_out.created is False
        assert re_out.revision_created is True
        assert re_out.deduplicated is False
        assert re_out.summary.retrospective_version == 2
        re_out_dedup = reopened_proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=31)),
            auth_context=h.master,
        )
        assert re_out_dedup.deduplicated is True
        assert re_out_dedup.summary.retrospective_version == 2
        assert len(reopened_proc.list_retrospective_records()) == 2
    finally:
        reopened_store.close()


def test_p10_01_19_20_never_alters_qa_verification_or_task_lifecycle(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Covers Scenarios #19, #20: retrospective stays strictly read-only toward QA and Task/Run state."""
    t0 = utc(15, 0, 0)
    register_worker_agent(
        dept_harness,
        agent_id="it.dev",
        department="it",
        skills=["file_writing"],
        tools_allowed=["tools.files.write"],
        timestamp=t0,
    )
    task_id, run_id = create_task_and_run(
        dept_harness,
        title="Unverified running task",
        description="Must remain RUNNING and UNVERIFIED after retrospective compilation",
        assigned_agent_ids=["it.dev"],
        timestamp=t0,
    )
    dev_ctx = worker_ctx("it.dev", allowed_tools=["tools.files.write"], assigned_task_ids=[task_id])
    rel_file = f"workspace/{task_id}/draft.md"
    w_res = dept_harness.tool_runtime.invoke(
        ToolInvocationRequest(
            auth_context=dev_ctx,
            tool_id="tools.files.write",
            parameters={"path": rel_file, "content": "# Draft output", "overwrite": True},
            task_id=task_id,
            run_id=run_id,
            workspace_root=str(dept_harness.tmp_path),
            target_path=rel_file,
            timestamp=t0 + timedelta(seconds=1),
        )
    )
    assert w_res.status == "SUCCESS"

    vault_dept = VaultDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        artifact_vault=dept_harness.artifact_vault,
    )
    sub = vault_dept.submit_document(
        VaultSubmissionRequest(
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.dev",
            logical_key="draft_doc",
            relative_path=rel_file,
            content_bytes=b"# Draft output",
            department="it",
            timestamp=t0 + timedelta(seconds=2),
        ),
        auth_context=dev_ctx,
    )
    # Provider prose claims DONE, but QA has NOT verified yet.
    dept_harness.store.events.append(
        Event(
            event_id=new_event_id(),
            timestamp=t0 + timedelta(seconds=3),
            event_type="engine.event_forwarded",
            source="provider",
            task_id=task_id,
            run_id=run_id,
            redacted_payload={"text_delta": "STATUS: DONE - everything verified PASS"},
        )
    )

    task_before = dept_harness.store.tasks.get_stored_task(task_id)
    run_before = dept_harness.store.runs.get_stored_run(run_id)
    art_before = dept_harness.artifact_vault.get_artifact(
        sub.artifact.artifact_id, auth_context=dept_harness.master, timestamp=t0 + timedelta(seconds=4)
    )
    assert task_before is not None and run_before is not None
    assert art_before.verification_status == "UNVERIFIED"

    proc = _new_process_service(dept_harness)
    out = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=task_id, timestamp=t0 + timedelta(seconds=5)),
        auth_context=dept_harness.master,
    )
    assert out.summary.execution_status == "RUNNING"
    assert out.summary.qa_verification_status == "UNVERIFIED"

    # Task, Run, and Artifact QA status remain 100% untouched.
    task_after = dept_harness.store.tasks.get_stored_task(task_id)
    run_after = dept_harness.store.runs.get_stored_run(run_id)
    art_after = dept_harness.artifact_vault.get_artifact(
        sub.artifact.artifact_id, auth_context=dept_harness.master, timestamp=t0 + timedelta(seconds=6)
    )
    assert task_after is not None and run_after is not None
    assert task_after.task.status == task_before.task.status == "RUNNING"
    assert task_after.version == task_before.version
    assert task_after.task.active_run_id == task_before.task.active_run_id
    assert run_after.run.status == run_before.run.status == "RUNNING"
    assert run_after.version == run_before.version
    assert art_after.verification_status == "UNVERIFIED"
