"""First concrete AgentRuntime backed by ADR-0002's Custom Orchestrator and EngineRegistry."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Callable, Literal

from core.orchestration import (
    AttributedSpikeEvent,
    MasterWorkerSpikeResult,
    SpikeAgentSpec,
    run_master_worker_spike,
)
from core.runtime.contract import (
    AgentRuntimeArtifactRef,
    AgentRuntimeError,
    AgentRuntimeEvent,
    AgentRuntimeInfo,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    AgentRuntimeTelemetry,
)
from engines.contract import EngineResult
from engines.registry import EngineRegistry

logger = logging.getLogger(__name__)


def _aggregate_runtime_telemetry(
    total_duration_ms: int,
    engine_results: list[EngineResult],
) -> AgentRuntimeTelemetry:
    """Aggregate Engine telemetry honestly without fabricating zeros for missing usage."""
    provider_duration_ms = sum(res.duration_ms for res in engine_results)
    call_count = len(engine_results)
    if not engine_results:
        return AgentRuntimeTelemetry(
            total_duration_ms=total_duration_ms,
            provider_duration_ms=0,
            engine_call_count=0,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        )

    all_verified = all(res.telemetry.telemetry_status == "VERIFIED_MEASURED" for res in engine_results)
    sources = {res.telemetry.telemetry_source for res in engine_results}
    telemetry_source: Literal["provider_cli", "provider_api", "unavailable"] = (
        "provider_cli"
        if sources == {"provider_cli"}
        else ("provider_api" if sources == {"provider_api"} else "unavailable")
    )

    def _sum_opt(attr: str) -> int | None:
        vals = [getattr(res.telemetry, attr) for res in engine_results]
        if any(v is None for v in vals):
            return None
        return sum(int(v) for v in vals if v is not None)

    in_tok = _sum_opt("input_tokens")
    out_tok = _sum_opt("output_tokens")
    tot_tok = _sum_opt("total_tokens")
    if not all_verified or in_tok is None or out_tok is None or tot_tok is None:
        return AgentRuntimeTelemetry(
            total_duration_ms=total_duration_ms,
            provider_duration_ms=provider_duration_ms,
            engine_call_count=call_count,
            input_tokens=None,
            output_tokens=None,
            thinking_tokens=None,
            cached_tokens=None,
            total_tokens=None,
            estimated_cost_usd=None,
            actual_cost_usd=None,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        )

    return AgentRuntimeTelemetry(
        total_duration_ms=total_duration_ms,
        provider_duration_ms=provider_duration_ms,
        engine_call_count=call_count,
        input_tokens=in_tok,
        output_tokens=out_tok,
        thinking_tokens=_sum_opt("thinking_tokens"),
        cached_tokens=_sum_opt("cached_tokens"),
        total_tokens=tot_tok,
        estimated_cost_usd=None,
        actual_cost_usd=None,
        telemetry_status="VERIFIED_MEASURED",
        telemetry_source=telemetry_source,
    )


def _map_spike_event(
    *,
    invocation_id: str,
    runtime_id: str,
    event: AttributedSpikeEvent,
) -> AgentRuntimeEvent:
    """Convert one ``AttributedSpikeEvent`` into ``AgentRuntimeEvent``."""
    return AgentRuntimeEvent(
        invocation_id=invocation_id,
        runtime_id=runtime_id,
        sequence=event.sequence,
        stage=event.stage,
        actor_id=event.agent_id,
        actor_role=event.role,
        logical_session_id=event.logical_session_id,
        provider_id=event.engine_event.provider_id,
        provider_conversation_id=event.provider_conversation_id,
        engine_event=event.engine_event,
    )


def _map_artifact_ref(spike: MasterWorkerSpikeResult) -> AgentRuntimeArtifactRef | None:
    """Convert independent QA disk verification into ``AgentRuntimeArtifactRef``."""
    qa = spike.qa_verification
    if qa is None:
        return None
    return AgentRuntimeArtifactRef(
        artifact_relpath=qa.artifact_relpath,
        artifact_abs_path=qa.artifact_abs_path,
        exists_on_disk=qa.artifact_exists,
        sha256=qa.actual_sha256,
        size_bytes=qa.file_size_bytes,
        qa_status=qa.status,
    )


class CustomAgentRuntime:
    """``AgentRuntime`` implementation delegating to the verified Custom Orchestrator."""

    def __init__(
        self,
        *,
        registry: EngineRegistry,
        roster: dict[str, SpikeAgentSpec] | None = None,
        allow_temp_workspace: bool = False,
        runtime_id: str = "custom.master_worker_v1",
    ) -> None:
        """Initialize the runtime composing ``EngineRegistry`` and P03.01 orchestration."""
        self._registry = registry
        self._roster = roster
        self._allow_temp_workspace = allow_temp_workspace
        self._runtime_id = runtime_id

    def get_runtime_info(self) -> AgentRuntimeInfo:
        """Report Custom Orchestrator runtime identity and cancellation boundary."""
        return AgentRuntimeInfo(
            runtime_id=self._runtime_id,
            runtime_kind="custom_orchestrator",
            version="0.3.3",
            supports_streaming_events=True,
            supports_independent_qa=True,
            supports_workflow_cancellation=False,
            cancellation_boundary_note=(
                "Subprocess cancellation uses AntigravityProcessManager + Win32JobGuard; "
                "workflow-level Task/Run cancellation belongs to PHASE 04."
            ),
        )

    def execute(
        self,
        request: AgentRuntimeRequest,
        *,
        on_event: Callable[[AgentRuntimeEvent], None] | None = None,
    ) -> AgentRuntimeResult:
        """Execute a workflow via ``run_master_worker_spike`` and return normalized outcomes."""
        started_at = datetime.now(timezone.utc)

        def _forward_event(ev: AttributedSpikeEvent) -> None:
            if on_event is not None:
                on_event(
                    _map_spike_event(
                        invocation_id=request.invocation_id,
                        runtime_id=self._runtime_id,
                        event=ev,
                    )
                )

        try:
            spike = run_master_worker_spike(
                registry=self._registry,
                user_request=request.user_objective,
                master_workspace_dir=Path(request.workspace_scope.orchestrator_workspace_path),
                worker_workspace_dir=Path(request.workspace_scope.worker_workspace_path),
                allowed_root=Path(request.workspace_scope.allowed_root_path),
                expected_artifact_relpath=request.acceptance.artifact_relpath,
                permitted_read_relpaths=tuple(request.workspace_scope.permitted_read_paths),
                permitted_write_relpaths=tuple(request.workspace_scope.permitted_write_paths),
                expected_artifact_content=request.acceptance.expected_content,
                required_artifact_substrings=tuple(request.acceptance.required_substrings),
                provider_id=request.routing.provider_id,
                connection_id=request.routing.connection_id,
                model_id=request.routing.model_id,
                timeout_seconds=request.timeout_seconds,
                roster=self._roster,
                on_event=_forward_event if on_event is not None else None,
                master_session_id=request.orchestrator_session_id,
                worker_session_id=request.worker_session_id,
                allow_temp_workspace=self._allow_temp_workspace,
            )
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            elapsed_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
            logger.exception(
                "CustomAgentRuntime execution error for invocation=%s",
                request.invocation_id,
            )
            return AgentRuntimeResult(
                runtime_id=self._runtime_id,
                invocation_id=request.invocation_id,
                correlation_id=f"corr-err-{request.invocation_id}",
                status="FAILED",
                technical_execution_succeeded=False,
                business_verified=False,
                telemetry=AgentRuntimeTelemetry(
                    total_duration_ms=elapsed_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                error=AgentRuntimeError(
                    error_code="RUNTIME_EXECUTION_ERROR",
                    stage="custom_runtime",
                    message=f"CustomAgentRuntime execution failed: {exc}",
                ),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=elapsed_ms,
            )

        engine_results: list[EngineResult] = []
        if spike.master_plan_result is not None:
            engine_results.append(spike.master_plan_result)
        if spike.worker_execution is not None:
            engine_results.append(spike.worker_execution.engine_result)
        if spike.master_synthesis_result is not None:
            engine_results.append(spike.master_synthesis_result)

        actors_executed: list[str] = []
        if spike.master_plan_result is not None or spike.master_synthesis_result is not None:
            actors_executed.append(spike.master_agent_id)
        if spike.worker_execution is not None:
            actors_executed.append(spike.worker_execution.worker_agent_id)

        conv_ids: list[str] = []
        for cid in (
            spike.master_plan_conversation_id,
            spike.worker_conversation_id,
            spike.master_synthesis_conversation_id,
        ):
            if cid and cid not in conv_ids:
                conv_ids.append(cid)

        observed_tools = (
            list(spike.worker_execution.engine_result.observed_tool_names)
            if spike.worker_execution is not None
            else []
        )
        runtime_events = [
            _map_spike_event(
                invocation_id=request.invocation_id,
                runtime_id=self._runtime_id,
                event=ev,
            )
            for ev in spike.attributed_events
        ]
        runtime_error = (
            AgentRuntimeError(
                error_code=spike.error.error_code,
                stage=spike.error.stage,
                message=spike.error.message,
                details=spike.error.details,
            )
            if spike.error is not None
            else None
        )
        tech_ok = bool(
            spike.worker_execution is not None
            and spike.worker_execution.engine_result.status == "SUCCEEDED"
        )
        qa_ok = bool(
            spike.status == "DELIVERED"
            and spike.qa_verification is not None
            and spike.qa_verification.status == "VERIFIED_PASS"
            and spike.qa_verification.business_verified is True
        )

        return AgentRuntimeResult(
            runtime_id=self._runtime_id,
            invocation_id=request.invocation_id,
            correlation_id=spike.correlation_id,
            status=spike.status,
            technical_execution_succeeded=tech_ok,
            business_verified=qa_ok,
            actors_executed=actors_executed,
            orchestrator_session_id=spike.master_session_id,
            worker_session_id=spike.worker_session_id,
            provider_conversation_ids=conv_ids,
            observed_tool_names=observed_tools,
            engine_results=engine_results,
            artifact=_map_artifact_ref(spike),
            qa_verification=spike.qa_verification,
            final_summary=(
                spike.master_synthesis.final_summary
                if spike.master_synthesis is not None
                else None
            ),
            telemetry=_aggregate_runtime_telemetry(spike.duration_ms, engine_results),
            events=runtime_events,
            error=runtime_error,
            started_at=spike.started_at,
            finished_at=spike.finished_at,
            duration_ms=spike.duration_ms,
        )
