"""Option A benchmark wrapper measuring the unmodified P03.01 Custom Orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Callable, Sequence

from core.orchestration import (
    AttributedSpikeEvent,
    MasterWorkerSpikeResult,
    SpikeAgentSpec,
    run_master_worker_spike,
)
from engines.contract import EngineEvent, EngineRequest, EngineResult
from engines.registry import EngineRegistry
from tests.benchmarks.p03_02.models import (
    BenchmarkRunOutcome,
    aggregate_timing_breakdown,
)

logger = logging.getLogger(__name__)


class _RegistrySpyProxy:
    """Thin read-only proxy forwarding ``execute()`` to ``EngineRegistry`` while recording calls."""

    def __init__(self, inner: EngineRegistry) -> None:
        """Wrap an existing ``EngineRegistry`` without altering behavior."""
        self._inner = inner
        self.requests: list[EngineRequest] = []
        self.results: list[EngineResult] = []

    def execute(
        self,
        request: EngineRequest,
        *,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        """Delegate execution directly to the wrapped registry and record telemetry."""
        self.requests.append(request)
        result = self._inner.execute(request, on_event=on_event)
        self.results.append(result)
        return result


def run_custom_benchmark(
    *,
    registry: EngineRegistry,
    user_request: str,
    master_workspace_dir: Path,
    worker_workspace_dir: Path,
    allowed_root: Path,
    expected_artifact_relpath: str = "proof.txt",
    permitted_read_relpaths: Sequence[str] = ("package.json",),
    permitted_write_relpaths: Sequence[str] = ("proof.txt",),
    expected_artifact_content: str | None = None,
    required_artifact_substrings: Sequence[str] = (),
    provider_id: str = "antigravity",
    connection_id: str | None = None,
    model_id: str | None = None,
    timeout_seconds: int = 90,
    roster: dict[str, SpikeAgentSpec] | None = None,
    on_event: Callable[[AttributedSpikeEvent], None] | None = None,
    master_session_id: str | None = None,
    worker_session_id: str | None = None,
    allow_temp_workspace: bool = False,
) -> tuple[BenchmarkRunOutcome, MasterWorkerSpikeResult]:
    """Execute Option A (Custom Orchestrator) and collect normalized benchmark metrics."""
    spy = _RegistrySpyProxy(registry)
    t0 = time.perf_counter()
    spike_result = run_master_worker_spike(
        registry=spy,  # type: ignore[arg-type]
        user_request=user_request,
        master_workspace_dir=master_workspace_dir,
        worker_workspace_dir=worker_workspace_dir,
        allowed_root=allowed_root,
        expected_artifact_relpath=expected_artifact_relpath,
        permitted_read_relpaths=permitted_read_relpaths,
        permitted_write_relpaths=permitted_write_relpaths,
        expected_artifact_content=expected_artifact_content,
        required_artifact_substrings=required_artifact_substrings,
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        timeout_seconds=timeout_seconds,
        roster=roster,
        on_event=on_event,
        master_session_id=master_session_id,
        worker_session_id=worker_session_id,
        allow_temp_workspace=allow_temp_workspace,
    )
    wall_ms = int((time.perf_counter() - t0) * 1000)
    prompt_chars = sum(len(req.prompt) for req in spy.requests)

    conv_ids = [
        cid
        for cid in (
            spike_result.master_plan_conversation_id,
            spike_result.worker_conversation_id,
            spike_result.master_synthesis_conversation_id,
        )
        if cid
    ]
    observed_tools = (
        list(spike_result.worker_execution.engine_result.observed_tool_names)
        if spike_result.worker_execution is not None
        else []
    )

    timing = aggregate_timing_breakdown(
        total_duration_ms=wall_ms,
        verification_duration_ms=0,
        engine_results=spy.results,
        prompt_chars_total=prompt_chars,
        attributed_event_count=len(spike_result.attributed_events),
        framework_event_count=0,
        expected_baseline_calls=3,
    )
    outcome = BenchmarkRunOutcome(
        option="custom_orchestrator",
        status=spike_result.status,
        correlation_id=spike_result.correlation_id,
        master_session_id=spike_result.master_session_id,
        worker_session_id=spike_result.worker_session_id,
        provider_conversation_ids=conv_ids,
        observed_tool_names=observed_tools,
        qa_verification=spike_result.qa_verification,
        engine_results=spy.results,
        attributed_events=spike_result.attributed_events,
        framework_events=[],
        framework_token_usage=None,
        persistence_db_path=None,
        flow_id=None,
        error=spike_result.error,
        timing=timing,
        notes={
            "orchestrator_wall_ms": wall_ms,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "verification_included_in_overhead": True,
        },
    )
    logger.info(
        "Option A benchmark correlation=%s status=%s total_ms=%s provider_ms=%s calls=%s",
        outcome.correlation_id,
        outcome.status,
        timing.total_duration_ms,
        timing.provider_duration_ms,
        timing.provider_call_count,
    )
    return outcome, spike_result
