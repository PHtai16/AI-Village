"""Tests for Phase 10 / P10.04: IT technical validation, independent QA A/B benchmarks, and immutability."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import sqlite3

import pytest

from core.skills import (
    ItTechnicalReviewRequest,
    QaSkillBenchmarkRequest,
    SkillBenchmarkCaseOutcome,
    SkillBenchmarkError,
    SkillGovernanceError,
    SkillIndependenceError,
    SkillIntegrityError,
    SkillPackageRecord,
    SkillTestCaseSpec,
)
from core.tools.models import ToolInvocationRequest
from tests.departments.conftest import create_task_and_run, register_worker_agent, utc, worker_ctx
from tests.skills.conftest import SkillFactoryHarness, make_candidate_request


class _WorkspaceAbFixtureRunner:
    """Real ToolRuntime-backed deterministic fixture runner executing baseline A vs Skill B."""

    def __init__(self, harness: SkillFactoryHarness, *, task_id: str, run_id: str) -> None:
        self._h = harness
        self._task_id = task_id
        self._run_id = run_id
        self._ctx = worker_ctx(
            "qa.benchmarker",
            allowed_tools=["tools.files.write", "tools.files.read"],
            assigned_task_ids=[task_id],
        )

    def execute_fixture_arm(
        self,
        *,
        package: SkillPackageRecord,
        case: SkillTestCaseSpec,
        arm: str,
    ) -> SkillBenchmarkCaseOutcome:
        rel_path = f"workspace/{self._task_id}/bench_{case.case_id}_{arm}.csv"
        if case.case_kind == "happy_path":
            if arm == "A_BASELINE":
                # Baseline performs redundant trial writes/reads before clean output -> 2 tool calls, higher duration.
                w1 = self._h.dept.tool_runtime.invoke(
                    ToolInvocationRequest(
                        auth_context=self._ctx,
                        tool_id="tools.files.write",
                        parameters={"path": rel_path, "content": "\ufeffa,b\n1,2\n", "overwrite": True},
                        task_id=self._task_id,
                        run_id=self._run_id,
                        workspace_root=str(self._h.dept.tmp_path),
                        target_path=rel_path,
                        timestamp=utc(10, 5, 0),
                    )
                )
                r1 = self._h.dept.tool_runtime.invoke(
                    ToolInvocationRequest(
                        auth_context=self._ctx,
                        tool_id="tools.files.read",
                        parameters={"path": rel_path},
                        task_id=self._task_id,
                        run_id=self._run_id,
                        workspace_root=str(self._h.dept.tmp_path),
                        target_path=rel_path,
                        timestamp=utc(10, 5, 1),
                    )
                )
                w2 = self._h.dept.tool_runtime.invoke(
                    ToolInvocationRequest(
                        auth_context=self._ctx,
                        tool_id="tools.files.write",
                        parameters={"path": rel_path, "content": "a,b\n1,2\n", "overwrite": True},
                        task_id=self._task_id,
                        run_id=self._run_id,
                        workspace_root=str(self._h.dept.tmp_path),
                        target_path=rel_path,
                        timestamp=utc(10, 5, 2),
                    )
                )
                assert w1.status == r1.status == w2.status == "SUCCESS"
                sha = hashlib.sha256(b"a,b\n1,2\n").hexdigest()
                return SkillBenchmarkCaseOutcome(
                    case_id=case.case_id,
                    case_kind=case.case_kind,
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=45,
                    tool_calls_count=3,
                    error_count=1,
                    cost_usd=None,
                    output_sha256=sha,
                    notes="Baseline retried BOM decode manually.",
                )
            # Arm B executes the standardized 1-step skill cleanly.
            w_b = self._h.dept.tool_runtime.invoke(
                ToolInvocationRequest(
                    auth_context=self._ctx,
                    tool_id="tools.files.write",
                    parameters={"path": rel_path, "content": "a,b\n1,2\n", "overwrite": True},
                    task_id=self._task_id,
                    run_id=self._run_id,
                    workspace_root=str(self._h.dept.tmp_path),
                    target_path=rel_path,
                    timestamp=utc(10, 5, 3),
                )
            )
            assert w_b.status == "SUCCESS"
            sha = hashlib.sha256(b"a,b\n1,2\n").hexdigest()
            return SkillBenchmarkCaseOutcome(
                case_id=case.case_id,
                case_kind=case.case_kind,
                arm="B_SKILL",
                passed=True,
                duration_ms=15,
                tool_calls_count=1,
                error_count=0,
                cost_usd=None,
                output_sha256=sha,
                notes="Skill arm handled BOM in single pass.",
            )

        # failure_case: missing file check handles fail-closed cleanly on Arm B with fewer calls/errors.
        missing_read = self._h.dept.tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=self._ctx,
                tool_id="tools.files.read",
                parameters={"path": f"workspace/{self._task_id}/does_not_exist_{arm}.csv"},
                task_id=self._task_id,
                run_id=self._run_id,
                workspace_root=str(self._h.dept.tmp_path),
                target_path=f"workspace/{self._task_id}/does_not_exist_{arm}.csv",
                timestamp=utc(10, 5, 4),
            )
        )
        assert missing_read.status == "FAILED"
        sha = hashlib.sha256(f"{case.case_id}:{arm}:FILE_NOT_FOUND".encode()).hexdigest()
        return SkillBenchmarkCaseOutcome(
            case_id=case.case_id,
            case_kind=case.case_kind,
            arm="A_BASELINE" if arm == "A_BASELINE" else "B_SKILL",
            passed=True,
            duration_ms=25 if arm == "A_BASELINE" else 10,
            tool_calls_count=2 if arm == "A_BASELINE" else 1,
            error_count=0,
            cost_usd=None,
            output_sha256=sha,
            notes="Verified fail-closed missing-input guard.",
        )


def test_p10_04_it_review_and_qa_independent_ab_benchmark_passed(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Exercise IT technical review + real ToolRuntime QA A/B benchmark without self-certification."""
    h = skill_harness.dept
    t0 = utc(10, 0, 0)
    register_worker_agent(
        h,
        agent_id="it.dev",
        department="it",
        skills=["sandbox_testing"],
        timestamp=t0,
    )
    register_worker_agent(
        h,
        agent_id="qa.benchmarker",
        department="qa",
        skills=["artifact_qa_inspection"],
        tools_allowed=["tools.files.write", "tools.files.read"],
        timestamp=t0 + timedelta(seconds=2),
    )
    task_id, run_id = create_task_and_run(
        h,
        title="QA benchmark csv_bom_handler",
        description="Run A/B benchmark over workspace fixtures",
        timestamp=t0 + timedelta(seconds=5),
    )

    cand = skill_harness.skills.create_candidate(
        make_candidate_request(
            skill_id="csv_bom_handler",
            version="1.0.0",
            tools=["tools.files.read", "tools.files.write"],
            scripts={"clean_bom.py": "def strip_bom(raw: str) -> str:\n    return raw.lstrip('\\ufeff')\n"},
            minute_offset=1,
        ),
        auth_context=h.master,
    )
    assert cand.benchmark_status == "NOT_BENCHMARKED"

    # IT validates tool compatibility + sandbox readiness -> moves CANDIDATE -> UNDER_REVIEW, NOT APPROVED.
    it_ctx = worker_ctx("it.dev")
    review, reviewed_pkg = skill_harness.skills.record_it_technical_review(
        ItTechnicalReviewRequest(
            skill_id="csv_bom_handler",
            version="1.0.0",
            task_id=task_id,
            run_id=run_id,
            sandbox_passed=True,
            sandbox_stdout="2 passed in 0.12s sk-1234567890secretleaktest",
            findings=["Verified scripts/clean_bom.py cleanly inside sandbox."],
            timestamp=t0 + timedelta(minutes=2),
        ),
        auth_context=it_ctx,
    )
    assert review.passed is True
    assert "sk-1234567890" not in review.sandbox_log_excerpt
    assert reviewed_pkg.lifecycle_status == "UNDER_REVIEW"
    assert reviewed_pkg.it_review_passed is True
    assert reviewed_pkg.approved_for_production is False

    # QA Independence: author (core.master), worker (qa.worker), AND IT reviewer (it.dev) CANNOT QA benchmark!
    with pytest.raises(SkillIndependenceError):
        skill_harness.skills.run_qa_benchmark(
            QaSkillBenchmarkRequest(
                skill_id="csv_bom_handler",
                version="1.0.0",
                summary="Master attempting self-benchmark",
                timestamp=t0 + timedelta(minutes=3),
            ),
            auth_context=h.master,
        )
    # Even if it.dev had trusted_verifier role, IT reviewer is explicitly barred by SoD!
    from core.governance.models import TrustedAuthContext

    it_spoofing_verifier = TrustedAuthContext(
        actor_id="it.dev",
        actor_kind="trusted_verifier",
        auth_source="application_boundary",
        authenticated=True,
    )
    with pytest.raises(SkillIndependenceError, match="Author/IT reviewer"):
        skill_harness.skills.run_qa_benchmark(
            QaSkillBenchmarkRequest(
                skill_id="csv_bom_handler",
                version="1.0.0",
                summary="IT reviewer attempting QA sign-off",
                timestamp=t0 + timedelta(minutes=3),
            ),
            auth_context=it_spoofing_verifier,
        )

    runner = _WorkspaceAbFixtureRunner(skill_harness, task_id=task_id, run_id=run_id)
    bm, pkg_after_bm = skill_harness.skills.run_qa_benchmark(
        QaSkillBenchmarkRequest(
            skill_id="csv_bom_handler",
            version="1.0.0",
            task_id=task_id,
            run_id=run_id,
            summary="Independent QA A/B fixture suite passed with reduced calls and latency.",
            timestamp=t0 + timedelta(minutes=4),
        ),
        auth_context=h.qa_verifier,
        runner=runner,
    )
    assert bm.benchmark_status == "PASSED"
    assert bm.evidence_id is not None
    assert bm.metrics.pass_rate_a == pytest.approx(1.0)
    assert bm.metrics.pass_rate_b == pytest.approx(1.0)
    assert bm.metrics.duration_reduction_pct > 0.0
    assert bm.metrics.tool_calls_reduction_pct > 0.0
    assert bm.metrics.cost_usd_a is None and bm.metrics.cost_usd_b is None
    assert pkg_after_bm.benchmark_status == "PASSED"
    assert pkg_after_bm.lifecycle_status == "UNDER_REVIEW"
    assert pkg_after_bm.approved_for_production is False  # Still awaits P10.05 Master + Human Approval!

    # SQLite triggers enforce immutability of skill_benchmarks.
    with h.store.open_connection() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE skill_benchmarks SET benchmark_status = 'FAILED' WHERE benchmark_id = ?;",
                (bm.benchmark_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM skill_benchmarks WHERE benchmark_id = ?;", (bm.benchmark_id,))


def test_p10_04_failed_inconclusive_missing_arms_and_disk_tamper(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Verify FAILED -> REJECTED, INCONCLUSIVE tie handling, incomplete arm rejection, and SHA tampering."""
    h = skill_harness.dept
    t0 = utc(11, 0, 0)
    sha = hashlib.sha256(b"fixture-out").hexdigest()

    # 1. Regression -> FAILED transitions package to REJECTED cleanly.
    skill_harness.skills.create_candidate(
        make_candidate_request(skill_id="regressing_skill", version="1.0.0"),
        auth_context=h.master,
    )
    bm_fail, pkg_fail = skill_harness.skills.run_qa_benchmark(
        QaSkillBenchmarkRequest(
            skill_id="regressing_skill",
            version="1.0.0",
            explicit_outcomes=[
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=20,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm="B_SKILL",
                    passed=False,
                    duration_ms=25,
                    tool_calls_count=2,
                    error_count=1,
                    output_sha256=sha,
                ),
                SkillBenchmarkCaseOutcome(
                    case_id="case_fail_missing_file",
                    case_kind="failure_case",
                    arm="A_BASELINE",
                    passed=True,
                    duration_ms=10,
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
            summary="Regressed happy path in Arm B",
            timestamp=t0 + timedelta(minutes=1),
        ),
        auth_context=h.qa_verifier,
    )
    assert bm_fail.benchmark_status == "FAILED"
    assert pkg_fail.benchmark_status == "FAILED"
    assert pkg_fail.lifecycle_status == "REJECTED"

    # 2. Exact tie with require_improvement=True -> INCONCLUSIVE (transparent status!).
    skill_harness.skills.create_candidate(
        make_candidate_request(skill_id="tie_skill", version="1.0.0"),
        auth_context=h.master,
    )
    bm_tie, pkg_tie = skill_harness.skills.run_qa_benchmark(
        QaSkillBenchmarkRequest(
            skill_id="tie_skill",
            version="1.0.0",
            explicit_outcomes=[
                SkillBenchmarkCaseOutcome(
                    case_id="case_happy_utf8_bom",
                    case_kind="happy_path",
                    arm=arm,  # type: ignore[arg-type]
                    passed=True,
                    duration_ms=20,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                )
                for arm in ("A_BASELINE", "B_SKILL")
            ]
            + [
                SkillBenchmarkCaseOutcome(
                    case_id="case_fail_missing_file",
                    case_kind="failure_case",
                    arm=arm,  # type: ignore[arg-type]
                    passed=True,
                    duration_ms=10,
                    tool_calls_count=1,
                    error_count=0,
                    output_sha256=sha,
                )
                for arm in ("A_BASELINE", "B_SKILL")
            ],
            require_improvement=True,
            summary="No quantitative speedup observed",
            timestamp=t0 + timedelta(minutes=2),
        ),
        auth_context=h.qa_verifier,
    )
    assert bm_tie.benchmark_status == "INCONCLUSIVE"
    assert pkg_tie.benchmark_status == "INCONCLUSIVE"

    # 3. Incomplete arm rejected fail-closed.
    with pytest.raises(SkillBenchmarkError, match="both A_BASELINE and B_SKILL") as exc_incomplete:
        skill_harness.skills.run_qa_benchmark(
            QaSkillBenchmarkRequest(
                skill_id="tie_skill",
                version="1.0.0",
                explicit_outcomes=[
                    SkillBenchmarkCaseOutcome(
                        case_id="case_happy_utf8_bom",
                        case_kind="happy_path",
                        arm="A_BASELINE",
                        passed=True,
                        duration_ms=20,
                        tool_calls_count=1,
                        error_count=0,
                        output_sha256=sha,
                    )
                ],
                summary="Incomplete single arm",
                timestamp=t0 + timedelta(minutes=3),
            ),
            auth_context=h.qa_verifier,
        )
    assert exc_incomplete.value.error_code == "INCOMPLETE_BENCHMARK_ARMS"

    # 4. Disk tampering between authoring and QA fails closed with SkillIntegrityError.
    tampered = skill_harness.skills.create_candidate(
        make_candidate_request(skill_id="tamper_test_skill", version="1.0.0"),
        auth_context=h.master,
    )
    md_path = skill_harness.skills_root / tampered.package_relpath / "SKILL.md"
    md_path.write_text(md_path.read_text(encoding="utf-8") + "\n# Tampered\n", encoding="utf-8")
    with pytest.raises(SkillIntegrityError, match="changed since registration") as exc_tamper:
        skill_harness.skills.run_qa_benchmark(
            QaSkillBenchmarkRequest(
                skill_id="tamper_test_skill",
                version="1.0.0",
                explicit_outcomes=[
                    SkillBenchmarkCaseOutcome(
                        case_id="case_happy_utf8_bom",
                        case_kind="happy_path",
                        arm="A_BASELINE",
                        passed=True,
                        duration_ms=20,
                        tool_calls_count=1,
                        error_count=0,
                        output_sha256=sha,
                    )
                ],
                summary="Must abort due to tampered file",
                timestamp=t0 + timedelta(minutes=4),
            ),
            auth_context=h.qa_verifier,
        )
    assert exc_tamper.value.error_code == "CANDIDATE_INTEGRITY_MISMATCH"
