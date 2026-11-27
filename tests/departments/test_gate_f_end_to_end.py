"""Gate F End-to-End Acceptance Suite: 3 distinct real deliverables via dynamic subsets without fixed pipelines."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from core.contracts import RunTelemetryRef
from core.departments import (
    HrDepartmentService,
    ItRdDepartmentService,
    MarketingDepartmentService,
    MarketingWorkflowRequest,
    ProcessDepartmentService,
    QaDepartmentService,
    QaVerificationRequest,
    ResourcesDepartmentService,
    SandboxTestExecutionRequest,
    ServiceUxDepartmentService,
    TaskRetrospectiveInput,
    VaultDepartmentService,
    VaultReuseRequest,
    VaultSubmissionRequest,
    WorkspaceCodeAuthoringRequest,
    WorkspaceFileSpec,
)
from core.departments.service_ux import DeliverableComparisonRequest
from core.orchestration import (
    CollaborationDeliveryService,
    DynamicTeamBuilderService,
    MasterDeliveryCompileRequest,
    MasterPlanningService,
    PlanStepSpec,
    StructuredUserBrief,
    TeamBuildRequest,
)
from core.tools.models import ToolInvocationRequest
from tools.code import CommandRunResult
from tests.departments.conftest import (
    DepartmentTestHarness,
    create_task_and_run,
    make_department_harness,
    register_worker_agent,
    utc,
    worker_ctx,
)


def _setup_orchestration(h: DepartmentTestHarness) -> tuple[MasterPlanningService, DynamicTeamBuilderService, CollaborationDeliveryService]:
    planner = MasterPlanningService(
        plan_repo=h.store.plans,
        agent_registry=h.agent_registry,
        permission_manager=h.permissions,
        workspace_root=h.tmp_path,
        registered_tools=sorted(m.id for m in h.tool_registry.list_manifests()),
    )
    builder = DynamicTeamBuilderService(
        team_repo=h.store.teams,
        plan_repo=h.store.plans,
        agent_registry=h.agent_registry,
        permission_manager=h.permissions,
        registered_tools=sorted(m.id for m in h.tool_registry.list_manifests()),
    )
    delivery = CollaborationDeliveryService(
        collaboration_repo=h.store.collaboration,
        handoff_repo=h.store.handoffs,
        task_repo=h.store.tasks,
        run_repo=h.store.runs,
        metadata_repo=h.store.metadata,
        permission_manager=h.permissions,
        artifact_vault=h.artifact_vault,
    )
    return planner, builder, delivery


def test_gate_f_three_distinct_real_tasks_with_dynamic_teams_and_verified_deliverables(
    tmp_path: Path,
) -> None:
    """Gate F: execute Code, Research, and Office tasks via distinct dynamic squads with real VERIFIED outputs."""
    h = make_department_harness(
        tmp_path,
        sandbox_results={
            "test_tax_calc.py": CommandRunResult(
                exit_code=0, stdout="3 passed in 0.04s", stderr="", duration_ms=40
            )
        },
        fetched_html="<html><body><h1>Enterprise AI Adoption 2026</h1><p>Audit-verified pipelines grow 3x.</p></body></html>",
    )
    t0 = utc(14, 0, 0)
    try:
        planner, builder, delivery = _setup_orchestration(h)
        depts_root = Path(__file__).resolve().parents[2] / "agents" / "departments"
        hr = HrDepartmentService(registry=h.agent_registry, departments_root=depts_root)
        resources = ResourcesDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            metadata_repo=h.store.metadata,
            tool_invoker=h.tool_runtime,
        )
        service_ux = ServiceUxDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            tool_invoker=h.tool_runtime,
        )
        marketing = MarketingDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            tool_invoker=h.tool_runtime,
        )
        it_rd = ItRdDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            tool_invoker=h.tool_runtime,
        )
        process = ProcessDepartmentService(permission_manager=h.permissions, journal=h.store.events)
        qa = QaDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            evidence_vault=h.evidence_vault,
            artifact_vault=h.artifact_vault,
            tool_invoker=h.tool_runtime,
        )
        vault_dept = VaultDepartmentService(
            permission_manager=h.permissions,
            journal=h.store.events,
            artifact_vault=h.artifact_vault,
            knowledge_vault=h.evidence_vault,
        )

        # Register diverse ACTIVE workers across distinct departments (Master will only mobilize needed subsets).
        register_worker_agent(
            h,
            agent_id="it.python_dev",
            department="it",
            skills=["code_execution", "automated_testing", "file_writing"],
            tools_allowed=["tools.files.write", "tools.code.run_sandbox"],
            timestamp=t0,
        )
        register_worker_agent(
            h,
            agent_id="marketing.analyst",
            department="marketing",
            skills=["web_research", "content_planning"],
            tools_allowed=["tools.research.web_search", "tools.research.web_fetch", "tools.files.write"],
            timestamp=t0,
        )
        register_worker_agent(
            h,
            agent_id="resources.fin_ops",
            department="resources",
            skills=["deterministic_analytics", "xlsx_processing"],
            tools_allowed=["tools.office.compute_table", "tools.office.write_excel"],
            timestamp=t0,
        )
        register_worker_agent(
            h,
            agent_id="service.doc_editor",
            department="service",
            skills=["docx_authoring", "brief_alignment"],
            tools_allowed=["tools.office.write_docx", "tools.files.read"],
            timestamp=t0,
        )
        register_worker_agent(
            h,
            agent_id="qa.auditor",
            department="qa",
            skills=["artifact_qa_inspection"],
            tools_allowed=["tools.files.read"],
            timestamp=t0,
        )
        register_worker_agent(
            h,
            agent_id="vault.librarian",
            department="vault",
            skills=["artifact_archival"],
            tools_allowed=[],
            timestamp=t0,
        )
        assert len(hr.export_explorer_catalog().agents) == 6

        # =====================================================================
        # TASK 1 (CODE): IT + QA subset ONLY -> Python module + sandbox pytest
        # =====================================================================
        t_code, r_code = create_task_and_run(
            h,
            title="Implement tax calculation utility",
            description="Write tax_calc.py + pytest suite in isolated workspace.",
            timestamp=t0 + timedelta(seconds=10),
            task_status="QUEUED",
        )
        code_plan = planner.create_plan(
            brief=StructuredUserBrief(
                task_id=t_code,
                title="Tax calc utility",
                objective="Create tax calculation module and run isolated unit tests.",
                requested_by="operator.alice",
                acceptance_criteria=["pytest passes cleanly in sandbox"],
            ),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=11),
            steps=[
                PlanStepSpec(
                    step_id="step_code_impl",
                    order=0,
                    title="Implement tax calc & tests",
                    description="Author isolated Python files",
                    required_capabilities=["code_execution", "automated_testing"],
                    expected_outputs=[f"workspace/{t_code}/tax_calc.py"],
                    allowed_write_paths=[f"workspace/{t_code}/tax_calc.py"],
                    acceptance_criteria=["tax_calc.py saved"],
                ),
                PlanStepSpec(
                    step_id="step_code_qa",
                    order=1,
                    title="Independent QA inspection",
                    description="Verify sandbox proof and hash",
                    depends_on=["step_code_impl"],
                    required_capabilities=["artifact_qa_inspection"],
                    expected_outputs=[f"workspace/{t_code}/qa_code.json"],
                    allowed_write_paths=[f"workspace/{t_code}/qa_code.json"],
                    acceptance_criteria=["QA PASS with evd_*"],
                ),
            ],
        )
        assert code_plan.stored_plan is not None
        code_applied, _ = planner.apply_plan_to_task(
            code_plan.stored_plan.plan_id,
            expected_plan_record_version=code_plan.stored_plan.record_version,
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=12),
        )
        code_team = builder.build_team_proposal(
            TeamBuildRequest(task_id=t_code, plan_id=code_applied.plan_id),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=13),
        )
        code_agents = {m.agent_id for m in code_team.proposal.pinned_members}
        assert code_agents == {"it.python_dev", "qa.auditor"}

        it_actor = worker_ctx(
            "it.python_dev",
            allowed_tools=["tools.files.write", "tools.code.run_sandbox"],
            assigned_task_ids=[t_code],
        )
        written_code = it_rd.write_isolated_files(
            WorkspaceCodeAuthoringRequest(
                task_id=t_code,
                run_id=r_code,
                workspace_root=str(h.tmp_path),
                files=[
                    WorkspaceFileSpec(
                        relative_path=f"workspace/{t_code}/tax_calc.py",
                        content="def net_amount(gross: float, rate: float) -> float:\n    return round(gross * (1.0 - rate), 2)\n",
                    ),
                    WorkspaceFileSpec(
                        relative_path=f"workspace/{t_code}/test_tax_calc.py",
                        content="from tax_calc import net_amount\n\ndef test_net():\n    assert net_amount(100.0, 0.1) == 90.0\n",
                    ),
                ],
                timestamp=t0 + timedelta(seconds=14),
            ),
            auth_context=it_actor,
        )
        sandbox_res = it_rd.run_sandbox_tests(
            SandboxTestExecutionRequest(
                task_id=t_code,
                run_id=r_code,
                workspace_root=str(h.tmp_path),
                operation="pytest",
                target_relpath=f"workspace/{t_code}/test_tax_calc.py",
                timestamp=t0 + timedelta(seconds=15),
            ),
            auth_context=it_actor,
        )
        code_sub = vault_dept.submit_document(
            VaultSubmissionRequest(
                task_id=t_code,
                run_id=r_code,
                owner_agent_id="it.python_dev",
                logical_key="gate_f_tax_calc",
                relative_path=f"workspace/{t_code}/tax_calc.py",
                content_bytes=(h.tmp_path / f"workspace/{t_code}/tax_calc.py").read_bytes(),
                department="it",
                timestamp=t0 + timedelta(seconds=16),
            ),
            auth_context=it_actor,
        )
        qa_code = qa.verify_deliverables(
            QaVerificationRequest(
                task_id=t_code,
                run_id=r_code,
                producer_agent_id="it.python_dev",
                artifact_id=code_sub.artifact.artifact_id,
                expected_sha256=written_code.written_files[0].sha256,
                sandbox_passed=sandbox_res.sandbox_passed,
                verified_checks=["sandbox_pytest_passed"],
                require_pass_verdict=True,
                summary="Tax module hash and sandbox pytest independently verified.",
                timestamp=t0 + timedelta(seconds=17),
            ),
            auth_context=h.qa_verifier,
        )
        assert qa_code.verification_status == "VERIFIED_PASS"
        deliv_code = delivery.compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=t_code,
                run_id=r_code,
                workspace_root=str(h.tmp_path),
                artifact_ids=[code_sub.artifact.artifact_id],
                summary="Delivered verified Python tax_calc module.",
                timestamp=t0 + timedelta(seconds=18),
            )
        )
        assert deliv_code.delivery_verification_status == "VERIFIED"

        # =====================================================================
        # TASK 2 (RESEARCH): Marketing + QA subset ONLY -> Markdown report
        # =====================================================================
        t_res, r_res = create_task_and_run(
            h,
            title="Research 2026 Enterprise AI Adoption",
            description="Fetch public source, plan briefing, and write Markdown deliverable.",
            timestamp=t0 + timedelta(seconds=30),
            task_status="QUEUED",
        )
        res_plan = planner.create_plan(
            brief=StructuredUserBrief(
                task_id=t_res,
                title="AI Adoption Research Brief",
                objective="Research Enterprise AI Adoption trends and publish verified Markdown report.",
                requested_by="operator.alice",
                acceptance_criteria=["Markdown report saved with cited source"],
            ),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=31),
            steps=[
                PlanStepSpec(
                    step_id="step_res_brief",
                    order=0,
                    title="Execute web research and write report",
                    description="Use marketing tools",
                    required_capabilities=["web_research", "content_planning"],
                    expected_outputs=[f"workspace/{t_res}/ai_adoption_brief.md"],
                    allowed_write_paths=[f"workspace/{t_res}/ai_adoption_brief.md"],
                    acceptance_criteria=["ai_adoption_brief.md generated"],
                ),
                PlanStepSpec(
                    step_id="step_res_qa",
                    order=1,
                    title="QA verify research evidence",
                    description="Check source evidence and output hash",
                    depends_on=["step_res_brief"],
                    required_capabilities=["artifact_qa_inspection"],
                    expected_outputs=[f"workspace/{t_res}/qa_res.json"],
                    allowed_write_paths=[f"workspace/{t_res}/qa_res.json"],
                    acceptance_criteria=["QA PASS verified"],
                ),
            ],
        )
        assert res_plan.stored_plan is not None
        res_applied, _ = planner.apply_plan_to_task(
            res_plan.stored_plan.plan_id,
            expected_plan_record_version=res_plan.stored_plan.record_version,
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=32),
        )
        res_team = builder.build_team_proposal(
            TeamBuildRequest(task_id=t_res, plan_id=res_applied.plan_id),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=33),
        )
        res_agents = {m.agent_id for m in res_team.proposal.pinned_members}
        assert res_agents == {"marketing.analyst", "qa.auditor"}

        mkt_actor = worker_ctx(
            "marketing.analyst",
            allowed_tools=["tools.research.web_search", "tools.research.web_fetch", "tools.files.write"],
            assigned_task_ids=[t_res],
        )
        mkt_out = marketing.run_campaign_workflow(
            MarketingWorkflowRequest(
                task_id=t_res,
                run_id=r_res,
                topic="Enterprise AI Adoption 2026 Briefing",
                target_audience="CTO & VP Engineering",
                channel="blog",
                tone="Executive factual",
                objectives=["Summarize verified adoption trend", "Cite external source URL"],
                search_queries=["enterprise ai adoption 2026"],
                research_urls=["https://example.org/ai-adoption-2026"],
                output_relpath=f"workspace/{t_res}/ai_adoption_brief.md",
                output_format="markdown",
                workspace_root=str(h.tmp_path),
                timestamp=t0 + timedelta(seconds=34),
            ),
            auth_context=mkt_actor,
        )
        res_sub = vault_dept.submit_document(
            VaultSubmissionRequest(
                task_id=t_res,
                run_id=r_res,
                owner_agent_id="marketing.analyst",
                logical_key="gate_f_ai_adoption_brief",
                relative_path=mkt_out.output_relpath,
                content_bytes=(h.tmp_path / mkt_out.output_relpath).read_bytes(),
                department="marketing",
                access_scope="project",
                timestamp=t0 + timedelta(seconds=35),
            ),
            auth_context=mkt_actor,
        )
        qa_res = qa.verify_deliverables(
            QaVerificationRequest(
                task_id=t_res,
                run_id=r_res,
                producer_agent_id="marketing.analyst",
                artifact_id=res_sub.artifact.artifact_id,
                expected_sha256=mkt_out.output_sha256,
                verified_checks=["untrusted_source_boundary_labeled", "sha256_verified"],
                existing_evidence_ids=mkt_out.evidence_refs,
                require_pass_verdict=True,
                summary="Research Markdown file & external source citations verified.",
                timestamp=t0 + timedelta(seconds=36),
            ),
            auth_context=h.qa_verifier,
        )
        assert qa_res.verification_status == "VERIFIED_PASS"
        deliv_res = delivery.compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=t_res,
                run_id=r_res,
                workspace_root=str(h.tmp_path),
                artifact_ids=[res_sub.artifact.artifact_id],
                summary="Delivered verified Enterprise AI Adoption Markdown briefing.",
                timestamp=t0 + timedelta(seconds=37),
            )
        )
        assert deliv_res.delivery_verification_status == "VERIFIED"

        # =====================================================================
        # TASK 3 (OFFICE): Resources + Service + Vault + QA subset -> XLSX + DOCX + Reuse
        # =====================================================================
        t_off, r_off = create_task_and_run(
            h,
            title="Compile Q4 Budget Workbook and Executive DOCX",
            description="Compute budget summary Excel and DOCX narrative reusing Research brief.",
            timestamp=t0 + timedelta(seconds=50),
            task_status="QUEUED",
        )
        off_plan = planner.create_plan(
            brief=StructuredUserBrief(
                task_id=t_off,
                title="Q4 Office Budget Package",
                objective="Compute Q4 spend Excel and compile executive DOCX report.",
                requested_by="operator.alice",
                acceptance_criteria=["Q4 Excel + DOCX verified by QA"],
            ),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=51),
            steps=[
                PlanStepSpec(
                    step_id="step_off_xlsx",
                    order=0,
                    title="Build deterministic Q4 budget XLSX",
                    description="Compute and export workbook",
                    required_capabilities=["deterministic_analytics", "xlsx_processing"],
                    expected_outputs=[f"workspace/{t_off}/q4_budget.xlsx"],
                    allowed_write_paths=[f"workspace/{t_off}/q4_budget.xlsx"],
                    acceptance_criteria=["q4_budget.xlsx created"],
                ),
                PlanStepSpec(
                    step_id="step_off_docx",
                    order=1,
                    title="Compile DOCX brief alignment",
                    description="Author executive DOCX & check brief",
                    depends_on=["step_off_xlsx"],
                    required_capabilities=["docx_authoring", "brief_alignment"],
                    expected_outputs=[f"workspace/{t_off}/q4_executive.docx"],
                    allowed_write_paths=[f"workspace/{t_off}/q4_executive.docx"],
                    acceptance_criteria=["q4_executive.docx aligns with brief"],
                ),
                PlanStepSpec(
                    step_id="step_off_qa",
                    order=2,
                    title="QA verify Office deliverables",
                    description="Inspect Excel and DOCX hashes",
                    depends_on=["step_off_docx"],
                    required_capabilities=["artifact_qa_inspection"],
                    expected_outputs=[f"workspace/{t_off}/qa_off.json"],
                    allowed_write_paths=[f"workspace/{t_off}/qa_off.json"],
                    acceptance_criteria=["QA PASS verified"],
                ),
            ],
        )
        assert off_plan.stored_plan is not None
        off_applied, _ = planner.apply_plan_to_task(
            off_plan.stored_plan.plan_id,
            expected_plan_record_version=off_plan.stored_plan.record_version,
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=52),
        )
        off_team = builder.build_team_proposal(
            TeamBuildRequest(task_id=t_off, plan_id=off_applied.plan_id),
            auth_context=h.master,
            timestamp=t0 + timedelta(seconds=53),
        )
        off_agents = {m.agent_id for m in off_team.proposal.pinned_members}
        assert off_agents == {"resources.fin_ops", "service.doc_editor", "qa.auditor"}

        # 3 distinct task briefs produced 3 distinct agent squads!
        assert len({frozenset(code_agents), frozenset(res_agents), frozenset(off_agents)}) == 3

        fin_actor = worker_ctx(
            "resources.fin_ops",
            allowed_tools=["tools.office.compute_table", "tools.office.write_excel"],
            assigned_task_ids=[t_off],
        )
        doc_actor = worker_ctx(
            "service.doc_editor",
            allowed_tools=["tools.office.write_docx", "tools.files.read"],
            assigned_task_ids=[t_off],
        )
        vault_actor = worker_ctx("vault.librarian", assigned_task_ids=[t_res, t_off])

        # Vault safely reuses Task 2's research artifact inside Task 3!
        reused_brief = vault_dept.reuse_artifact_in_task(
            VaultReuseRequest(
                artifact_id=res_sub.artifact.artifact_id,
                target_task_id=t_off,
                target_run_id=r_off,
                timestamp=t0 + timedelta(seconds=54),
            ),
            auth_context=vault_actor,
        )
        assert b"Enterprise AI Adoption 2026 Briefing" in reused_brief.content_bytes

        # Resources runs real compute_table + write_excel + post-run inventory cleanly.
        calc_out = h.tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=fin_actor,
                tool_id="tools.office.compute_table",
                parameters={
                    "rows": [
                        {"dept": "IT", "cost": 120.0},
                        {"dept": "Marketing", "cost": 80.0},
                    ],
                    "group_by": "dept",
                    "metrics": [{"column": "cost", "op": "sum"}],
                },
                task_id=t_off,
                run_id=r_off,
                timestamp=t0 + timedelta(seconds=55),
            )
        )
        assert calc_out.status == "SUCCESS"
        xlsx_rel = f"workspace/{t_off}/q4_budget.xlsx"
        inv_off = resources.compile_post_run_inventory(
            auth_context=fin_actor,
            task_id=t_off,
            run_id=r_off,
            provider_id="antigravity",
            connection_id="default_antigravity",
            run_telemetry=RunTelemetryRef(telemetry_status="UNAVAILABLE_FROM_PROVIDER", telemetry_source="unavailable"),
            tool_usage=h.store.tools.summarize_usage(task_id=t_off, run_id=r_off),
            export_excel_relpath=xlsx_rel,
            workspace_root=str(h.tmp_path),
            timestamp=t0 + timedelta(seconds=56),
        )
        assert inv_off.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
        assert (h.tmp_path / xlsx_rel).is_file()

        docx_rel = f"workspace/{t_off}/q4_executive.docx"
        docx_res = h.tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=doc_actor,
                tool_id="tools.office.write_docx",
                parameters={
                    "path": docx_rel,
                    "title": "Q4 Executive Budget & Adoption Report",
                    "paragraphs": [
                        "Total tracked compute budget: 200.00 USD across IT and Marketing.",
                        "Referenced Enterprise AI Adoption briefing cleanly from Vault.",
                    ],
                    "overwrite": True,
                },
                task_id=t_off,
                run_id=r_off,
                workspace_root=str(h.tmp_path),
                target_path=docx_rel,
                timestamp=t0 + timedelta(seconds=57),
            )
        )
        assert docx_res.status == "SUCCESS"
        cmp_off = service_ux.compare_deliverable_against_brief(
            DeliverableComparisonRequest(
                task_id=t_off,
                run_id=r_off,
                brief_criteria=["Enterprise AI Adoption briefing"],
                deliverable_text="Q4 Executive Budget & Adoption Report citing Enterprise AI Adoption briefing cleanly.",
                timestamp=t0 + timedelta(seconds=58),
            ),
            auth_context=doc_actor,
        )
        assert cmp_off.ux_readiness == "READY_FOR_QA_OR_DELIVERY"

        xlsx_sub = vault_dept.submit_document(
            VaultSubmissionRequest(
                task_id=t_off,
                run_id=r_off,
                owner_agent_id="resources.fin_ops",
                logical_key="gate_f_q4_budget",
                relative_path=xlsx_rel,
                content_bytes=(h.tmp_path / xlsx_rel).read_bytes(),
                department="resources",
                timestamp=t0 + timedelta(seconds=59),
            ),
            auth_context=fin_actor,
        )
        docx_sub = vault_dept.submit_document(
            VaultSubmissionRequest(
                task_id=t_off,
                run_id=r_off,
                owner_agent_id="service.doc_editor",
                logical_key="gate_f_q4_executive",
                relative_path=docx_rel,
                content_bytes=(h.tmp_path / docx_rel).read_bytes(),
                department="service",
                timestamp=t0 + timedelta(seconds=60),
            ),
            auth_context=doc_actor,
        )
        qa.verify_deliverables(
            QaVerificationRequest(
                task_id=t_off,
                run_id=r_off,
                producer_agent_id="resources.fin_ops",
                artifact_id=xlsx_sub.artifact.artifact_id,
                verified_checks=["xlsx_deterministic_sum_verified"],
                require_pass_verdict=True,
                summary="Verified Q4 Excel budget workbook.",
                timestamp=t0 + timedelta(seconds=61),
            ),
            auth_context=h.qa_verifier,
        )
        qa.verify_deliverables(
            QaVerificationRequest(
                task_id=t_off,
                run_id=r_off,
                producer_agent_id="service.doc_editor",
                artifact_id=docx_sub.artifact.artifact_id,
                verified_checks=["docx_integrity_and_brief_match"],
                require_pass_verdict=True,
                summary="Verified Q4 executive DOCX report.",
                timestamp=t0 + timedelta(seconds=62),
            ),
            auth_context=h.qa_verifier,
        )
        deliv_off = delivery.compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=t_off,
                run_id=r_off,
                workspace_root=str(h.tmp_path),
                artifact_ids=[xlsx_sub.artifact.artifact_id, docx_sub.artifact.artifact_id],
                summary="Delivered verified Q4 Excel workbook and Executive DOCX report.",
                timestamp=t0 + timedelta(seconds=63),
            )
        )
        assert deliv_off.delivery_verification_status == "VERIFIED"

        # Process captures retrospective on Office run cleanly without auto-activating skills.
        retro = process.record_task_retrospective(
            TaskRetrospectiveInput(
                task_id=t_off,
                run_id=r_off,
                departments_involved=["resources", "service", "qa", "vault"],
                tool_executions=h.store.tools.list_executions(task_id=t_off),
                duration_ms=250,
                timestamp=t0 + timedelta(seconds=64),
            ),
            auth_context=h.master,
        )
        assert "tools.office.write_excel" in retro.tools_used
    finally:
        h.store.close()
