"""Deterministic A/B benchmark metrics calculator and verdict evaluator for P10.04 Skill QA."""

from __future__ import annotations

from typing import Iterable

from core.skills.models import (
    BenchmarkStatus,
    SkillBenchmarkCaseOutcome,
    SkillBenchmarkError,
    SkillBenchmarkMetrics,
    SkillTestCaseSpec,
)
from engines.antigravity.diagnostics import redact_secrets


def scrub_benchmark_outcomes(
    outcomes: Iterable[SkillBenchmarkCaseOutcome],
) -> list[SkillBenchmarkCaseOutcome]:
    """Return secret-scrubbed defensive copies of fixture benchmark outcomes."""
    return [
        o.model_copy(update={"notes": redact_secrets(o.notes).strip()[:500]})
        for o in outcomes
    ]


def compute_ab_benchmark_metrics(
    test_cases: list[SkillTestCaseSpec],
    outcomes: list[SkillBenchmarkCaseOutcome],
) -> tuple[SkillBenchmarkMetrics, list[SkillBenchmarkCaseOutcome], list[SkillBenchmarkCaseOutcome]]:
    """Compute strictly factual baseline A vs candidate B benchmark metrics across paired fixtures."""
    if not test_cases:
        raise SkillBenchmarkError(
            "MISSING_FIXTURE_CASES",
            "Skill package defines zero fixture test cases for A/B benchmarking.",
        )

    by_key: dict[tuple[str, str], SkillBenchmarkCaseOutcome] = {}
    valid_ids = {tc.case_id for tc in test_cases}
    for outcome in outcomes:
        if outcome.case_id not in valid_ids:
            raise SkillBenchmarkError(
                "UNKNOWN_BENCHMARK_CASE",
                f"Benchmark outcome references unknown fixture case '{outcome.case_id}'.",
            )
        key = (outcome.case_id, outcome.arm)
        if key in by_key:
            raise SkillBenchmarkError(
                "DUPLICATE_BENCHMARK_OUTCOME",
                f"Duplicate outcome supplied for fixture '{outcome.case_id}' arm '{outcome.arm}'.",
            )
        by_key[key] = outcome

    arm_a: list[SkillBenchmarkCaseOutcome] = []
    arm_b: list[SkillBenchmarkCaseOutcome] = []
    for tc in test_cases:
        out_a = by_key.get((tc.case_id, "A_BASELINE"))
        out_b = by_key.get((tc.case_id, "B_SKILL"))
        if out_a is None or out_b is None:
            raise SkillBenchmarkError(
                "INCOMPLETE_BENCHMARK_ARMS",
                f"Fixture '{tc.case_id}' must provide both A_BASELINE and B_SKILL measured outcomes.",
            )
        arm_a.append(out_a)
        arm_b.append(out_b)

    n = len(test_cases)
    avg_dur_a = round(sum(o.duration_ms for o in arm_a) / n, 2)
    avg_dur_b = round(sum(o.duration_ms for o in arm_b) / n, 2)
    dur_red_pct = (
        round(((avg_dur_a - avg_dur_b) / avg_dur_a) * 100.0, 2) if avg_dur_a > 0 else 0.0
    )

    avg_calls_a = round(sum(o.tool_calls_count for o in arm_a) / n, 2)
    avg_calls_b = round(sum(o.tool_calls_count for o in arm_b) / n, 2)
    calls_red_pct = (
        round(((avg_calls_a - avg_calls_b) / avg_calls_a) * 100.0, 2)
        if avg_calls_a > 0
        else 0.0
    )

    costs_a = [o.cost_usd for o in arm_a if o.cost_usd is not None]
    costs_b = [o.cost_usd for o in arm_b if o.cost_usd is not None]

    metrics = SkillBenchmarkMetrics(
        cases_count=n,
        happy_path_passed_a=sum(
            1 for o in arm_a if o.case_kind == "happy_path" and o.passed
        ),
        happy_path_passed_b=sum(
            1 for o in arm_b if o.case_kind == "happy_path" and o.passed
        ),
        failure_case_passed_a=sum(
            1 for o in arm_a if o.case_kind == "failure_case" and o.passed
        ),
        failure_case_passed_b=sum(
            1 for o in arm_b if o.case_kind == "failure_case" and o.passed
        ),
        pass_rate_a=round(sum(1 for o in arm_a if o.passed) / n, 4),
        pass_rate_b=round(sum(1 for o in arm_b if o.passed) / n, 4),
        avg_duration_ms_a=avg_dur_a,
        avg_duration_ms_b=avg_dur_b,
        duration_reduction_pct=dur_red_pct,
        avg_tool_calls_a=avg_calls_a,
        avg_tool_calls_b=avg_calls_b,
        tool_calls_reduction_pct=calls_red_pct,
        error_count_a=sum(o.error_count for o in arm_a),
        error_count_b=sum(o.error_count for o in arm_b),
        cost_usd_a=round(sum(costs_a), 8) if costs_a else None,
        cost_usd_b=round(sum(costs_b), 8) if costs_b else None,
    )
    return metrics, arm_a, arm_b


def evaluate_benchmark_status(
    *,
    metrics: SkillBenchmarkMetrics,
    arm_b: list[SkillBenchmarkCaseOutcome],
    require_improvement: bool,
    qualitative_rubric: dict[str, float],
) -> tuple[BenchmarkStatus, list[str]]:
    """Classify transparent A/B benchmark outcome without fabricating pass verdicts."""
    findings: list[str] = []

    failed_happy_b = [o.case_id for o in arm_b if o.case_kind == "happy_path" and not o.passed]
    failed_fail_b = [o.case_id for o in arm_b if o.case_kind == "failure_case" and not o.passed]
    if failed_happy_b:
        findings.append(f"B_SKILL failed happy_path fixtures: {failed_happy_b}.")
    if failed_fail_b:
        findings.append(f"B_SKILL failed failure_case fixtures: {failed_fail_b}.")
    if metrics.pass_rate_b < metrics.pass_rate_a:
        findings.append(
            f"Pass rate regressed under skill arm: B={metrics.pass_rate_b} < A={metrics.pass_rate_a}."
        )
    if metrics.error_count_b > metrics.error_count_a:
        findings.append(
            f"Error count regressed under skill arm: B={metrics.error_count_b} > A={metrics.error_count_a}."
        )
    if findings:
        return "FAILED", findings

    if any(not o.passed for o in arm_b):
        failed_edge = [o.case_id for o in arm_b if not o.passed]
        findings.append(f"B_SKILL failed edge fixture cases: {failed_edge}.")
        return "FAILED", findings

    if require_improvement:
        quant_gain = (
            metrics.pass_rate_b > metrics.pass_rate_a
            or metrics.duration_reduction_pct > 0.0
            or metrics.tool_calls_reduction_pct > 0.0
            or metrics.error_count_b < metrics.error_count_a
        )
        rubric_gain = bool(qualitative_rubric) and all(
            float(score) >= 0.7 for score in qualitative_rubric.values()
        )
        if not quant_gain and not rubric_gain:
            findings.append(
                "No measurable improvement observed in pass rate, duration, tool calls, errors, or rubric."
            )
            return "INCONCLUSIVE", findings

    return "PASSED", findings


__all__ = [
    "compute_ab_benchmark_metrics",
    "evaluate_benchmark_status",
    "scrub_benchmark_outcomes",
]
