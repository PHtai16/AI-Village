"""Application-level workflow caller depending strictly on the AgentRuntime Protocol."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Callable

from core.runtime.contract import (
    AgentRuntime,
    AgentRuntimeError,
    AgentRuntimeEvent,
    AgentRuntimeInfo,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    AgentRuntimeTelemetry,
)

logger = logging.getLogger(__name__)


def _require_runtime_protocol(runtime: AgentRuntime) -> None:
    """Reject non-conforming objects fail-closed before workflow invocation."""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError(
            f"Expected an AgentRuntime implementation, got {type(runtime).__name__}."
        )


def _enforce_qa_invariant(result: AgentRuntimeResult) -> AgentRuntimeResult:
    """Prevent any runtime from claiming DELIVERED without independent VERIFIED_PASS QA."""
    if result.status != "DELIVERED":
        return result

    qa = result.qa_verification
    qa_passed = (
        qa is not None
        and qa.status == "VERIFIED_PASS"
        and qa.business_verified is True
        and result.business_verified is True
    )
    if qa_passed:
        return result

    logger.error(
        "Application QA boundary rejected invalid DELIVERED result for invocation=%s",
        result.invocation_id,
    )
    return result.model_copy(
        update={
            "status": "FAILED",
            "business_verified": False,
            "error": AgentRuntimeError(
                error_code="QA_INVARIANT_VIOLATION",
                stage="application_qa_gate",
                message="Runtime returned DELIVERED without VERIFIED_PASS QA evidence.",
                details={"original_status": result.status},
            ),
        }
    )


def execute_agent_workflow(
    runtime: AgentRuntime,
    request: AgentRuntimeRequest,
    *,
    on_event: Callable[[AgentRuntimeEvent], None] | None = None,
) -> AgentRuntimeResult:
    """Execute one user objective solely through the ``AgentRuntime`` interface.

    Args:
        runtime: Injected runtime conforming to ``AgentRuntime``.
        request: Validated ``AgentRuntimeRequest`` carrying scope, routing, and QA specs.
        on_event: Optional callback receiving normalized ``AgentRuntimeEvent`` updates.

    Returns:
        Normalized ``AgentRuntimeResult`` verified against the application QA invariant.
    """
    _require_runtime_protocol(runtime)
    info = runtime.get_runtime_info()
    started_at = datetime.now(timezone.utc)
    logger.info(
        "Executing workflow invocation=%s via runtime=%s kind=%s",
        request.invocation_id,
        info.runtime_id,
        info.runtime_kind,
    )

    try:
        result = runtime.execute(request, on_event=on_event)
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        elapsed_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        logger.exception(
            "Unhandled runtime error for invocation=%s runtime=%s",
            request.invocation_id,
            info.runtime_id,
        )
        return AgentRuntimeResult(
            runtime_id=info.runtime_id,
            invocation_id=request.invocation_id,
            correlation_id=f"corr-unhandled-{request.invocation_id}",
            status="FAILED",
            technical_execution_succeeded=False,
            business_verified=False,
            telemetry=AgentRuntimeTelemetry(
                total_duration_ms=elapsed_ms,
                provider_duration_ms=0,
                engine_call_count=0,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
            error=AgentRuntimeError(
                error_code="RUNTIME_UNHANDLED_ERROR",
                stage="application",
                message=f"Runtime raised unhandled exception: {exc}",
            ),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=elapsed_ms,
        )

    return _enforce_qa_invariant(result)


class AgentWorkflowRunner:
    """Thin application service injected with any ``AgentRuntime`` implementation."""

    def __init__(self, runtime: AgentRuntime) -> None:
        """Bind a runtime satisfying ``AgentRuntime`` without concrete class coupling."""
        _require_runtime_protocol(runtime)
        self._runtime = runtime

    @property
    def runtime_info(self) -> AgentRuntimeInfo:
        """Inspect runtime identity and capability boundaries."""
        return self._runtime.get_runtime_info()

    def run(
        self,
        request: AgentRuntimeRequest,
        *,
        on_event: Callable[[AgentRuntimeEvent], None] | None = None,
    ) -> AgentRuntimeResult:
        """Dispatch a workflow request strictly through ``AgentRuntime.execute``."""
        return execute_agent_workflow(self._runtime, request, on_event=on_event)
