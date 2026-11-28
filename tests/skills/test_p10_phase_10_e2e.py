"""End-to-end Method C Continuous Improvement test spanning P10.01 through P10.05."""

from __future__ import annotations

from datetime import timedelta
import hashlib

import pytest

from core.agents.hr import HrDepartmentService
from core.agents.hr_models import SkillGrantRequest
from core.contracts import Event, new_event_id
from core.departments import (
    CompileTaskRetrospectiveRequest,
    PatternAnalysisRequest,
    ProcessDepartmentService,
)
from core.governance.manager import PermissionDeniedError
from core.governance.models import ApprovalPolicyContext
from core.skills import (
    ActivateSkillRequest,
    ItTechnicalReviewRequest,
    ProposeSkillActivationRequest,
    QaSkillBenchmarkRequest,
    RecordImprovementOutcomeRequest,
    SkillBenchmarkCaseOutcome,
)
from core.tools.models import ToolExecutionRecord
from tests.departments.conftest import (
    REPO_ROOT,
    create_task_and_run,
    register_worker_agent,
    utc,
    worker_ctx,
)
from tests.skills.conftest import SkillFactoryHarness, make_candidate_request


def test_phase_10_complete_method_c_continuous_improvement_loop(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Walk full Method C loop: P10.01 retros -> P10.02 pattern -> P10.03 skill -> P10.04 QA -> P10.05 deploy + HR + rollback."""
    h = skill_harness.dept
    t0 = utc(9, 0, 0)
    register_worker_agent(
        h,
        agent_id="it.specialist",
        department="it",
        skills=["invoice_table_cleanup"],
        tools_allowed=["tools.files.read", "tools.files.write"],
        timestamp=t0,
    )
    proc = ProcessDepartmentService(
        permission_manager=h.permissions,
        journal=h.store.events,
        retrospective_repo=h.store.process,
        skill_catalog=skill_harness.skills,
    )

    # Step 1-2 (P10.01): 3 real historical Tasks fail repeatedly before recovery on same pattern.
    baseline_task_ids: list[str] = []
    for i in range(3):
        tid, rid = create_task_and_run(
            h,
            title=f"Invoice table cleanup #{i + 1}",
            description="Repeated manual BOM + header normalization error",
            assigned_agent_ids=["it.specialist"],
            timestamp=t0 + timedelta(seconds=i * 20),
        )
        h.store.tools.record_execution(
            ToolExecutionRecord(
                execution_id=f"tex_m_fail_{i}",
                tool_id="tools.files.read",
                task_id=tid,
                run_id=rid,
                actor_id="it.specialist",
                action="files.read",
                status="FAILED",
                error_code="HEADER_BOM_MISMATCH",
                error_message="BOM header parse error",
                duration_ms=60,
                executed_at=t0 + timedelta(seconds=i * 20 + 1),
            )
        )
        h.store.tools.record_execution(
            ToolExecutionRecord(
                execution_id=f"tex_m_retry_{i}",
                tool_id="tools.files.read",
                task_id=tid,
                run_id=rid,
                actor_id="it.specialist",
                action="files.read",
                status="SUCCESS",
                duration_ms=40,
                executed_at=t0 + timedelta(seconds=i * 20 + 2),
            )
        )
        retro = proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(
                task_id=tid,
                pattern_tag="invoice_table_cleanup",
                timestamp=t0 + timedelta(seconds=i * 20 + 5),
            ),
            auth_context=h.master,
        )
        assert retro.summary.skill_usage_status == "DECLARED_OR_ALLOWED_ONLY_NOT_INVOKED"
        baseline_task_ids.append(tid)

    # Step 3 (P10.02): Process mines persistent pattern suggestion across the 3 Tasks.
    scan = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(min_occurrences=3, timestamp=t0 + timedelta(minutes=2)),
        auth_context=h.master,
    )
    gap_imp = next(
        imp for imp in scan.created_improvements if imp.pattern_type == "missing_skill_gap"
    )
    assert gap_imp.suggested_skill_id == "invoice_table_cleanup"
    assert gap_imp.metrics.occurrence_count == 3

    # Step 4 (P10.03): Draft & register Skill Candidate v1.0.0 linked to improvement.
    cand = skill_harness.skills.create_candidate(
        make_candidate_request(
            skill_id="invoice_table_cleanup",
            version="1.0.0",
            tools=["tools.files.read", "tools.files.write"],
            scripts={"clean_table.py": "def strip_bom(txt: str) -> str:\n    return txt.lstrip('\\ufeff')\n"},
            source_improvement_id=gap_imp.improvement_id,
            risk_level="medium",
            minute_offset=3,
        ),
        auth_context=h.master,
    )
    assert cand.lifecycle_status == "CANDIDATE"
    linked_imp = proc.get_suggested_improvement(gap_imp.improvement_id, auth_context=h.master)
    assert linked_imp is not None and linked_imp.status == "CANDIDATE_CREATED"

    # Step 5 (P10.04): IT technical review + independent QA A/B benchmark PASS.
    skill_harness.skills.record_it_technical_review(
        ItTechnicalReviewRequest(
            skill_id="invoice_table_cleanup",
            version="1.0.0",
            sandbox_passed=True,
            sandbox_stdout="clean_table.py passed 2 sandbox fixture checks.",
            timestamp=t0 + timedelta(minutes=4),
        ),
        auth_context=worker_ctx("it.specialist"),
    )
    sha = hashlib.sha256(b"invoice_table_cleanup@1.0.0").hexdigest()
    bm, _ = skill_harness.skills.run_qa_benchmark(
        QaSkillBenchmarkRequest(
            skill_id="invoice_table_cleanup",
            version="1.0.0",
            task_id=baseline_task_ids[-1],
            explicit_outcomes=[
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=100,
                    tool_calls_count=4,
                    error_count=1,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm="B_SKILL",
                    passed=True,
                    duration_ms=30,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_fail_missing_file",
                    case_kind="failure_case",
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=20,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_fail_missing_file",
                    case_kind="failure_case",
                    arm="B_SKILL",
                    passed=True,
                    duration_ms=10,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                ),
            ],
            summary="70% faster execution and 0 errors under Skill arm B.",
            timestamp=t0 + timedelta(minutes=5),
        ),
        auth_context=h.qa_verifier,
    )
    assert bm.benchmark_status == "PASSED"

    # Step 6-7 (P10.05): Master proposes activation -> Human Operator approves -> Skill promoted!
    prop = skill_harness.skills.propose_skill_activation(
        ProposeSkillActivationRequest(
            skill_id="invoice_table_cleanup",
            version="1.0.0",
            rationale="Fixes recurring HEADER_BOM_MISMATCH with QA-verified A/B gains.",
            task_id=baseline_task_ids[-1],
            timestamp=t0 + timedelta(minutes=6),
        ),
        auth_context=h.master,
    )
    assert prop.approval_id is not None
    h.approvals.decide_approval(
        prop.approval_id,
        decider=h.bob,
        approved=True,
        reason="Approved sensitive write + script Skill after QA A/B proof.",
        timestamp=t0 + timedelta(minutes=7),
    )
    skill_harness.skills.activate_skill_version(
        ActivateSkillRequest(proposal_id=prop.proposal_id, timestamp=t0 + timedelta(minutes=8)),
        auth_context=h.master,
    )

    # Step 8 (Integration with HR & Runtime): HR grants newly APPROVED skill to worker!
    hr = HrDepartmentService(
        registry=h.agent_registry,
        departments_root=REPO_ROOT / "agents" / "departments",
        skill_catalog=skill_harness.skills,
    )
    agent_rec = h.agent_registry.get_agent("it.specialist")
    assert agent_rec is not None
    with pytest.raises(PermissionDeniedError) as hr_err:
        hr.grant_skills(
            SkillGrantRequest(
                agent_id="it.specialist",
                skill_ids=["invoice_table_cleanup"],
                expected_record_version=agent_rec.record_version,
                reason="Verified skill ready for production tasks",
                auth_context=h.alice,
                approval_id=None,
                timestamp=t0 + timedelta(minutes=9),
            )
        )
    hr_apr_req = h.approvals.create_approval_request(
        requester=h.alice,
        action=hr_err.value.decision.action,
        target_resource=hr_err.value.decision.resource,
        task_id=None,
        run_id=None,
        request_fingerprint=hr_err.value.decision.request_fingerprint,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=hr_err.value.decision.matched_rule_ids,
            rule_versions=h.evaluator.policy_snapshot(hr_err.value.decision.matched_rule_ids),
            reason_code=hr_err.value.decision.reason_code,
        ),
        risk_summary="Grant QA-approved invoice_table_cleanup skill",
        requested_at=t0 + timedelta(minutes=9),
    )
    h.approvals.decide_approval(
        hr_apr_req.approval_id,
        decider=h.bob,
        approved=True,
        reason="Approved skill grant after QA A/B proof",
        timestamp=t0 + timedelta(minutes=9, seconds=30),
    )
    grant_out = hr.grant_skills(
        SkillGrantRequest(
            agent_id="it.specialist",
            skill_ids=["invoice_table_cleanup"],
            expected_record_version=agent_rec.record_version,
            reason="Verified skill ready for production tasks",
            auth_context=h.alice,
            approval_id=hr_apr_req.approval_id,
            timestamp=t0 + timedelta(minutes=10),
        )
    )
    assert grant_out.record.manifest.skills.allowed == ["invoice_table_cleanup"]

    # Post-activation task invokes the approved Skill and emits skill.usage_observed -> outcome RESOLVED!
    post_t, post_r = create_task_and_run(
        h,
        title="Post-activation invoice run",
        description="Execute using approved invoice_table_cleanup",
        assigned_agent_ids=["it.specialist"],
        timestamp=t0 + timedelta(minutes=12),
    )
    h.store.tools.record_execution(
        ToolExecutionRecord(
            execution_id="tex_m_post_0",
            tool_id="tools.files.write",
            task_id=post_t,
            run_id=post_r,
            actor_id="it.specialist",
            action="files.write",
            status="SUCCESS",
            duration_ms=25,
            executed_at=t0 + timedelta(minutes=12, seconds=2),
        )
    )
    h.store.events.append(
        Event(
            event_id=new_event_id(),
            timestamp=t0 + timedelta(minutes=12, seconds=3),
            event_type="skill.usage_observed",
            source="agent_runtime",
            task_id=post_t,
            run_id=post_r,
            actor_id="it.specialist",
            redacted_payload={
                "skill_id": "invoice_table_cleanup",
                "outcome": "SUCCEEDED",
                "participated": True,
            },
        )
    )
    post_retro = proc.compile_task_retrospective(
        CompileTaskRetrospectiveRequest(task_id=post_t, timestamp=t0 + timedelta(minutes=12, seconds=5)),
        auth_context=h.master,
    )
    assert post_retro.summary.skill_usage_status == "OBSERVED"
    assert post_retro.summary.skills_invoked == ["invoice_table_cleanup"]

    imp_outcome = skill_harness.skills.record_improvement_outcome(
        RecordImprovementOutcomeRequest(
            skill_id="invoice_table_cleanup",
            version="1.0.0",
            source_improvement_id=gap_imp.improvement_id,
            baseline_task_ids=baseline_task_ids,
            post_activation_task_ids=[post_t],
            timestamp=t0 + timedelta(minutes=13),
        ),
        auth_context=h.master,
    )
    assert imp_outcome.regression_detected is False
    resolved_imp = proc.get_suggested_improvement(gap_imp.improvement_id, auth_context=h.master)
    assert resolved_imp is not None and resolved_imp.status == "RESOLVED"

    # Restart store recovery confirms complete Phase 10 Method C state persists cleanly.
    re_store, re_skills = skill_harness.reopen()
    try:
        approved = re_skills.get_approved_skill("invoice_table_cleanup")
        assert approved is not None and approved.version == "1.0.0"
        assert len(re_skills.list_improvement_outcomes(skill_id="invoice_table_cleanup")) == 1
    finally:
        re_store.close()
