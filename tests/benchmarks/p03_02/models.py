"""Shared Pydantic v2 models and honest metric aggregators for P03.02 benchmarks."""

from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.orchestration import (
    AttributedSpikeEvent,
    OrchestrationError,
)
from engines.contract import EngineResult
from engines.verification import EngineArtifactVerificationResult


class BenchmarkTimingBreakdown(BaseModel):
    """Separated latency, call-count, prompt-size, and honest token metrics."""

    model_config = ConfigDict(extra="forbid")

    total_duration_ms: int = Field(ge=0)
    provider_duration_ms: int = Field(ge=0)
    verification_duration_ms: int = Field(ge=0)
    framework_overhead_ms: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    extra_reasoning_calls: int = Field(ge=0)
    prompt_chars_total: int = Field(ge=0)
    attributed_event_count: int = Field(ge=0)
    framework_event_count: int = Field(ge=0)
    input_tokens_total: int | None = None
    output_tokens_total: int | None = None
    thinking_tokens_total: int | None = None
    cached_tokens_total: int | None = None
    total_tokens_total: int | None = None
    telemetry_complete: bool


class BenchmarkRunOutcome(BaseModel):
    """Normalized outcome produced across Custom Orchestrator, CrewAI Flow, and CrewAI Crew."""

    model_config = ConfigDict(extra="forbid")

    option: Literal["custom_orchestrator", "crewai_flow", "crewai_crew"]
    status: Literal["DELIVERED", "FAILED", "BLOCKED", "REJECTED_BY_QA"]
    correlation_id: str = Field(min_length=1)
    master_session_id: str = Field(min_length=1)
    worker_session_id: str | None = None
    provider_conversation_ids: list[str] = Field(default_factory=list)
    observed_tool_names: list[str] = Field(default_factory=list)
    qa_verification: EngineArtifactVerificationResult | None = None
    engine_results: list[EngineResult] = Field(default_factory=list)
    attributed_events: list[AttributedSpikeEvent] = Field(default_factory=list)
    framework_events: list[dict[str, Any]] = Field(default_factory=list)
    framework_token_usage: dict[str, Any] | None = None
    persistence_db_path: str | None = None
    flow_id: str | None = None
    error: OrchestrationError | None = None
    timing: BenchmarkTimingBreakdown
    notes: dict[str, Any] = Field(default_factory=dict)


def _sum_optional_metric(
    results: Sequence[EngineResult],
    attr: str,
) -> tuple[int | None, bool]:
    """Sum a telemetry integer attribute across results without converting None to zero."""
    if not results:
        return None, False
    values = [getattr(res.telemetry, attr) for res in results]
    if any(val is None for val in values):
        return None, False
    return sum(int(val) for val in values if val is not None), True


def aggregate_timing_breakdown(
    *,
    total_duration_ms: int,
    verification_duration_ms: int,
    engine_results: Sequence[EngineResult],
    prompt_chars_total: int,
    attributed_event_count: int,
    framework_event_count: int,
    expected_baseline_calls: int = 3,
) -> BenchmarkTimingBreakdown:
    """Compute separated overhead metrics while keeping missing telemetry strictly None."""
    provider_duration_ms = sum(res.duration_ms for res in engine_results)
    framework_overhead_ms = max(
        0,
        total_duration_ms - provider_duration_ms - verification_duration_ms,
    )
    call_count = len(engine_results)
    extra_calls = max(0, call_count - expected_baseline_calls)

    input_total, in_ok = _sum_optional_metric(engine_results, "input_tokens")
    output_total, out_ok = _sum_optional_metric(engine_results, "output_tokens")
    thinking_total, think_ok = _sum_optional_metric(engine_results, "thinking_tokens")
    cached_total, cache_ok = _sum_optional_metric(engine_results, "cached_tokens")
    total_tokens, tot_ok = _sum_optional_metric(engine_results, "total_tokens")
    all_verified = bool(engine_results) and all(
        res.telemetry.telemetry_status == "VERIFIED_MEASURED" for res in engine_results
    )
    telemetry_complete = bool(
        all_verified and in_ok and out_ok and tot_ok and think_ok and cache_ok
    )

    return BenchmarkTimingBreakdown(
        total_duration_ms=max(0, total_duration_ms),
        provider_duration_ms=provider_duration_ms,
        verification_duration_ms=max(0, verification_duration_ms),
        framework_overhead_ms=framework_overhead_ms,
        provider_call_count=call_count,
        extra_reasoning_calls=extra_calls,
        prompt_chars_total=max(0, prompt_chars_total),
        attributed_event_count=max(0, attributed_event_count),
        framework_event_count=max(0, framework_event_count),
        input_tokens_total=input_total,
        output_tokens_total=output_total,
        thinking_tokens_total=thinking_total,
        cached_tokens_total=cached_total,
        total_tokens_total=total_tokens,
        telemetry_complete=telemetry_complete,
    )
