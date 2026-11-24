"""Provider-neutral EngineAdapter wrapper around the verified Antigravity CLI adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable, cast

from pydantic import ValidationError

from engines.antigravity.adapter import AntigravityAdapter
from engines.antigravity.diagnostics import redact_secrets
from engines.antigravity.models import (
    AntigravityError,
    AntigravityHandshakeRequest,
    AntigravityHandshakeResult,
    AntigravityStreamEvent,
    AntigravityStreamRequest,
    AntigravityStreamResult,
    AntigravityTelemetry,
)
from engines.connections import EngineConnectionRef
from engines.contract import (
    EngineAdapter,
    EngineCapabilities,
    EngineError,
    EngineErrorCode,
    EngineErrorSource,
    EngineEvent,
    EngineEventType,
    EngineExecutionStatus,
    EnginePlatformKind,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
    EngineTelemetry,
)

logger = logging.getLogger(__name__)

_ERROR_CODE_MAP: dict[str, EngineErrorCode] = {
    "EXECUTABLE_NOT_FOUND": "EXECUTION_BLOCKED",
    "UNSUPPORTED_ARGUMENT": "UNSUPPORTED_ARGUMENT",
    "UNSUPPORTED_MODE": "UNSUPPORTED_CAPABILITY",
    "SANDBOX_UNVERIFIED": "UNSUPPORTED_CAPABILITY",
    "AUTH_ERROR": "AUTH_ERROR",
    "QUOTA_EXCEEDED": "QUOTA_EXCEEDED",
    "RATE_LIMITED": "RATE_LIMITED",
    "TIMED_OUT": "TIMED_OUT",
    "PROVIDER_ERROR": "PROVIDER_ERROR",
    "INVALID_RESPONSE": "INVALID_RESPONSE",
    "PROCESS_LAUNCH_ERROR": "PROCESS_LAUNCH_ERROR",
    "STREAM_PARSE_ERROR": "INVALID_RESPONSE",
    "STREAM_INTERRUPTED": "INVALID_RESPONSE",
    "MISSING_COMPLETION_EVENT": "INVALID_RESPONSE",
    "CANCELLED": "CANCELLED",
}


class AntigravityEngineAdapter(EngineAdapter):
    """EngineAdapter exposing the P02.01-P02.05 Antigravity CLI runtime through the shared contract."""

    def __init__(
        self,
        *,
        adapter: AntigravityAdapter | None = None,
        default_workspace_path: str | None = None,
    ) -> None:
        """Initialize the wrapper around ``AntigravityAdapter``.

        Args:
            adapter: Existing ``AntigravityAdapter`` instance to reuse.
            default_workspace_path: Optional workspace directory used when
                ``EngineRequest.workspace_path`` is omitted.
        """
        self._adapter = adapter or AntigravityAdapter()
        self._default_workspace_path = default_workspace_path

    @property
    def provider_id(self) -> str:
        """Return ``antigravity``."""
        return "antigravity"

    @property
    def adapter_id(self) -> str:
        """Return ``antigravity_cli``."""
        return "antigravity_cli"

    @property
    def platform_kind(self) -> EnginePlatformKind:
        """Return ``agent_execution_platform``."""
        return "agent_execution_platform"

    def get_capabilities(
        self,
        connection: EngineConnectionRef | None = None,
    ) -> EngineCapabilities:
        """Probe the local Antigravity CLI executable and return verified capability metadata."""
        _ = connection
        discovery = self._adapter.discover()
        if not discovery.found:
            return EngineCapabilities(
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                platform_kind=self.platform_kind,
                text_generation="UNSUPPORTED",
                structured_output="UNSUPPORTED",
                streaming="UNSUPPORTED",
                tool_calling_proposal="UNSUPPORTED",
                native_tool_execution="UNSUPPORTED",
                workspace_access="UNSUPPORTED",
                session_continuation="UNSUPPORTED",
                cancellation="UNSUPPORTED",
                usage_telemetry="UNSUPPORTED",
                model_discovery="UNSUPPORTED",
                requires_authentication=True,
                data_residency="cli_managed",
                evidence_notes={"discovery": discovery.error or "executable_not_found"},
            )

        caps = self._adapter.probe(timeout_seconds=15)
        probe_ok = caps.probe_exit_code == 0 or (
            caps.supports_print_mode and caps.supports_json_output
        )
        text_gen = "SUPPORTED" if (probe_ok and caps.supports_print_mode and caps.supports_json_output) else "UNSUPPORTED"
        streaming = (
            "SUPPORTED"
            if (probe_ok and caps.supports_print_mode and caps.supports_stream_json_flag and caps.supports_mode_flag)
            else "UNSUPPORTED"
        )
        native_tools = (
            "SUPPORTED"
            if (probe_ok and caps.supports_print_mode and caps.supports_sandbox_flag and caps.supports_mode_flag)
            else "UNSUPPORTED"
        )
        workspace_access = "SUPPORTED" if (probe_ok and caps.supports_sandbox_flag) else "UNSUPPORTED"
        session_cont = "SUPPORTED" if (probe_ok and caps.supports_conversation_flag) else "UNSUPPORTED"
        model_disc = "NOT_VERIFIED" if caps.supports_models_subcommand else "UNSUPPORTED"

        return EngineCapabilities(
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            platform_kind=self.platform_kind,
            text_generation=text_gen,
            structured_output="UNSUPPORTED",
            streaming=streaming,
            tool_calling_proposal="UNSUPPORTED",
            native_tool_execution=native_tools,
            workspace_access=workspace_access,
            session_continuation=session_cont,
            cancellation="SUPPORTED" if probe_ok else "UNSUPPORTED",
            usage_telemetry="SUPPORTED" if probe_ok else "UNSUPPORTED",
            model_discovery=model_disc,
            requires_authentication=True,
            data_residency="cli_managed",
            evidence_notes={
                "version": caps.version or "unknown",
                "executable": caps.executable_path,
            },
        )

    @staticmethod
    def _build_prompt(request: EngineRequest) -> str:
        """Combine optional context and prompt without losing separation."""
        if request.context and request.context.strip():
            return f"Context:\n{request.context.strip()}\n\nTask:\n{request.prompt}"
        return request.prompt

    @staticmethod
    def _map_telemetry(src: AntigravityTelemetry) -> EngineTelemetry:
        """Convert ``AntigravityTelemetry`` into provider-neutral ``EngineTelemetry``."""
        is_verified = src.telemetry_status == "VERIFIED_MEASURED"
        usage_dict: dict[str, Any] | None = None
        if is_verified:
            usage_dict = {
                key: val
                for key, val in (
                    ("input_tokens", src.input_tokens),
                    ("output_tokens", src.output_tokens),
                    ("thinking_tokens", src.thinking_tokens),
                    ("cache_read_tokens", src.cache_read_tokens),
                    ("total_tokens", src.total_tokens),
                )
                if val is not None
            }
        return EngineTelemetry(
            duration_ms=src.duration_ms,
            input_tokens=src.input_tokens,
            output_tokens=src.output_tokens,
            total_tokens=src.total_tokens,
            thinking_tokens=src.thinking_tokens,
            cached_tokens=src.cache_read_tokens,
            provider_duration_seconds=src.provider_duration_seconds,
            provider_reported_usage=usage_dict,
            estimated_cost_usd=None,
            actual_cost_usd=None,
            telemetry_status=src.telemetry_status,
            telemetry_source="provider_cli" if is_verified else "unavailable",
        )

    def _map_error(
        self,
        err: AntigravityError | None,
        connection_id: str | None,
    ) -> EngineError | None:
        """Map an ``AntigravityError`` into a normalized ``EngineError``."""
        if err is None:
            return None
        mapped_code: EngineErrorCode = _ERROR_CODE_MAP.get(err.error_code, "PROVIDER_ERROR")
        mapped_source: EngineErrorSource
        if err.error_source is not None:
            mapped_source = err.error_source
        elif err.error_code in {"EXECUTABLE_NOT_FOUND", "UNSUPPORTED_MODE", "SANDBOX_UNVERIFIED"}:
            mapped_source = "capability"
        elif err.error_code in {"TIMED_OUT", "PROCESS_LAUNCH_ERROR"}:
            mapped_source = "adapter"
        elif err.error_code == "CANCELLED":
            mapped_source = "cancellation"
        elif err.error_code == "UNSUPPORTED_ARGUMENT":
            mapped_source = "cli"
        else:
            mapped_source = "provider"

        return EngineError(
            error_code=mapped_code,
            error_source=mapped_source,
            message=redact_secrets(err.message) or f"Antigravity error: {err.error_code}",
            retryable=err.retryable,
            retry_after_seconds=err.retry_after_seconds,
            provider_id=self.provider_id,
            connection_id=connection_id,
            exit_code=err.exit_code,
            redacted_details=redact_secrets(err.redacted_stderr),
        )

    def _map_stream_event(
        self,
        event: AntigravityStreamEvent,
        connection_id: str | None,
    ) -> EngineEvent:
        """Convert one ``AntigravityStreamEvent`` into ``EngineEvent``."""
        event_map: dict[str, EngineEventType] = {
            "execution_started": "execution_started",
            "text_delta": "text_delta",
            "tool_call_started": "tool_execution_observed",
            "tool_call_completed": "tool_execution_observed",
            "tool_call_failed": "tool_execution_observed",
            "execution_completed": "execution_completed",
            "provider_error": "provider_error",
            "process_cancelled": "process_cancelled",
            "process_timed_out": "process_timed_out",
        }
        mapped_type: EngineEventType = event_map.get(event.event_type, "adapter_status")
        mapped_source = "provider" if event.source == "provider_cli" else "adapter"
        text_delta = (
            str(event.redacted_payload["text_delta"])
            if mapped_type == "text_delta" and "text_delta" in event.redacted_payload
            else None
        )
        return EngineEvent(
            event_id=event.event_id,
            sequence_number=event.sequence_number,
            timestamp=event.timestamp,
            event_type=mapped_type,
            source=mapped_source,
            provider_id=self.provider_id,
            connection_id=connection_id,
            status=event.status,
            text_delta=text_delta,
            tool_name=event.tool_name,
            tool_call_id=event.tool_call_id,
            redacted_payload={
                "antigravity_event_type": event.event_type,
                **event.redacted_payload,
            },
            telemetry=self._map_telemetry(event.telemetry) if event.telemetry else None,
        )

    @staticmethod
    def _build_session_reference(
        base: EngineSessionReference | None,
        conversation_id: str | None,
    ) -> EngineSessionReference | None:
        """Return updated session reference preserving AI Village ``session_id``."""
        if base is None and conversation_id is None:
            return None
        return EngineSessionReference(
            session_id=base.session_id if base else None,
            provider_conversation_id=conversation_id or (base.provider_conversation_id if base else None),
            binding_source="provider_cli" if conversation_id else (base.binding_source if base else "none"),
        )

    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        """Execute a request via Antigravity CLI streaming or handshake mode."""
        connection_id = connection.connection_id if connection else request.connection_id
        if connection is not None and not connection.enabled:
            return self.build_blocked_result(
                request,
                EngineError(
                    error_code="CONNECTION_DISABLED",
                    error_source="credential",
                    message=f"Connection '{connection.connection_id}' is disabled.",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection.connection_id,
                ),
                connection_id=connection_id,
            )

        capabilities = self.get_capabilities(connection)
        cap_error = self.validate_request_capabilities(
            request,
            capabilities,
            connection_id=connection_id,
        )
        if cap_error is not None:
            return self.build_blocked_result(
                request,
                cap_error,
                capabilities=capabilities,
                connection_id=connection_id,
            )

        workspace_path = request.workspace_path or self._default_workspace_path
        if not workspace_path or not workspace_path.strip():
            return self.build_blocked_result(
                request,
                EngineError(
                    error_code="EXECUTION_BLOCKED",
                    error_source="validation",
                    message="Antigravity CLI requires a valid workspace_path.",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                ),
                capabilities=capabilities,
                connection_id=connection_id,
            )

        model_id = request.model_id or (connection.default_model_id if connection else None)
        combined_prompt = self._build_prompt(request)
        required_caps = set(self.required_capabilities_for_request(request))
        use_streaming = (
            on_event is not None
            or "streaming" in required_caps
            or "native_tool_execution" in required_caps
            or "workspace_access" in required_caps
            or "session_continuation" in required_caps
            or request.execution_mode in ("accept-edits", "plan")
        )

        if use_streaming:
            return self._execute_streaming(
                request=request,
                prompt=combined_prompt,
                workspace_path=workspace_path,
                model_id=model_id,
                connection_id=connection_id,
                capabilities=capabilities,
                on_event=on_event,
            )

        return self._execute_handshake(
            request=request,
            prompt=combined_prompt,
            workspace_path=workspace_path,
            model_id=model_id,
            connection_id=connection_id,
            capabilities=capabilities,
        )

    def _execute_streaming(
        self,
        *,
        request: EngineRequest,
        prompt: str,
        workspace_path: str,
        model_id: str | None,
        connection_id: str | None,
        capabilities: EngineCapabilities,
        on_event: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        """Run ``stream_task`` and translate events and outcome into ``EngineResult``."""
        try:
            stream_req = AntigravityStreamRequest(
                prompt=prompt,
                workspace_path=workspace_path,
                timeout_seconds=request.timeout_seconds,
                require_sandbox=True,
                execution_mode="plan" if request.execution_mode == "plan" else "accept-edits",
                model=model_id,
                conversation_id=(
                    request.session_reference.provider_conversation_id
                    if request.session_reference
                    else None
                ),
            )
        except ValidationError as exc:
            return self.build_blocked_result(
                request,
                EngineError(
                    error_code="UNSUPPORTED_ARGUMENT",
                    error_source="validation",
                    message=f"Antigravity streaming request validation failed: {exc}",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                ),
                capabilities=capabilities,
                connection_id=connection_id,
                model_id=model_id,
            )

        def _forward_event(ev: AntigravityStreamEvent) -> None:
            if on_event is not None:
                on_event(self._map_stream_event(ev, connection_id))

        stream_res: AntigravityStreamResult = self._adapter.stream_task(
            stream_req,
            on_event=_forward_event if on_event is not None else None,
        )
        mapped_events = [self._map_stream_event(ev, connection_id) for ev in stream_res.events]
        return EngineResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            connection_id=connection_id,
            model_id=stream_res.model_id or model_id,
            platform_kind=self.platform_kind,
            status=cast(EngineExecutionStatus, stream_res.status),
            response_text=stream_res.response_text,
            observed_tool_names=list(stream_res.observed_tool_names),
            session_reference=self._build_session_reference(
                request.session_reference,
                stream_res.conversation_id,
            ),
            started_at=stream_res.started_at,
            finished_at=stream_res.finished_at,
            duration_ms=stream_res.duration_ms,
            telemetry=self._map_telemetry(stream_res.telemetry),
            events=mapped_events,
            error=self._map_error(stream_res.error, connection_id),
            capability_evidence=capabilities.to_evidence_map(),
        )

    def _execute_handshake(
        self,
        *,
        request: EngineRequest,
        prompt: str,
        workspace_path: str,
        model_id: str | None,
        connection_id: str | None,
        capabilities: EngineCapabilities,
    ) -> EngineResult:
        """Run ``handshake`` for single-turn text generation without streaming overhead."""
        try:
            hs_req = AntigravityHandshakeRequest(
                prompt=prompt,
                workspace_path=workspace_path,
                timeout_seconds=request.timeout_seconds,
                sandbox=False,
                model=model_id,
            )
        except ValidationError as exc:
            return self.build_blocked_result(
                request,
                EngineError(
                    error_code="UNSUPPORTED_ARGUMENT",
                    error_source="validation",
                    message=f"Antigravity handshake request validation failed: {exc}",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                ),
                capabilities=capabilities,
                connection_id=connection_id,
                model_id=model_id,
            )

        hs_res: AntigravityHandshakeResult = self._adapter.handshake(hs_req)
        mapped_telemetry = self._map_telemetry(hs_res.telemetry)
        events = [
            EngineEvent(
                event_id=f"{request.request_id}-ev-1",
                sequence_number=1,
                timestamp=hs_res.started_at,
                event_type="execution_started",
                source="adapter",
                provider_id=self.provider_id,
                connection_id=connection_id,
                status="STARTED",
                redacted_payload={"command_summary": hs_res.command_summary},
            ),
            EngineEvent(
                event_id=f"{request.request_id}-ev-2",
                sequence_number=2,
                timestamp=hs_res.finished_at,
                event_type="execution_completed" if hs_res.status == "SUCCEEDED" else "provider_error",
                source="provider",
                provider_id=self.provider_id,
                connection_id=connection_id,
                status="COMPLETED" if hs_res.status == "SUCCEEDED" else "FAILED",
                telemetry=mapped_telemetry if hs_res.status == "SUCCEEDED" else None,
            ),
        ]
        return EngineResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            connection_id=connection_id,
            model_id=hs_res.model_id or model_id,
            platform_kind=self.platform_kind,
            status=cast(EngineExecutionStatus, hs_res.status),
            response_text=hs_res.response_text,
            session_reference=self._build_session_reference(
                request.session_reference,
                hs_res.conversation_id,
            ),
            started_at=hs_res.started_at,
            finished_at=hs_res.finished_at,
            duration_ms=hs_res.duration_ms,
            telemetry=mapped_telemetry,
            events=events,
            error=self._map_error(hs_res.error, connection_id),
            capability_evidence=capabilities.to_evidence_map(),
        )
