"""Tests for Phase 10 / P10.05: Master activation proposals, Human Approval Gate, outcomes, and rollback."""

from __future__ import annotations

from datetime import timedelta
import sqlite3

import pytest

from core.agents.hr import HrDepartmentService
from core.agents.hr_models import HrSkillApprovalError, SkillGrantRequest
from core.departments import CompileTaskRetrospectiveRequest, ProcessDepartmentService
from core.skills import (
    ActivateSkillRequest,
    ProposeSkillActivationRequest,
    QaSkillBenchmarkRequest,
    RecordImprovementOutcomeRequest,
    RollbackSkillRequest,
    SkillBenchmarkCaseOutcome,
    SkillGovernanceError,
    SkillIntegrityError,
    SkillStateError,
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


def _pass_benchmark(
    h_skills: SkillFactoryHarness,
    *,
    skill_id: str,
    version: str,
    minute_offset: int = 2,
) -> None:
    import hashlib

    sha = hashlib.sha256(f"{skill_id}@{version}".encode()).hexdigest()
    h_skills.skills.run_qa_benchmark(
        QaSkillBenchmarkRequest(
            skill_id=skill_id,
            version=version,
            explicit_outcomes=[
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=40,
                    tool_calls_count=3,
                    error_count=1,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm="B_SKILL",
                    passed=True,
                    duration_ms=15,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_fail_missing_file",
                    case_kind="failure_case",
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=12,
                    tool_calls_count=2,
                    error_count=0,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_fail_missing_file",
                    case_kind="failure_case",
                    arm="B_SKILL",
                    passed=True,
                    duration_ms=8,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                ),
            ],
            summary="Passed fixture A/B benchmark.",
            timestamp=utc(10, 0, 0) + timedelta(minutes=minute_offset),
        ),
        auth_context=h_skills.dept.qa_verifier,
    )


def test_p10_05_non_sensitive_master_activation_and_sensitive_human_approval_gate(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Verify non-sensitive vs sensitive skill gating, pending approvals queue, and tamper fail-closed."""
    h = skill_harness.dept
    t0 = utc(10, 0, 0)

    # 1. Low-risk read-only skill (no scripts, no dependencies, read-only tools): Master activates cleanly.
    skill_harness.skills.create_candidate(
        make_candidate_request(
            skill_id="read_only_guide",
            version="1.0.0",
            tools=["tools.files.read"],
            risk_level="low",
        ),
        auth_context=h.master,
    )
    _pass_benchmark(skill_harness, skill_id="read_only_guide", version="1.0.0", minute_offset=1)

    prop_low = skill_harness.skills.propose_skill_activation(
        ProposeSkillActivationRequest(
            skill_id="read_only_guide",
            version="1.0.0",
            rationale="Low-risk read-only guide passed QA fixtures.",
            timestamp=t0 + timedelta(minutes=3),
        ),
        auth_context=h.master,
    )
    assert prop_low.requires_human_approval is False
    assert prop_low.proposal_status == "PENDING_ACTIVATION"
    assert prop_low.approval_id is None

    # Worker NEVER activates even a non-sensitive skill!
    with pytest.raises(SkillGovernanceError):
        skill_harness.skills.activate_skill_version(
            ActivateSkillRequest(proposal_id=prop_low.proposal_id, timestamp=t0 + timedelta(minutes=4)),
            auth_context=worker_ctx("it.coder"),
        )

    act_low = skill_harness.skills.activate_skill_version(
        ActivateSkillRequest(proposal_id=prop_low.proposal_id, timestamp=t0 + timedelta(minutes=5)),
        auth_context=h.master,
    )
    assert act_low.package.lifecycle_status == "APPROVED"
    assert act_low.package.storage_zone == "approved"
    assert (skill_harness.skills_root / act_low.package.package_relpath / "SKILL.md").is_file()
    assert skill_harness.skills.get_approved_skill("read_only_guide") is not None
    assert "Mục đích" in skill_harness.skills.get_active_skill_markdown("read_only_guide")

    # 2. Sensitive skill (has scripts + sensitive tool tools.files.write): requires Human Operator approval!
    skill_harness.skills.create_candidate(
        make_candidate_request(
            skill_id="scripted_csv_writer",
            version="1.0.0",
            tools=["tools.files.read", "tools.files.write"],
            scripts={"write_norm.py": "def norm(s: str) -> str:\n    return s.strip()\n"},
            risk_level="medium",
            modifies_sensitive_workflow=True,
            minute_offset=6,
        ),
        auth_context=h.master,
    )
    _pass_benchmark(skill_harness, skill_id="scripted_csv_writer", version="1.0.0", minute_offset=7)

    prop_sens = skill_harness.skills.propose_skill_activation(
        ProposeSkillActivationRequest(
            skill_id="scripted_csv_writer",
            version="1.0.0",
            rationale="Introduces write tool and Python helper script.",
            timestamp=t0 + timedelta(minutes=8),
        ),
        auth_context=h.master,
    )
    assert prop_sens.requires_human_approval is True
    assert prop_sens.proposal_status == "PENDING_APPROVAL"
    assert prop_sens.approval_id is not None
    pending_q = skill_harness.skills.list_pending_skill_approvals()
    assert [p.proposal_id for p in pending_q] == [prop_sens.proposal_id]

    # Master attempting activation BEFORE operator decides ApprovalRequest FAILS CLOSED!
    with pytest.raises(SkillGovernanceError):
        skill_harness.skills.activate_skill_version(
            ActivateSkillRequest(
                proposal_id=prop_sens.proposal_id,
                approval_id=prop_sens.approval_id,
                timestamp=t0 + timedelta(minutes=9),
            ),
            auth_context=h.master,
        )

    # Human Operator approves the exact queued approval_id.
    h.approvals.decide_approval(
        prop_sens.approval_id,
        decider=h.alice,
        approved=True,
        reason="Reviewed clean Python script and QA benchmark results.",
        timestamp=t0 + timedelta(minutes=10),
    )
    act_sens = skill_harness.skills.activate_skill_version(
        ActivateSkillRequest(
            proposal_id=prop_sens.proposal_id,
            timestamp=t0 + timedelta(minutes=11),
        ),
        auth_context=h.master,
    )
    assert act_sens.package.lifecycle_status == "APPROVED"
    assert skill_harness.skills.list_pending_skill_approvals() == []

    # 3. Single-use approval replay rejected; disk tampering blocks catalog exposure fail-closed.
    approved_md = skill_harness.skills_root / act_sens.package.package_relpath / "SKILL.md"
    orig_md = approved_md.read_text(encoding="utf-8")
    approved_md.write_text(orig_md + "\n# Tampered post-approval\n", encoding="utf-8")
    assert skill_harness.skills.get_approved_skill("scripted_csv_writer") is None
    with pytest.raises(SkillIntegrityError):
        skill_harness.skills.get_active_skill_markdown("scripted_csv_writer")
    # Restore bytes so later checks stay valid on disk if needed.
    approved_md.write_text(orig_md, encoding="utf-8")
    assert skill_harness.skills.get_approved_skill("scripted_csv_writer") is not None


def test_p10_05_version_upgrade_instant_rollback_disable_and_measured_outcomes(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Verify v1 -> v2 upgrade, measured post-activation outcomes, instant rollback v2 -> v1, and emergency disable."""
    h = skill_harness.dept
    t0 = utc(11, 0, 0)
    proc = ProcessDepartmentService(
        permission_manager=h.permissions,
        journal=h.store.events,
        retrospective_repo=h.store.process,
    )

    # Seed baseline Tasks (2 Tasks) & activate v1.0.0.
    base_tids: list[str] = []
    for idx in range(2):
        tid, rid = create_task_and_run(h, title=f"Base task {idx}", description="Pre-skill work", timestamp=t0)
        h.store.tools.record_execution(
            ToolExecutionRecord(
                execution_id=f"tex_base_{idx}",
                tool_id="tools.files.read",
                task_id=tid,
                run_id=rid,
                actor_id="it.coder",
                action="files.read",
                status="FAILED",
                error_code="PARSE_FAIL",
                error_message="Bad CSV",
                duration_ms=80,
                executed_at=t0 + timedelta(seconds=idx + 1),
            )
        )
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=tid, pattern_tag="csv_safe_parse", timestamp=t0 + timedelta(seconds=10)),
            auth_context=h.master,
        )
        base_tids.append(tid)

    # Activate v1.0.0 cleanly.
    skill_harness.skills.create_candidate(
        make_candidate_request(skill_id="csv_safe_parse", version="1.0.0", minute_offset=61),
        auth_context=h.master,
    )
    _pass_benchmark(skill_harness, skill_id="csv_safe_parse", version="1.0.0", minute_offset=62)
    p1 = skill_harness.skills.propose_skill_activation(
        ProposeSkillActivationRequest(
            skill_id="csv_safe_parse",
            version="1.0.0",
            rationale="Initial v1.0.0 rollout",
            timestamp=t0 + timedelta(minutes=3),
        ),
        auth_context=h.master,
    )
    skill_harness.skills.activate_skill_version(
        ActivateSkillRequest(proposal_id=p1.proposal_id, timestamp=t0 + timedelta(minutes=4)),
        auth_context=h.master,
    )

    # Upgrade to v1.1.0 -> v1.0.0 becomes DEPRECATED while remaining immutable on disk.
    skill_harness.skills.create_candidate(
        make_candidate_request(skill_id="csv_safe_parse", version="1.1.0", minute_offset=65),
        auth_context=h.master,
    )
    _pass_benchmark(skill_harness, skill_id="csv_safe_parse", version="1.1.0", minute_offset=66)
    p2 = skill_harness.skills.propose_skill_activation(
        ProposeSkillActivationRequest(
            skill_id="csv_safe_parse",
            version="1.1.0",
            rationale="v1.1.0 rollout",
            timestamp=t0 + timedelta(minutes=7),
        ),
        auth_context=h.master,
    )
    skill_harness.skills.activate_skill_version(
        ActivateSkillRequest(proposal_id=p2.proposal_id, timestamp=t0 + timedelta(minutes=8)),
        auth_context=h.master,
    )
    ptr_v2 = skill_harness.skills.get_active_pointer("csv_safe_parse")
    assert ptr_v2 is not None and ptr_v2.active_version == "1.1.0"
    assert ptr_v2.previous_approved_version == "1.0.0"

    # Seed post-activation Tasks (zero errors, shorter durations) -> outcome shows non-regression!
    post_tids: list[str] = []
    for idx in range(2):
        tid, rid = create_task_and_run(h, title=f"Post task {idx}", description="Post-skill work", timestamp=t0 + timedelta(minutes=9))
        h.store.tools.record_execution(
            ToolExecutionRecord(
                execution_id=f"tex_post_{idx}",
                tool_id="tools.files.read",
                task_id=tid,
                run_id=rid,
                actor_id="it.coder",
                action="files.read",
                status="SUCCESS",
                duration_ms=20,
                executed_at=t0 + timedelta(minutes=9, seconds=idx + 1),
            )
        )
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=tid, timestamp=t0 + timedelta(minutes=9, seconds=10)),
            auth_context=h.master,
        )
        post_tids.append(tid)

    outcome = skill_harness.skills.record_improvement_outcome(
        RecordImprovementOutcomeRequest(
            skill_id="csv_safe_parse",
            version="1.1.0",
            baseline_task_ids=base_tids,
            post_activation_task_ids=post_tids,
            timestamp=t0 + timedelta(minutes=12),
        ),
        auth_context=h.master,
    )
    assert outcome.baseline_error_rate == pytest.approx(1.0)
    assert outcome.post_error_rate == pytest.approx(0.0)
    assert outcome.regression_detected is False

    # SQLite immutability check on skill_improvement_outcomes.
    with h.store.open_connection() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE skill_improvement_outcomes SET regression_detected = 1 WHERE outcome_id = ?;",
                (outcome.outcome_id,),
            )

    # Instant rollback from v1.1.0 -> v1.0.0!
    rb = skill_harness.skills.rollback_skill(
        RollbackSkillRequest(
            skill_id="csv_safe_parse",
            mark_current_as="DEPRECATED",
            reason="Regression seen in production edge cases; restoring 1.0.0.",
            timestamp=t0 + timedelta(minutes=15),
        ),
        auth_context=h.master,
    )
    assert rb.rolled_back_from_version == "1.1.0"
    assert rb.restored_version == "1.0.0"
    assert skill_harness.skills.get_approved_skill("csv_safe_parse").version == "1.0.0"  # type: ignore[union-attr]
    v1_pkg = skill_harness.skills.get_package("csv_safe_parse", "1.0.0")
    v2_pkg = skill_harness.skills.get_package("csv_safe_parse", "1.1.0")
    assert v1_pkg is not None and v1_pkg.lifecycle_status == "APPROVED"
    assert v2_pkg is not None and v2_pkg.lifecycle_status == "DEPRECATED"

    # Emergency disable active skill clears active pointer cleanly.
    dis = skill_harness.skills.disable_skill(
        "csv_safe_parse",
        "1.0.0",
        reason="Emergency policy hold",
        auth_context=h.alice,
        timestamp=t0 + timedelta(minutes=18),
    )
    assert dis.lifecycle_status == "DISABLED"
    assert skill_harness.skills.get_approved_skill("csv_safe_parse") is None

    # HR cannot grant DISABLED skill; rejected candidate & replay approval fail closed.
    register_worker_agent(h, agent_id="it.guard", department="it", skills=["csv_safe_parse"], timestamp=t0 + timedelta(minutes=19))
    guard_rec = h.agent_registry.get_agent("it.guard")
    assert guard_rec is not None
    hr = HrDepartmentService(
        registry=h.agent_registry,
        departments_root=REPO_ROOT / "agents" / "departments",
        skill_catalog=skill_harness.skills,
    )
    with pytest.raises(HrSkillApprovalError):
        hr.grant_skills(
            SkillGrantRequest(
                agent_id="it.guard",
                skill_ids=["csv_safe_parse"],
                expected_record_version=guard_rec.record_version,
                reason="Cannot grant disabled skill",
                auth_context=h.alice,
                approval_id="apr_unused_blocked",
                timestamp=t0 + timedelta(minutes=20),
            )
        )

    skill_harness.skills.create_candidate(
        make_candidate_request(skill_id="rejected_skill", version="1.0.0", minute_offset=70),
        auth_context=h.master,
    )
    rej = skill_harness.skills.reject_skill(
        "rejected_skill",
        "1.0.0",
        reason="Rejected by policy review",
        auth_context=h.master,
        timestamp=t0 + timedelta(minutes=21),
    )
    assert rej.lifecycle_status == "REJECTED"
    with pytest.raises(SkillStateError):
        skill_harness.skills.propose_skill_activation(
            ProposeSkillActivationRequest(
                skill_id="rejected_skill",
                version="1.0.0",
                rationale="Cannot propose rejected skill",
                timestamp=t0 + timedelta(minutes=22),
            ),
            auth_context=h.master,
        )
