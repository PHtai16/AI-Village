"""Unit, Governance, real ToolRuntime, and fail-closed tests for Phase 09 P09.02, P09.03, and P09.04."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError
import pytest

from core.contracts import Run, RunTelemetryRef, Task, new_run_id, new_task_id
from core.departments import (
    DepartmentGovernanceError,
    MarketingDepartmentService,
    MarketingWorkflowRequest,
    QuotaScopeRecord,
    ResourcesDepartmentService,
    ResourcesValidationError,
    ServiceUxDepartmentService,
    ServiceUxValidationError,
)
from core.departments.service_ux import (
    BriefClarificationRequest,
    DeliverableComparisonRequest,
    UserFeedbackRequest,
)
from core.tools.models import ToolUsageSummary
from engines.connections import EngineConnectionRef
from tests.departments.conftest import (
    REPO_ROOT,
    DepartmentTestHarness,
    create_task_and_run,
    register_worker_agent,
    utc,
    worker_ctx,
)


def test_p09_departments_ast_architecture_purity() -> None:
    """Verify core/departments/*.py contains zero forbidden direct SQLite, storage, API, UI, or network imports."""
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
    dept_dir = REPO_ROOT / "core" / "departments"
    py_files = sorted(dept_dir.rglob("*.py"))
    assert len(py_files) >= 4
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), (
                        f"Forbidden import '{alias.name}' in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden), (
                    f"Forbidden import-from '{node.module}' in {py_file}"
                )


def test_p09_02_resources_host_measurement_quota_isolation_and_post_run_inventory(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.02 real OS host read, unavailable null preservation, shared-key rule, and Excel inventory."""
    t0 = utc(9, 0, 0)
    task_id = new_task_id()
    run_id = new_run_id()
    dept_harness.workflows.create_task(
        Task(
            task_id=task_id,
            title="Analyze resource usage",
            description="Run truthful resource tracking",
            requested_by="operator.alice",
            status="RUNNING",
            created_at=t0,
        )
    )
    dept_harness.workflows.create_run(
        Run(
            run_id=run_id,
            task_id=task_id,
            runtime_id="custom.master_worker_v1",
            status="RUNNING",
            started_at=t0,
        )
    )

    res_svc = ResourcesDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        metadata_repo=dept_harness.store.metadata,
        tool_invoker=dept_harness.tool_runtime,
    )

    # 1. Real OS process CPU and RAM sample without fabricated values.
    start_sample = res_svc.measure_host_snapshot(timestamp=t0)
    assert start_sample.source == "os_measured"
    assert start_sample.cpu_seconds is not None and start_sample.cpu_seconds >= 0.0
    if start_sample.rss_memory_bytes is not None:
        assert start_sample.rss_memory_bytes > 0

    # 2. Reject claiming VERIFIED_MEASURED when cost & quota are both missing.
    with pytest.raises(ValidationError):
        QuotaScopeRecord(
            provider_id="google_gemini",
            connection_id="gem_a",
            telemetry_status="VERIFIED_MEASURED",
            observed_at=t0,
        )

    # 3. Never assume two API keys have independent quotas unless verified billing proof exists.
    conn_a = EngineConnectionRef(
        connection_id="gem_key_a",
        provider_id="google_gemini",
        project_ref="prj-billing-1",
        credential_ref="env:GEM_A",
        priority=10,
        default_model_id="gemini-2.5-pro",
    )
    conn_b = EngineConnectionRef(
        connection_id="gem_key_b",
        provider_id="google_gemini",
        project_ref="prj-billing-1",
        credential_ref="env:GEM_B",
        priority=20,
        default_model_id="gemini-2.5-flash",
    )
    shared_check = res_svc.evaluate_connection_quota_isolation(conn_a, conn_b)
    assert shared_check.isolation_status == "QUOTA_SHARED_BY_PROJECT"
    assert shared_check.assumed_independent is False
    assert shared_check.key_rotation_allowed is False

    conn_c = EngineConnectionRef(
        connection_id="openai_key",
        provider_id="openai",
        project_ref="org-alpha",
        credential_ref="env:OAI",
        priority=30,
        quota_scope_note="unknown",
    )
    unproven = res_svc.evaluate_connection_quota_isolation(conn_a, conn_c)
    assert unproven.isolation_status == "QUOTA_INDEPENDENCE_UNPROVEN"
    assert unproven.assumed_independent is False

    # 4. Budget & quota thresholds warn at >=80% and require approval when exceeding budget.
    res_svc.record_quota_scope(
        QuotaScopeRecord(
            provider_id="google_gemini",
            connection_id="gem_key_a",
            project_ref="prj-billing-1",
            model_id="gemini-2.5-pro",
            budget_limit_usd=10.0,
            spent_cost_usd=8.2,
            remaining_quota_units=1200,
            telemetry_status="VERIFIED_MEASURED",
            observed_at=t0 + timedelta(seconds=1),
        ),
        auth_context=dept_harness.master,
        task_id=task_id,
    )
    near_rec = res_svc.recommend_allocation(connections=[conn_a, conn_b], estimated_cost_usd=0.5)
    assert near_rec.status == "WARNING_NEAR_LIMIT"
    assert near_rec.notification_severity == "WARNING"
    assert near_rec.requires_operator_approval is False

    over_rec = res_svc.recommend_allocation(connections=[conn_a], estimated_cost_usd=2.5)
    assert over_rec.status == "BUDGET_OR_QUOTA_EXCEEDED"
    assert over_rec.requires_operator_approval is True

    # Missing scope telemetry keeps UNAVAILABLE_FROM_PROVIDER & None.
    unavail_rec = res_svc.recommend_allocation(connections=[conn_c])
    assert unavail_rec.status == "UNAVAILABLE_FROM_PROVIDER"
    assert unavail_rec.estimated_remaining_budget_usd is None

    # 5. Post-run inventory: preserve None tokens when UNAVAILABLE_FROM_PROVIDER + export real Excel.
    register_worker_agent(
        dept_harness,
        agent_id="resources.analyst",
        department="resources",
        skills=["deterministic_analytics"],
        tools_allowed=["tools.office.write_excel"],
        timestamp=t0 + timedelta(seconds=2),
    )
    res_worker = worker_ctx(
        "resources.analyst",
        allowed_tools=["tools.office.write_excel"],
        assigned_task_ids=[task_id],
    )
    inv_missing_tel = res_svc.compile_post_run_inventory(
        auth_context=res_worker,
        task_id=task_id,
        run_id=run_id,
        provider_id="antigravity",
        connection_id="gem_key_a",
        project_ref="prj-billing-1",
        model_id="gemini-2.5-pro",
        run_telemetry=RunTelemetryRef(
            total_duration_ms=420,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        ),
        tool_usage=ToolUsageSummary(
            task_id=task_id,
            run_id=run_id,
            total_calls=3,
            by_status={"SUCCESS": 3},
            total_duration_ms=110,
            known_cost_usd=None,
            unknown_cost_calls=3,
        ),
        start_host_sample=start_sample,
        budget_limit_usd=10.0,
        export_excel_relpath=f"workspace/{task_id}/resource_inventory.xlsx",
        workspace_root=str(dept_harness.tmp_path),
        timestamp=t0 + timedelta(seconds=5),
    )
    assert inv_missing_tel.input_tokens is None
    assert inv_missing_tel.output_tokens is None
    assert inv_missing_tel.total_tokens is None
    assert inv_missing_tel.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
    assert inv_missing_tel.excel_artifact_path is not None
    assert (dept_harness.workspace_root / task_id / "resource_inventory.xlsx").is_file()

    # Over-budget run persists real RESOURCE_WARNING NotificationRef linked to audit Event.
    inv_over_budget = res_svc.compile_post_run_inventory(
        auth_context=dept_harness.alice,
        task_id=task_id,
        run_id=run_id,
        provider_id="google_gemini",
        connection_id="gem_key_a",
        run_telemetry=RunTelemetryRef(
            input_tokens=1200,
            output_tokens=300,
            total_tokens=1500,
            actual_cost_usd=11.0,
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="provider_api",
        ),
        budget_limit_usd=10.0,
        timestamp=t0 + timedelta(seconds=10),
    )
    assert inv_over_budget.notification_id is not None
    notifs = dept_harness.store.metadata.list_notifications(task_id=task_id, run_id=run_id)
    assert any(n.notification_id == inv_over_budget.notification_id and n.severity == "ERROR" for n in notifs)

    # Unauthorized worker cannot write Excel outside workspace or record connection quota scope.
    with pytest.raises(DepartmentGovernanceError):
        res_svc.record_quota_scope(
            QuotaScopeRecord(provider_id="openai", connection_id="c1", observed_at=t0),
            auth_context=res_worker,
        )
    with pytest.raises(ResourcesValidationError, match="workspace_root"):
        res_svc.compile_post_run_inventory(
            auth_context=dept_harness.alice,
            task_id=task_id,
            run_id=run_id,
            provider_id="openai",
            connection_id="c1",
            export_excel_relpath="workspace/out.xlsx",
            timestamp=t0,
        )


def test_p09_03_service_ux_clarify_feedback_and_compare_deliverable_with_brief(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.03 ambiguity detection, actionable feedback enforcement, and file vs brief check."""
    t0 = utc(10, 0, 0)
    task_id, _ = create_task_and_run(
        dept_harness,
        title="Service UX clarity benchmark",
        description="Verify brief comparison and feedback capture.",
        timestamp=t0,
        assigned_agent_ids=["service.ux_reviewer"],
    )
    register_worker_agent(
        dept_harness,
        agent_id="service.ux_reviewer",
        department="service",
        skills=["file_reading"],
        tools_allowed=["tools.files.read"],
        timestamp=t0,
    )
    svc_ux = ServiceUxDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        tool_invoker=dept_harness.tool_runtime,
    )

    # 1. Ambiguous brief flags missing audience, format, and acceptance criteria.
    ambig = svc_ux.clarify_brief(
        BriefClarificationRequest(
            task_id=task_id,
            goal="Short goal",
            timestamp=t0,
        ),
        auth_context=dept_harness.master,
    )
    assert ambig.needs_clarification is True
    assert set(ambig.missing_fields) == {
        "detailed_goal",
        "target_audience",
        "output_format",
        "acceptance_criteria",
    }
    assert len(ambig.clarification_questions) == 4

    # Clear brief passes without fabricated questions.
    clear = svc_ux.clarify_brief(
        BriefClarificationRequest(
            task_id=task_id,
            goal="Write Q4 cloud latency benchmark summary comparing endpoints cleanly.",
            target_audience="Engineering leadership",
            output_format="markdown",
            acceptance_criteria=[
                "Compare endpoint latency metrics across providers",
                "Include rollback trigger recommendations",
            ],
            timestamp=t0 + timedelta(seconds=2),
        ),
        auth_context=dept_harness.alice,
    )
    assert clear.needs_clarification is False
    assert clear.missing_fields == []

    # 2. Revision feedback requires actionable_changes; accepted feedback stores qa_substitute=False.
    with pytest.raises(ServiceUxValidationError, match="actionable_changes"):
        svc_ux.record_user_feedback(
            UserFeedbackRequest(
                task_id=task_id,
                feedback_kind="REVISION_REQUESTED",
                summary="Need clearer executive summary",
                actionable_changes=[],
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=dept_harness.alice,
        )
    fb = svc_ux.record_user_feedback(
        UserFeedbackRequest(
            task_id=task_id,
            feedback_kind="REVISION_REQUESTED",
            summary="Missing rollback triggers section",
            actionable_changes=["Add explicit rollback trigger table"],
            timestamp=t0 + timedelta(seconds=4),
        ),
        auth_context=dept_harness.alice,
    )
    assert fb.qa_substitute is False
    assert len(svc_ux.list_feedback(task_id=task_id)) == 1

    # 3. Real deliverable comparison via tools.files.read inside task workspace.
    target_rel = f"workspace/{task_id}/benchmark_summary.md"
    report_file = dept_harness.tmp_path / target_rel
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(
        "# Latency Summary\nThis analysis compares endpoint latency metrics across providers.\n",
        encoding="utf-8",
    )
    ux_worker = worker_ctx(
        "service.ux_reviewer",
        allowed_tools=["tools.files.read"],
        assigned_task_ids=[task_id],
    )
    cmp_out = svc_ux.compare_deliverable_against_brief(
        DeliverableComparisonRequest(
            task_id=task_id,
            brief_criteria=[
                "Compare endpoint latency metrics across providers",
                "Include rollback trigger recommendations",
            ],
            deliverable_relpath=target_rel,
            workspace_root=str(dept_harness.tmp_path),
            timestamp=t0 + timedelta(seconds=5),
        ),
        auth_context=ux_worker,
    )
    assert cmp_out.qa_substitute is False
    assert cmp_out.met_count == 1
    assert cmp_out.unmet_count == 1
    assert cmp_out.ux_readiness == "NEEDS_REVISION"

    # After adding missing section, readiness updates truthfully without replacing QA.
    report_file.write_text(
        report_file.read_text(encoding="utf-8")
        + "Rollback trigger recommendations: revert immediately on p95 spike.\n",
        encoding="utf-8",
    )
    cmp_ready = svc_ux.compare_deliverable_against_brief(
        DeliverableComparisonRequest(
            task_id=task_id,
            brief_criteria=[
                "Compare endpoint latency metrics across providers",
                "Include rollback trigger recommendations",
            ],
            deliverable_relpath=target_rel,
            workspace_root=str(dept_harness.tmp_path),
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=ux_worker,
    )
    assert cmp_ready.met_count == 2
    assert cmp_ready.unmet_count == 0
    assert cmp_ready.ux_readiness == "READY_FOR_QA_OR_DELIVERY"


def test_p09_04_marketing_dynamic_non_hardcoded_research_plan_and_real_files(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.04 runs dynamic research, non-hardcoded planning, and real Markdown + DOCX output."""
    t0 = utc(11, 0, 0)
    mkt = MarketingDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        tool_invoker=dept_harness.tool_runtime,
    )

    task_md, _ = create_task_and_run(
        dept_harness,
        title="Marketing MD Campaign",
        description="Produce blog campaign from web search.",
        timestamp=t0,
        assigned_agent_ids=["marketing.strategist"],
    )
    task_docx, _ = create_task_and_run(
        dept_harness,
        title="Marketing DOCX Campaign",
        description="Produce email campaign DOCX.",
        timestamp=t0,
        assigned_agent_ids=["marketing.strategist"],
    )
    register_worker_agent(
        dept_harness,
        agent_id="marketing.strategist",
        department="marketing",
        skills=["web_research", "file_writing", "office_writing"],
        tools_allowed=[
            "tools.research.web_search",
            "tools.research.web_fetch",
            "tools.files.write",
            "tools.office.write_docx",
        ],
        timestamp=t0,
    )
    mkt_worker = worker_ctx(
        "marketing.strategist",
        allowed_tools=[
            "tools.research.web_search",
            "tools.research.web_fetch",
            "tools.files.write",
            "tools.office.write_docx",
        ],
        assigned_task_ids=[task_md, task_docx],
    )

    # Task A: Real web search + web fetch -> Markdown deliverable.
    out_a = mkt.run_campaign_workflow(
        MarketingWorkflowRequest(
            task_id=task_md,
            topic="Edge AI Telemetry Benchmark Launch",
            target_audience="Platform Engineering Teams",
            channel="blog",
            tone="Technical concise",
            objectives=["Highlight verified latency drops", "Drive sandbox trial adoption"],
            search_queries=["edge telemetry benchmark"],
            research_urls=["https://example.org/release-notes"],
            output_relpath=f"workspace/{task_md}/edge_telemetry_blog.md",
            output_format="markdown",
            workspace_root=str(dept_harness.tmp_path),
            timestamp=t0,
        ),
        auth_context=mkt_worker,
    )
    md_disk = dept_harness.tmp_path / out_a.output_relpath
    assert md_disk.is_file()
    md_text = md_disk.read_text(encoding="utf-8")
    assert "Edge AI Telemetry Benchmark Launch" in md_text
    assert all(c.content_trust_level == "UNTRUSTED_EXTERNAL_DATA" for c in out_a.citations)
    assert len(out_a.citations) >= 2
    assert len(out_a.evidence_refs) >= 1
    assert out_a.business_verification_status == "UNVERIFIED"

    # Task B: Different topic & channel -> produces distinct plan/KPIs and real DOCX artifact!
    out_b = mkt.run_campaign_workflow(
        MarketingWorkflowRequest(
            task_id=task_docx,
            topic="Zero-Trust Secret Hygiene Campaign",
            target_audience="Security Operations Leads",
            channel="email_campaign",
            tone="Authoritative advisory",
            objectives=["Enforce credential reference discipline"],
            output_relpath=f"workspace/{task_docx}/security_campaign.docx",
            output_format="docx",
            workspace_root=str(dept_harness.tmp_path),
            timestamp=t0 + timedelta(seconds=10),
        ),
        auth_context=mkt_worker,
    )
    docx_disk = dept_harness.tmp_path / out_b.output_relpath
    assert docx_disk.is_file() and docx_disk.stat().st_size > 100
    assert out_a.plan.kpis != out_b.plan.kpis
    assert out_a.plan.planned_deliverables != out_b.plan.planned_deliverables
    assert out_b.plan.research_gaps == [
        "NO_EXTERNAL_RESEARCH_EXECUTED: Draft strictly avoids unverified external statistics."
    ]
    assert out_a.output_sha256 != out_b.output_sha256

    # Path traversal outside workspace blocked fail-closed.
    with pytest.raises(ValidationError):
        MarketingWorkflowRequest(
            task_id=task_md,
            topic="Bad path campaign",
            target_audience="Ops",
            channel="social",
            tone="Direct",
            objectives=["Test path guard"],
            output_relpath="../escape.md",
            workspace_root=str(dept_harness.tmp_path),
            timestamp=t0,
        )
