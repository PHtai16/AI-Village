"""Unit tests covering all 20 scenarios of the P02.06 Universal Engine Contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import pytest
import requests

from engines import (
    AntigravityEngineAdapter,
    ConnectionRegistry,
    EngineCapabilities,
    EngineConnectionRef,
    EngineEvent,
    EngineRegistry,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
    EnvCredentialResolver,
    GoogleGeminiAdapter,
    ProviderConnectionGroup,
)
from engines.antigravity import (
    AntigravityAdapter,
    AntigravityCapabilities,
    AntigravityDiscoveryResult,
    AntigravityError,
    AntigravityHandshakeResult,
    AntigravityProcessManager,
    AntigravitySessionManager,
    AntigravityStreamEvent,
    AntigravityStreamResult,
    AntigravityTelemetry,
)


@dataclass
class _MockHttpResponse:
    """Minimal mock mimicking ``requests.Response`` for Gemini unit tests."""

    status_code: int
    payload: dict[str, Any] | str
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}

    @property
    def text(self) -> str:
        """Serialize payload cleanly as text."""
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload)

    def json(self) -> Any:
        """Parse payload as JSON or raise ``ValueError`` when invalid."""
        if isinstance(self.payload, str):
            return json.loads(self.payload)
        return self.payload


class _FakeAntigravityBackend:
    """Deterministic fake ``AntigravityAdapter`` preserving P02.01-P02.05 contracts."""

    def __init__(self, *, cli_found: bool = True) -> None:
        self.cli_found = cli_found
        self.handshake_calls: list[Any] = []
        self.stream_calls: list[Any] = []
        self.next_error: AntigravityError | None = None

    def discover(self) -> AntigravityDiscoveryResult:
        """Return simulated discovery result."""
        if not self.cli_found:
            return AntigravityDiscoveryResult(
                found=False,
                source="not_found",
                error="agy executable missing in unit fixture",
            )
        return AntigravityDiscoveryResult(
            found=True,
            executable_path="C:/fake/bin/agy.exe",
            source="explicit",
        )

    def probe(self, timeout_seconds: int = 15) -> AntigravityCapabilities:
        """Return simulated probed capabilities."""
        _ = timeout_seconds
        return AntigravityCapabilities(
            executable_path="C:/fake/bin/agy.exe",
            version="1.5.15",
            supports_print_mode=True,
            supports_json_output=True,
            supports_stream_json_flag=True,
            supports_print_timeout=True,
            supports_sandbox_flag=True,
            supports_mode_flag=True,
            supports_models_subcommand=True,
            supports_conversation_flag=True,
            supports_settings_flag=True,
            probe_exit_code=0,
        )

    def handshake(self, request: Any) -> AntigravityHandshakeResult:
        """Simulate a single-turn Antigravity handshake response."""
        self.handshake_calls.append(request)
        now = datetime.now(timezone.utc)
        if self.next_error is not None:
            return AntigravityHandshakeResult(
                status="FAILED",
                executable_path="C:/fake/bin/agy.exe",
                version="1.5.15",
                command_summary="agy --output-format json",
                started_at=now,
                finished_at=now,
                duration_ms=42,
                exit_code=1,
                model_id=request.model,
                telemetry=AntigravityTelemetry(
                    duration_ms=42,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                ),
                error=self.next_error,
            )
        return AntigravityHandshakeResult(
            status="SUCCEEDED",
            executable_path="C:/fake/bin/agy.exe",
            version="1.5.15",
            command_summary="agy --output-format json",
            started_at=now,
            finished_at=now,
            duration_ms=85,
            exit_code=0,
            response_text="ANTIGRAVITY_COMMON_OK",
            provider_status="SUCCESS",
            conversation_id="conv-agy-123",
            model_id=request.model,
            telemetry=AntigravityTelemetry(
                duration_ms=85,
                input_tokens=120,
                output_tokens=15,
                total_tokens=135,
                cache_read_tokens=40,
                telemetry_status="VERIFIED_MEASURED",
            ),
        )

    def stream_task(
        self,
        request: Any,
        *,
        on_event: Any = None,
    ) -> AntigravityStreamResult:
        """Simulate streaming execution including observed tool events."""
        self.stream_calls.append(request)
        now = datetime.now(timezone.utc)
        telemetry = AntigravityTelemetry(
            duration_ms=110,
            input_tokens=200,
            output_tokens=25,
            total_tokens=225,
            telemetry_status="VERIFIED_MEASURED",
        )
        raw_events = [
            AntigravityStreamEvent(
                event_id="ev-1",
                sequence_number=1,
                timestamp=now,
                event_type="execution_started",
                source="provider_cli",
                conversation_id="conv-agy-stream-1",
                status="STARTED",
            ),
            AntigravityStreamEvent(
                event_id="ev-2",
                sequence_number=2,
                timestamp=now,
                event_type="tool_call_started",
                source="provider_cli",
                conversation_id="conv-agy-stream-1",
                tool_name="read_file",
                status="STARTED",
            ),
            AntigravityStreamEvent(
                event_id="ev-3",
                sequence_number=3,
                timestamp=now,
                event_type="text_delta",
                source="provider_cli",
                conversation_id="conv-agy-stream-1",
                status="IN_PROGRESS",
                redacted_payload={"text_delta": "STREAM_CHUNK_OK"},
            ),
            AntigravityStreamEvent(
                event_id="ev-4",
                sequence_number=4,
                timestamp=now,
                event_type="execution_completed",
                source="provider_cli",
                conversation_id="conv-agy-stream-1",
                status="COMPLETED",
                telemetry=telemetry,
            ),
        ]
        if on_event is not None:
            for item in raw_events:
                on_event(item)
        return AntigravityStreamResult(
            status="SUCCEEDED",
            executable_path="C:/fake/bin/agy.exe",
            version="1.5.15",
            command_summary="agy --output-format stream-json",
            started_at=now,
            finished_at=now,
            duration_ms=110,
            exit_code=0,
            provider_status="SUCCESS",
            conversation_id="conv-agy-stream-1",
            model_id=request.model,
            response_text="STREAM_CHUNK_OK",
            telemetry=telemetry,
            events=raw_events,
            observed_tool_names=["read_file"],
            completion_event_received=True,
        )


@pytest.fixture
def gemini_connections() -> ProviderConnectionGroup:
    """Provide a validated two-connection Gemini BYOK group matching docs/architecture/vision.md."""
    return ProviderConnectionGroup(
        provider_id="google_gemini",
        connections=[
            EngineConnectionRef(
                connection_id="gemini_primary",
                provider_id="google_gemini",
                adapter_id="gemini_api",
                display_name="Primary Gemini Connection",
                project_ref="google_project_a",
                credential_ref="secret_ref_primary",
                default_model_id="gemini-2.5-flash",
                enabled=True,
                priority=10,
                quota_scope_note="project_shared",
            ),
            EngineConnectionRef(
                connection_id="gemini_secondary",
                provider_id="google_gemini",
                adapter_id="gemini_api",
                display_name="Secondary Gemini Connection",
                project_ref="google_project_a",
                credential_ref="secret_ref_secondary",
                default_model_id="gemini-2.5-pro",
                enabled=False,
                priority=20,
                quota_scope_note="project_shared",
            ),
        ],
    )


@pytest.fixture
def unit_secret_resolver() -> EnvCredentialResolver:
    """Return an isolated in-memory environment resolver with synthetic dummy secrets."""
    return EnvCredentialResolver(
        ref_to_env={
            "secret_ref_primary": "GEMINI_PRIMARY_DUMMY_KEY",
            "secret_ref_secondary": "GEMINI_SECONDARY_DUMMY_KEY",
            "secret_ref_missing": "UNSET_ENV_VAR",
        },
        environ={
            "GEMINI_PRIMARY_DUMMY_KEY": "dummy_test_secret_alpha_998877",
            "GEMINI_SECONDARY_DUMMY_KEY": "your_api_key_here",
        },
    )


def _neutral_application_caller(registry: EngineRegistry, request: EngineRequest) -> EngineResult:
    """Neutral application helper executing any provider purely through the shared contract."""
    return registry.execute(request)


def test_request_validation_and_secret_rejection() -> None:
    """Scenarios 1 & 17: EngineRequest validates inputs and blocks raw credentials."""
    req = EngineRequest(
        provider_id="google_gemini",
        prompt="Summarize health status.",
        model_id="gemini-2.5-flash",
    )
    assert req.workspace_path is None
    assert req.request_id.startswith("req-")

    with pytest.raises(ValidationError):
        EngineRequest(provider_id="google_gemini", prompt="   ")

    with pytest.raises(ValidationError):
        EngineRequest(
            provider_id="google_gemini",
            prompt="Hello",
            metadata={"api_key": "dummy_secret_value"},
        )

    with pytest.raises(ValidationError):
        EngineRequest(
            provider_id="google_gemini",
            prompt="Hello",
            metadata={"note": "sk-" + "dummyUnitTestSecret1234567890"},
        )


def test_capability_matrix_and_model_api_boundary() -> None:
    """Scenarios 5 & 6: Model API cannot claim native execution and fails closed."""
    with pytest.raises(ValidationError):
        EngineCapabilities(
            provider_id="google_gemini",
            adapter_id="gemini_api",
            platform_kind="model_api",
            native_tool_execution="SUPPORTED",
        )

    with pytest.raises(ValidationError):
        EngineCapabilities(
            provider_id="google_gemini",
            adapter_id="gemini_api",
            platform_kind="model_api",
            workspace_access="SUPPORTED",
        )

    called_http = False

    def _never_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal called_http
        called_http = True
        raise AssertionError("HTTP transport must not run when capability is unsupported.")

    gemini = GoogleGeminiAdapter(
        credential_resolver=EnvCredentialResolver(environ={"GEMINI_API_KEY": "dummy_val_123"}),
        http_post=_never_called,
        default_credential_ref="env:GEMINI_API_KEY",
    )
    caps = gemini.get_capabilities()
    assert caps.platform_kind == "model_api"
    assert caps.native_tool_execution == "UNSUPPORTED"
    assert caps.workspace_access == "UNSUPPORTED"

    tool_req = EngineRequest(
        provider_id="google_gemini",
        model_id="gemini-2.5-flash",
        prompt="Read package.json and write proof.txt",
        capabilities_required=["native_tool_execution", "workspace_access"],
        execution_mode="accept-edits",
    )
    blocked = gemini.execute(tool_req)
    assert not called_http
    assert blocked.status == "BLOCKED"
    assert blocked.business_verification_status == "UNVERIFIED"
    assert blocked.error is not None
    assert blocked.error.error_code == "TOOL_UNSUPPORTED_BY_PROVIDER"
    assert blocked.observed_tool_names == []

    local_req = EngineRequest(
        provider_id="google_gemini",
        model_id="gemini-2.5-flash",
        prompt="Keep data local only",
        data_locality="local_only",
    )
    blocked_local = gemini.execute(local_req)
    assert blocked_local.status == "BLOCKED"
    assert blocked_local.error is not None
    assert blocked_local.error.error_code == "DATA_BOUNDARY_VIOLATION"


def test_gemini_result_events_telemetry_and_tool_proposals(
    gemini_connections: ProviderConnectionGroup,
    unit_secret_resolver: EnvCredentialResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenarios 2, 3, 4, 15, 17, 19: Result/event/telemetry normalization and secret safety."""
    captured_requests: list[dict[str, Any]] = []

    def _fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: int) -> Any:
        captured_requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _MockHttpResponse(
            status_code=200,
            payload={
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": '{"ok": true}'},
                                {"functionCall": {"name": "read_file", "args": {"path": "package.json"}}},
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 14,
                    "candidatesTokenCount": 8,
                    "totalTokenCount": 22,
                },
            },
        )

    adapter = GoogleGeminiAdapter(
        credential_resolver=unit_secret_resolver,
        http_post=_fake_post,
    )
    registry = EngineRegistry()
    registry.register_adapter(adapter)
    registry.connections.register_group(gemini_connections)

    emitted: list[EngineEvent] = []
    caplog.set_level(logging.INFO)
    req = EngineRequest(
        connection_id="gemini_primary",
        prompt="Inspect package.json via structured tool proposal",
        context="Return JSON only.",
        execution_mode="structured",
        tool_declarations=[{"name": "read_file", "description": "Read a workspace file."}],
    )
    res = registry.execute(req, on_event=emitted.append)

    assert res.status == "SUCCEEDED"
    assert res.business_verification_status == "UNVERIFIED"
    assert res.provider_id == "google_gemini"
    assert res.connection_id == "gemini_primary"
    assert res.model_id == "gemini-2.5-flash"
    assert res.structured_output == {"ok": True}
    assert res.observed_tool_names == []
    assert len(res.tool_call_proposals) == 1
    assert res.tool_call_proposals[0].tool_name == "read_file"
    assert res.tool_call_proposals[0].executed is False

    # No fake text_delta for non-streaming REST call; events must record started, proposal, completed.
    event_types = [ev.event_type for ev in emitted]
    assert event_types == ["execution_started", "tool_call_proposed", "execution_completed"]
    assert all(ev.event_type != "tool_execution_observed" for ev in emitted)

    # Telemetry leaves missing optional metrics as None rather than converting them to 0.
    assert res.telemetry.telemetry_status == "VERIFIED_MEASURED"
    assert res.telemetry.telemetry_source == "provider_api"
    assert res.telemetry.input_tokens == 14
    assert res.telemetry.output_tokens == 8
    assert res.telemetry.total_tokens == 22
    assert res.telemetry.thinking_tokens is None
    assert res.telemetry.cached_tokens is None
    assert res.telemetry.estimated_cost_usd is None
    assert res.telemetry.actual_cost_usd is None

    # Secret never appears in URL, logs, serialized result, or repr(resolver).
    assert len(captured_requests) == 1
    assert "dummy_test_secret_alpha_998877" not in captured_requests[0]["url"]
    assert captured_requests[0]["headers"]["x-goog-api-key"] == "dummy_test_secret_alpha_998877"
    assert "dummy_test_secret_alpha_998877" not in caplog.text
    assert "dummy_test_secret_alpha_998877" not in res.model_dump_json()
    assert "dummy_test_secret_alpha_998877" not in repr(unit_secret_resolver)


def test_gemini_null_telemetry_and_error_normalization(
    unit_secret_resolver: EnvCredentialResolver,
) -> None:
    """Scenarios 8, 9, 10, 11, 12, 13, 14, 15, 17: Null telemetry and full error mapping."""
    base_conn = EngineConnectionRef(
        connection_id="gemini_primary",
        provider_id="google_gemini",
        credential_ref="secret_ref_primary",
        default_model_id="gemini-2.5-flash",
    )

    # Scenario 15: usageMetadata absent -> UNAVAILABLE_FROM_PROVIDER with None fields (never 0).
    no_usage_adapter = GoogleGeminiAdapter(
        credential_resolver=unit_secret_resolver,
        http_post=lambda *a, **k: _MockHttpResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
        ),
    )
    ok_no_usage = no_usage_adapter.execute(EngineRequest(prompt="Hi"), connection=base_conn)
    assert ok_no_usage.status == "SUCCEEDED"
    assert ok_no_usage.telemetry.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
    assert ok_no_usage.telemetry.input_tokens is None
    assert ok_no_usage.telemetry.output_tokens is None
    assert ok_no_usage.telemetry.total_tokens is None

    # Scenario 8: Invalid model local validation.
    bad_model_res = no_usage_adapter.execute(
        EngineRequest(prompt="Hi", model_id="bad/../model?key=x"),
        connection=EngineConnectionRef(
            connection_id="gemini_no_default",
            provider_id="google_gemini",
            credential_ref="secret_ref_primary",
        ),
    )
    assert bad_model_res.status == "FAILED"
    assert bad_model_res.error is not None
    assert bad_model_res.error.error_code == "INVALID_MODEL"

    # Scenario 9: Missing or placeholder credential fails closed.
    missing_cred_res = no_usage_adapter.execute(
        EngineRequest(prompt="Hi", model_id="gemini-2.5-flash"),
        connection=EngineConnectionRef(
            connection_id="gemini_secondary",
            provider_id="google_gemini",
            credential_ref="secret_ref_secondary",
        ),
    )
    assert missing_cred_res.status == "BLOCKED"
    assert missing_cred_res.error is not None
    assert missing_cred_res.error.error_code == "CREDENTIAL_UNAVAILABLE"

    # Scenarios 10, 11, 12, 8 (HTTP 404): HTTP status error classification + secret scrubbing.
    http_cases: list[tuple[int, dict[str, Any] | str, dict[str, str], str, bool, float | None]] = [
        (401, {"error": {"message": "Unauthorized dummy_test_secret_alpha_998877"}}, {}, "AUTH_ERROR", False, None),
        (429, {"error": {"message": "Rate limit hit; try again later"}}, {"Retry-After": "12"}, "RATE_LIMITED", True, 12.0),
        (429, {"error": {"message": "You exceeded your current quota for billing project."}}, {}, "QUOTA_EXCEEDED", False, None),
        (404, {"error": {"message": "Requested model was not found."}}, {}, "INVALID_MODEL", False, None),
    ]
    for status_code, payload, hdrs, expected_code, expected_retry, expected_delay in http_cases:
        err_adapter = GoogleGeminiAdapter(
            credential_resolver=unit_secret_resolver,
            http_post=lambda *a, sc=status_code, pl=payload, hd=hdrs, **k: _MockHttpResponse(sc, pl, hd),
        )
        res = err_adapter.execute(EngineRequest(prompt="Hi"), connection=base_conn)
        assert res.status == "FAILED"
        assert res.error is not None
        assert res.error.error_code == expected_code
        assert res.error.retryable is expected_retry
        assert res.error.retry_after_seconds == expected_delay
        assert "dummy_test_secret_alpha_998877" not in res.error.message
        assert "dummy_test_secret_alpha_998877" not in res.error.redacted_details

    # Scenarios 13 & 14: Timeout and Network failure.
    timeout_adapter = GoogleGeminiAdapter(
        credential_resolver=unit_secret_resolver,
        http_post=lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("dummy_test_secret_alpha_998877 timeout")),
    )
    timeout_res = timeout_adapter.execute(EngineRequest(prompt="Hi"), connection=base_conn)
    assert timeout_res.status == "TIMED_OUT"
    assert timeout_res.error is not None
    assert timeout_res.error.error_code == "TIMED_OUT"
    assert "dummy_test_secret_alpha_998877" not in timeout_res.error.message

    net_adapter = GoogleGeminiAdapter(
        credential_resolver=unit_secret_resolver,
        http_post=lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("DNS down")),
    )
    net_res = net_adapter.execute(EngineRequest(prompt="Hi"), connection=base_conn)
    assert net_res.status == "FAILED"
    assert net_res.error is not None
    assert net_res.error.error_code == "NETWORK_ERROR"
    assert net_res.error.retryable is True


def test_multi_connection_registry_provider_switching_and_p02_regression(
    gemini_connections: ProviderConnectionGroup,
    unit_secret_resolver: EnvCredentialResolver,
    tmp_path: Path,
) -> None:
    """Scenarios 7, 16, 18, 19, 20: Multi-connection, neutral switching, Antigravity compat, regression."""
    # Scenario 16: Reject raw secret in connection fields or endpoint URL query string.
    with pytest.raises(ValidationError):
        EngineConnectionRef(
            connection_id="bad_conn",
            provider_id="google_gemini",
            credential_ref="sk-" + "dummyRawKeyRejected1234567890",
        )
    with pytest.raises(ValidationError):
        EngineConnectionRef(
            connection_id="bad_url",
            provider_id="google_gemini",
            endpoint_url="https://generativelanguage.googleapis.com?key=secret123",
        )

    registry = EngineRegistry()
    registry.connections.register_group(gemini_connections)
    assert registry.connections.has_shared_project_scope("gemini_primary", "gemini_secondary") is True
    assert registry.connections.has_independent_quota_proof("gemini_primary", "gemini_secondary") is False
    safe_summary = gemini_connections.connections[0].to_safe_summary()
    assert safe_summary["credential_ref_summary"].startswith("ref:")

    fake_agy = _FakeAntigravityBackend()
    agy_wrapper = AntigravityEngineAdapter(
        adapter=fake_agy,  # type: ignore[arg-type]
        default_workspace_path=str(tmp_path),
    )
    gemini_wrapper = GoogleGeminiAdapter(
        credential_resolver=unit_secret_resolver,
        http_post=lambda *a, **k: _MockHttpResponse(
            200,
            {
                "modelVersion": "gemini-2.5-flash",
                "candidates": [{"content": {"parts": [{"text": "GEMINI_COMMON_OK"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 4, "totalTokenCount": 9},
            },
        ),
    )
    registry.register_adapter(agy_wrapper)
    registry.register_adapter(gemini_wrapper)

    # Scenario 7: Unknown or disabled connection returns fail-closed BLOCKED without cross-fallback.
    missing_res = registry.execute(EngineRequest(connection_id="gemini_unknown", prompt="Hello"))
    assert missing_res.status == "BLOCKED"
    assert missing_res.error is not None
    assert missing_res.error.error_code == "CONNECTION_NOT_FOUND"

    disabled_res = registry.execute(EngineRequest(connection_id="gemini_secondary", prompt="Hello"))
    assert disabled_res.status == "BLOCKED"
    assert disabled_res.error is not None
    assert disabled_res.error.error_code == "CONNECTION_DISABLED"

    # Scenario 18 & 19: Same neutral caller switches from Antigravity to Gemini without touching core/.
    res_agy = _neutral_application_caller(
        registry,
        EngineRequest(provider_id="antigravity", prompt="Echo AGY", execution_mode="text"),
    )
    res_gem = _neutral_application_caller(
        registry,
        EngineRequest(connection_id="gemini_primary", prompt="Echo Gemini", execution_mode="text"),
    )
    assert res_agy.provider_id == "antigravity"
    assert res_agy.response_text == "ANTIGRAVITY_COMMON_OK"
    assert res_gem.provider_id == "google_gemini"
    assert res_gem.response_text == "GEMINI_COMMON_OK"

    # Streaming + native tool + session continuation path through AntigravityEngineAdapter.
    streamed_events: list[EngineEvent] = []
    res_agy_stream = registry.execute(
        EngineRequest(
            provider_id="antigravity",
            prompt="Read file",
            execution_mode="accept-edits",
            capabilities_required=["streaming", "native_tool_execution", "workspace_access"],
            session_reference=EngineSessionReference(
                session_id="sess_a",
                provider_conversation_id="conv-prev",
                binding_source="provider_cli",
            ),
        ),
        on_event=streamed_events.append,
    )
    assert res_agy_stream.status == "SUCCEEDED"
    assert res_agy_stream.observed_tool_names == ["read_file"]
    assert any(ev.event_type == "tool_execution_observed" for ev in streamed_events)
    assert res_agy_stream.session_reference is not None
    assert res_agy_stream.session_reference.session_id == "sess_a"
    assert res_agy_stream.session_reference.provider_conversation_id == "conv-agy-stream-1"

    # Scenario 20: Existing P02.01-P02.05 components remain intact and usable alongside P02.06.
    legacy_adapter = AntigravityAdapter()
    proc_mgr = AntigravityProcessManager(legacy_adapter)
    sess_mgr = AntigravitySessionManager(adapter=legacy_adapter, process_manager=proc_mgr)
    assert proc_mgr.list_executions() == []
    assert sess_mgr.list_sessions() == []
