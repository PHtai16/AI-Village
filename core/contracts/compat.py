"""Pure read-only bridge helpers between Engine/AgentRuntime contracts and Core contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import (
    validate_agent_id,
    validate_legacy_session_ref,
    validate_session_id,
)
from core.contracts.references import ConnectionRef
from core.contracts.run import RunTelemetryRef
from core.runtime.contract import (
    AgentRuntimeEvent,
    AgentRuntimeTelemetry,
    ExecutionRouting,
)
from engines.connections import EngineConnectionRef


def connection_ref_from_engine(
    engine_conn: EngineConnectionRef,
    *,
    model_id: str | None = None,
    include_credential_ref: bool = False,
) -> ConnectionRef:
    """Create a Core ``ConnectionRef`` from an ``EngineConnectionRef`` without duplicating storage."""
    return ConnectionRef(
        provider_id=engine_conn.provider_id,
        connection_id=engine_conn.connection_id,
        model_id=model_id if model_id is not None else engine_conn.default_model_id,
        project_ref=engine_conn.project_ref,
        credential_ref=engine_conn.credential_ref if include_credential_ref else None,
    )


def connection_ref_to_execution_routing(conn: ConnectionRef) -> ExecutionRouting:
    """Convert a Core ``ConnectionRef`` into an ``AgentRuntime`` ``ExecutionRouting`` DTO."""
    return ExecutionRouting(
        provider_id=conn.provider_id,
        connection_id=conn.connection_id,
        model_id=conn.model_id,
    )


def run_telemetry_from_runtime(telemetry: AgentRuntimeTelemetry) -> RunTelemetryRef:
    """Project ``AgentRuntimeTelemetry`` into ``RunTelemetryRef`` preserving ``None`` metrics."""
    return RunTelemetryRef(
        total_duration_ms=telemetry.total_duration_ms,
        provider_duration_ms=telemetry.provider_duration_ms,
        engine_call_count=telemetry.engine_call_count,
        input_tokens=telemetry.input_tokens,
        output_tokens=telemetry.output_tokens,
        thinking_tokens=telemetry.thinking_tokens,
        cached_tokens=telemetry.cached_tokens,
        total_tokens=telemetry.total_tokens,
        estimated_cost_usd=telemetry.estimated_cost_usd,
        actual_cost_usd=telemetry.actual_cost_usd,
        telemetry_status=telemetry.telemetry_status,
        telemetry_source=telemetry.telemetry_source,
    )


def core_event_from_runtime_event(
    runtime_event: AgentRuntimeEvent,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    connection_id: str | None = None,
    correlation_id: str | None = None,
    artifact_id: str | None = None,
    evidence_id: str | None = None,
    timestamp: datetime | None = None,
) -> Event:
    """Project an ``AgentRuntimeEvent`` into a sanitized Core ``Event`` without fake timestamps."""
    resolved_ts = timestamp
    if resolved_ts is None and runtime_event.engine_event is not None:
        resolved_ts = runtime_event.engine_event.timestamp
    if resolved_ts is None:
        raise ValueError(
            "Cannot project AgentRuntimeEvent to Event without a real timestamp from caller or EngineEvent."
        )

    payload: dict[str, Any] = {
        "invocation_id": runtime_event.invocation_id,
        "runtime_id": runtime_event.runtime_id,
        "sequence": runtime_event.sequence,
        "stage": runtime_event.stage,
        "actor_role": runtime_event.actor_role,
    }
    if runtime_event.provider_conversation_id is not None:
        payload["provider_conversation_id"] = runtime_event.provider_conversation_id

    resolved_session_id = session_id
    logical = runtime_event.logical_session_id
    try:
        validated_uuid = validate_session_id(logical)
        if resolved_session_id is None:
            resolved_session_id = validated_uuid
    except ValueError:
        payload["legacy_session_ref"] = validate_legacy_session_ref(logical)

    resolved_actor_id: str | None
    try:
        resolved_actor_id = validate_agent_id(runtime_event.actor_id)
    except ValueError:
        resolved_actor_id = None
        payload["raw_actor_id"] = runtime_event.actor_id

    resolved_connection_id = connection_id
    if resolved_connection_id is None and runtime_event.engine_event is not None:
        resolved_connection_id = runtime_event.engine_event.connection_id

    if runtime_event.engine_event is not None:
        payload["engine_event_id"] = runtime_event.engine_event.event_id
        payload["engine_event_type"] = runtime_event.engine_event.event_type
        payload["engine_status"] = runtime_event.engine_event.status
        if runtime_event.engine_event.tool_name is not None:
            payload["tool_name"] = runtime_event.engine_event.tool_name
        if runtime_event.engine_event.redacted_payload:
            payload["engine_payload"] = runtime_event.engine_event.redacted_payload

    return Event(
        timestamp=resolved_ts,
        event_type="engine.event_forwarded",
        source="agent_runtime",
        task_id=task_id,
        run_id=run_id,
        session_id=resolved_session_id,
        actor_id=resolved_actor_id,
        provider_id=runtime_event.provider_id,
        connection_id=resolved_connection_id,
        artifact_id=artifact_id,
        evidence_id=evidence_id,
        correlation_id=correlation_id,
        redacted_payload=sanitize_event_payload(payload),
    )
