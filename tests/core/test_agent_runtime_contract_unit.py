"""Unit and contract tests for P03.03 AgentRuntime abstraction and CustomAgentRuntime."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

import pytest
from pydantic import ValidationError

from core.runtime import (
    AgentRuntime,
    AgentRuntimeArtifactRef,
    AgentRuntimeError,
    AgentRuntimeEvent,
    AgentRuntimeInfo,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    AgentRuntimeTelemetry,
    AgentWorkflowRunner,
    ArtifactAcceptanceSpec,
    CustomAgentRuntime,
    ExecutionRouting,
    WorkspaceScope,
    execute_agent_workflow,
)
from engines import (
    EngineAdapter,
    EngineArtifactVerificationResult,
    EngineCapabilities,
    EngineConnectionRef,
    EngineError,
    EngineEvent,
    EnginePlatformKind,
    EngineRegistry,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
    EngineTelemetry,
    compute_sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeAgentRuntime:
    """Pure in-memory FakeAgentRuntime independent of Antigravity and CrewAI."""

    def __init__(
        self,
        *,
        runtime_id: str = "fake.runtime_v1",
        simulate_mode: str = "delivered",
    ) -> None:
        self.runtime_id = runtime_id
        self.simulate_mode = simulate_mode
        self.received_requests: list[AgentRuntimeRequest] = []

    def get_runtime_info(self) -> AgentRuntimeInfo:
        """Return metadata claiming ``fake`` runtime kind."""
        return AgentRuntimeInfo(
            runtime_id=self.runtime_id,
            runtime_kind="fake",
            version="0.0.1",
            supports_streaming_events=True,
            supports_independent_qa=True,
            supports_workflow_cancellation=False,
            cancellation_boundary_note="Fake test runtime without real subprocess management.",
        )

    def execute(
        self,
        request: AgentRuntimeRequest,
        *,
        on_event: Callable[[AgentRuntimeEvent], None] | None = None,
    ) -> AgentRuntimeResult:
        """Simulate compliant, failing, lying-QA, or crashing runtime behaviors."""
        self.received_requests.append(request)
        if self.simulate_mode == "raise_exception":
            leaked = "sk-" + ("x" * 24)
            raise RuntimeError(f"Simulated crash carrying secret {leaked}")

        now = datetime.now(timezone.utc)
        ev = AgentRuntimeEvent(
            invocation_id=request.invocation_id,
            runtime_id=self.runtime_id,
            sequence=1,
            stage="worker_execute",
            actor_id="fake.worker",
            actor_role="worker",
            logical_session_id=request.worker_session_id or "fake-worker-sess",
            provider_id=request.routing.provider_id,
            provider_conversation_id="fake-conv-1",
            engine_event=None,
        )
        if on_event is not None:
            on_event(ev)

        if self.simulate_mode == "lie_delivered_without_qa_pass":
            bad_qa = EngineArtifactVerificationResult(
                status="VERIFIED_FAIL",
                engine_execution_status="SUCCEEDED",
                artifact_relpath=request.acceptance.artifact_relpath,
                engine_succeeded=True,
                artifact_exists=False,
                within_workspace=True,
                content_verified=False,
                hash_verified=False,
                source_integrity_verified=True,
                business_verified=False,
                failure_reasons=["Artifact missing on disk."],
            )
            return AgentRuntimeResult(
                runtime_id=self.runtime_id,
                invocation_id=request.invocation_id,
                correlation_id="corr-fake-lie",
                status="DELIVERED",
                technical_execution_succeeded=True,
                business_verified=True,
                actors_executed=["fake.master", "fake.worker"],
                orchestrator_session_id=request.orchestrator_session_id or "fake-m",
                worker_session_id=request.worker_session_id or "fake-w",
                provider_conversation_ids=["fake-conv-1"],
                observed_tool_names=["write_file"],
                qa_verification=bad_qa,
                final_summary="False claim of delivery.",
                telemetry=AgentRuntimeTelemetry(
                    total_duration_ms=10,
                    provider_duration_ms=0,
                    engine_call_count=0,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=[ev],
                started_at=now,
                finished_at=now,
                duration_ms=10,
            )

        sha = compute_sha256_bytes((request.acceptance.expected_content or "ok").encode("utf-8"))
        pass_qa = EngineArtifactVerificationResult(
            status="VERIFIED_PASS",
            engine_execution_status="SUCCEEDED",
            artifact_relpath=request.acceptance.artifact_relpath,
            engine_succeeded=True,
            artifact_exists=True,
            within_workspace=True,
            content_verified=True,
            hash_verified=True,
            source_integrity_verified=True,
            business_verified=True,
            actual_sha256=sha,
        )
        return AgentRuntimeResult(
            runtime_id=self.runtime_id,
            invocation_id=request.invocation_id,
            correlation_id="corr-fake-ok",
            status="DELIVERED",
            technical_execution_succeeded=True,
            business_verified=True,
            actors_executed=["fake.master", "fake.worker"],
            orchestrator_session_id=request.orchestrator_session_id or "fake-m",
            worker_session_id=request.worker_session_id or "fake-w",
            provider_conversation_ids=["fake-conv-1"],
            observed_tool_names=["read_file", "write_file"],
            artifact=AgentRuntimeArtifactRef(
                artifact_relpath=request.acceptance.artifact_relpath,
                exists_on_disk=True,
                sha256=sha,
                qa_status="VERIFIED_PASS",
            ),
            qa_verification=pass_qa,
            final_summary="Fake runtime completed verified run.",
            telemetry=AgentRuntimeTelemetry(
                total_duration_ms=12,
                provider_duration_ms=0,
                engine_call_count=0,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
            events=[ev],
            started_at=now,
            finished_at=now,
            duration_ms=12,
        )


class _ScriptedFakeEngineAdapter(EngineAdapter):
    """Deterministic EngineAdapter backing CustomAgentRuntime in offline tests."""

    def __init__(
        self,
        handler: Callable[[EngineRequest, int, Callable[[EngineEvent], None] | None], EngineResult],
    ) -> None:
        self._handler = handler
        self.requests: list[EngineRequest] = []

    @property
    def provider_id(self) -> str:
        return "antigravity"

    @property
    def adapter_id(self) -> str:
        return "antigravity_cli"

    @property
    def platform_kind(self) -> EnginePlatformKind:
        return "agent_execution_platform"

    def get_capabilities(
        self,
        connection: EngineConnectionRef | None = None,
    ) -> EngineCapabilities:
        _ = connection
        return EngineCapabilities(
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            platform_kind=self.platform_kind,
            text_generation="SUPPORTED",
            structured_output="UNSUPPORTED",
            streaming="SUPPORTED",
            tool_calling_proposal="UNSUPPORTED",
            native_tool_execution="SUPPORTED",
            workspace_access="SUPPORTED",
            session_continuation="SUPPORTED",
            cancellation="SUPPORTED",
            usage_telemetry="SUPPORTED",
            model_discovery="NOT_VERIFIED",
        )

    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        _ = connection
        self.requests.append(request)
        return self._handler(request, len(self.requests), on_event)


def _engine_result(
    req: EngineRequest,
    *,
    status: str = "SUCCEEDED",
    response_text: str | None = None,
    conv_id: str | None = None,
    with_usage: bool = True,
    error: EngineError | None = None,
    on_event: Callable[[EngineEvent], None] | None = None,
) -> EngineResult:
    now = datetime.now(timezone.utc)
    ev = EngineEvent(
        event_id=f"{req.request_id}-ev-1",
        sequence_number=1,
        timestamp=now,
        event_type="execution_completed" if status == "SUCCEEDED" else "provider_error",
        source="provider",
        provider_id=req.provider_id or "antigravity",
        status="COMPLETED" if status == "SUCCEEDED" else "FAILED",
        redacted_payload={"conversation_id": conv_id} if conv_id else {},
    )
    if on_event is not None:
        on_event(ev)
    telemetry = (
        EngineTelemetry(
            duration_ms=25,
            input_tokens=100,
            output_tokens=20,
            thinking_tokens=5,
            cached_tokens=0,
            total_tokens=120,
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="provider_cli",
        )
        if with_usage
        else EngineTelemetry(
            duration_ms=25,
            input_tokens=None,
            output_tokens=None,
            thinking_tokens=None,
            cached_tokens=None,
            total_tokens=None,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        )
    )
    sid = req.session_reference.session_id if req.session_reference else None
    return EngineResult(
        request_id=req.request_id,
        provider_id=req.provider_id or "antigravity",
        adapter_id="antigravity_cli",
        platform_kind="agent_execution_platform",
        status=status,  # type: ignore[arg-type]
        business_verification_status="UNVERIFIED",
        response_text=response_text,
        observed_tool_names=["read_file", "write_file"] if status == "SUCCEEDED" else [],
        session_reference=EngineSessionReference(
            session_id=sid,
            provider_conversation_id=conv_id,
            binding_source="provider_cli" if conv_id else "adapter_logical",
        ),
        started_at=now,
        finished_at=now,
        duration_ms=25,
        telemetry=telemetry,
        events=[ev],
        error=error,
    )


@pytest.fixture
def runtime_dirs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """Create disjoint workspaces and non-default artifact fixture under tmp_path."""
    allowed_root = tmp_path / "runtime_root"
    master_ws = allowed_root / "master_ws"
    worker_ws = allowed_root / "worker_ws"
    master_ws.mkdir(parents=True)
    worker_ws.mkdir(parents=True)
    marker = "mk-rt-p0303-55aa"
    (worker_ws / "input_data.json").write_text(
        json.dumps({"name": "ai-village-runtime-pkg", "verification_marker": marker}) + "\n",
        encoding="utf-8",
    )
    expected_out = f"name=ai-village-runtime-pkg\nverification_marker={marker}\n"
    return allowed_root, master_ws, worker_ws, expected_out


def _build_request(
    allowed_root: Path,
    master_ws: Path,
    worker_ws: Path,
    expected_out: str,
    *,
    provider_id: str = "antigravity",
    orch_session: str | None = "sess-orch-rt-1",
    worker_session: str | None = "sess-work-rt-1",
    artifact_relpath: str = "output_report.txt",
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
) -> AgentRuntimeRequest:
    return AgentRuntimeRequest(
        user_objective="Inspect input_data.json without editing it and create output_report.txt.",
        workspace_scope=WorkspaceScope(
            allowed_root_path=str(allowed_root),
            orchestrator_workspace_path=str(master_ws),
            worker_workspace_path=str(worker_ws),
            permitted_read_paths=read_paths or ["input_data.json"],
            permitted_write_paths=write_paths or [artifact_relpath],
        ),
        routing=ExecutionRouting(provider_id=provider_id),
        acceptance=ArtifactAcceptanceSpec(
            artifact_relpath=artifact_relpath,
            expected_content=expected_out,
            required_substrings=["name=ai-village-runtime-pkg"],
        ),
        timeout_seconds=90,
        orchestrator_session_id=orch_session,
        worker_session_id=worker_session,
    )


def _plan_json() -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "task_summary": "Produce output_report.txt from input_data.json.",
            "target_worker_id": "it.artifact_worker",
            "worker_objective": "Read input_data.json and create output_report.txt.",
            "allowed_read_paths": ["input_data.json"],
            "allowed_write_paths": ["output_report.txt"],
            "forbidden_actions": ["Run shell commands", "Modify input_data.json"],
            "acceptance_criteria": ["input_data.json intact", "output_report.txt verified"],
            "artifact_expectation": {
                "required_substrings": ["name="],
                "expected_line_count": 2,
                "must_preserve_inputs": True,
            },
        }
    )


def _synth_json(sha_hex: str, relpath: str = "output_report.txt") -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "delivery_status": "DELIVERED",
            "qa_status_observed": "VERIFIED_PASS",
            "worker_status_observed": "SUCCEEDED",
            "artifact_relpath": relpath,
            "artifact_sha256": sha_hex,
            "final_summary": "Artifact output_report.txt passed independent SHA-256 verification.",
            "verified_facts": ["input_data.json unchanged", "SHA-256 verified"],
            "unverified_or_failed_reasons": [],
        }
    )


def test_boundary_architecture_no_crewai_or_concrete_coupling_in_contract_and_caller() -> None:
    """Criteria 2, 15: contract.py and application.py have zero concrete runtime or CrewAI imports."""
    forbidden_modules = {
        "crewai",
        "crewai_core",
        "core.orchestration",
        "core.runtime.custom_runtime",
        "engines.providers.antigravity",
        "engines.registry",
    }
    for rel in ("core/runtime/contract.py", "core/runtime/application.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for mod in imported:
            for forbidden in forbidden_modules:
                assert not mod.startswith(forbidden), f"{rel} illegally imports {mod}"

    for py_file in (REPO_ROOT / "core").rglob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        assert "crewai" not in src.lower(), f"Forbidden CrewAI reference in {py_file}"


def test_fake_runtime_swappable_in_application_caller(
    runtime_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Criteria 1, 2: FakeAgentRuntime conforms to AgentRuntime and runs via AgentWorkflowRunner."""
    allowed_root, master_ws, worker_ws, expected_out = runtime_dirs
    fake = _FakeAgentRuntime()
    assert isinstance(fake, AgentRuntime)

    runner = AgentWorkflowRunner(fake)
    assert runner.runtime_info.runtime_kind == "fake"
    events: list[AgentRuntimeEvent] = []
    req = _build_request(allowed_root, master_ws, worker_ws, expected_out)
    res = runner.run(req, on_event=events.append)

    assert res.status == "DELIVERED"
    assert res.business_verified is True
    assert res.runtime_id == "fake.runtime_v1"
    assert len(fake.received_requests) == 1
    assert len(events) == 1 and events[0].actor_id == "fake.worker"

    with pytest.raises(TypeError):
        AgentWorkflowRunner("not-a-runtime")  # type: ignore[arg-type]


def test_custom_runtime_through_caller_mapping_events_sessions_and_sha256(
    runtime_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Criteria 3-7, 16: CustomAgentRuntime swaps cleanly into same caller preserving all controls."""
    allowed_root, master_ws, worker_ws, expected_out = runtime_dirs
    expected_sha = compute_sha256_bytes(expected_out.encode("utf-8"))

    def _handler(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _engine_result(req, response_text=_plan_json(), conv_id="conv-rt-m1", on_event=cb)
        if idx == 2:
            raw = json.loads((Path(req.workspace_path or "") / "input_data.json").read_text(encoding="utf-8"))
            content = f"name={raw['name']}\nverification_marker={raw['verification_marker']}\n"
            (Path(req.workspace_path or "") / "output_report.txt").write_text(
                content,
                encoding="utf-8",
                newline="\n",
            )
            return _engine_result(req, response_text="Created output_report.txt.", conv_id="conv-rt-w1", on_event=cb)
        return _engine_result(
            req,
            response_text=_synth_json(expected_sha),
            conv_id="conv-rt-m2",
            on_event=cb,
        )

    adapter = _ScriptedFakeEngineAdapter(_handler)
    registry = EngineRegistry()
    registry.register_adapter(adapter)
    runtime: AgentRuntime = CustomAgentRuntime(registry=registry, allow_temp_workspace=True)
    assert isinstance(runtime, AgentRuntime)

    runner = AgentWorkflowRunner(runtime)
    assert runner.runtime_info.runtime_kind == "custom_orchestrator"
    assert runner.runtime_info.supports_workflow_cancellation is False

    streamed: list[AgentRuntimeEvent] = []
    req = _build_request(allowed_root, master_ws, worker_ws, expected_out)
    result = runner.run(req, on_event=streamed.append)

    assert result.status == "DELIVERED"
    assert result.technical_execution_succeeded is True
    assert result.business_verified is True
    assert result.actors_executed == ["core.master", "it.artifact_worker"]
    assert result.orchestrator_session_id == "sess-orch-rt-1"
    assert result.worker_session_id == "sess-work-rt-1"
    assert result.provider_conversation_ids == ["conv-rt-m1", "conv-rt-w1", "conv-rt-m2"]
    assert result.observed_tool_names == ["read_file", "write_file"]
    assert result.artifact is not None and result.artifact.sha256 == expected_sha
    assert result.artifact.qa_status == "VERIFIED_PASS"
    assert result.qa_verification is not None and result.qa_verification.status == "VERIFIED_PASS"
    assert result.telemetry.telemetry_status == "VERIFIED_MEASURED"
    assert result.telemetry.total_tokens == 360
    assert result.telemetry.estimated_cost_usd is None
    assert result.telemetry.actual_cost_usd is None
    assert len(result.events) == 3
    assert [ev.invocation_id for ev in streamed] == [req.invocation_id] * 3
    assert adapter.requests[0].execution_mode == "plan"
    assert adapter.requests[0].denied_paths == [str(worker_ws.resolve())]
    assert adapter.requests[1].execution_mode == "accept-edits"
    assert adapter.requests[1].denied_paths == [str(master_ws.resolve())]
    assert all(r.deny_shell_commands is True for r in adapter.requests)


def test_qa_verified_fail_and_unhandled_exception_boundaries(
    runtime_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Criteria 8, 9, 12: Missing artifact fails QA; lying or crashing FakeRuntime caught fail-closed."""
    allowed_root, master_ws, worker_ws, expected_out = runtime_dirs

    # 1. Missing artifact in CustomAgentRuntime cannot pass QA.
    def _missing_file_handler(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _engine_result(req, response_text=_plan_json(), conv_id="m1", on_event=cb)
        if idx == 2:
            return _engine_result(req, response_text="Claimed created.", conv_id="w1", on_event=cb)
        return _engine_result(
            req,
            response_text=json.dumps(
                {
                    "schema_version": "1.0",
                    "delivery_status": "REJECTED",
                    "qa_status_observed": "VERIFIED_FAIL",
                    "worker_status_observed": "SUCCEEDED",
                    "artifact_relpath": "output_report.txt",
                    "artifact_sha256": None,
                    "final_summary": "Rejected due to missing artifact.",
                    "verified_facts": [],
                    "unverified_or_failed_reasons": ["Missing output_report.txt"],
                }
            ),
            conv_id="m2",
            on_event=cb,
        )

    reg = EngineRegistry()
    reg.register_adapter(_ScriptedFakeEngineAdapter(_missing_file_handler))
    custom_rt = CustomAgentRuntime(registry=reg, allow_temp_workspace=True)
    req = _build_request(allowed_root, master_ws, worker_ws, expected_out)
    res_missing = execute_agent_workflow(custom_rt, req)
    assert res_missing.status == "REJECTED_BY_QA"
    assert res_missing.business_verified is False
    assert res_missing.qa_verification is not None and res_missing.qa_verification.status == "VERIFIED_FAIL"
    assert res_missing.artifact is not None and res_missing.artifact.exists_on_disk is False

    # 2. Lying FakeRuntime returning DELIVERED despite VERIFIED_FAIL is rejected by application boundary.
    lying_rt = _FakeAgentRuntime(simulate_mode="lie_delivered_without_qa_pass")
    res_lying = execute_agent_workflow(lying_rt, req)
    assert res_lying.status == "FAILED"
    assert res_lying.business_verified is False
    assert res_lying.error is not None and res_lying.error.error_code == "QA_INVARIANT_VIOLATION"

    # 3. Crashing FakeRuntime never raises raw exception and scrubs secrets in error message.
    crash_rt = _FakeAgentRuntime(simulate_mode="raise_exception")
    res_crash = execute_agent_workflow(crash_rt, req)
    assert res_crash.status == "FAILED"
    assert res_crash.error is not None
    assert res_crash.error.error_code == "RUNTIME_UNHANDLED_ERROR"
    assert "sk-[REDACTED]" in res_crash.error.message


def test_request_validation_workspace_isolation_no_fallback_and_none_telemetry(
    runtime_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Criteria 10, 11, 13, 14: Reject secrets, unauthorized/temp workspace, missing provider, None telemetry."""
    allowed_root, master_ws, worker_ws, expected_out = runtime_dirs

    # 1. Request rejects secret metadata and missing required paths.
    with pytest.raises(ValidationError):
        AgentRuntimeRequest(
            user_objective="test",
            workspace_scope=WorkspaceScope(
                allowed_root_path=str(allowed_root),
                orchestrator_workspace_path=str(master_ws),
                worker_workspace_path=str(worker_ws),
                permitted_read_paths=["input_data.json"],
                permitted_write_paths=["output_report.txt"],
            ),
            routing=ExecutionRouting(provider_id="antigravity"),
            acceptance=ArtifactAcceptanceSpec(artifact_relpath="output_report.txt"),
            metadata={"api_key": "dummy"},
        )

    # 2. Default allow_temp_workspace=False blocks OS temp workspace fail-closed.
    reg_empty = EngineRegistry()
    strict_custom = CustomAgentRuntime(registry=reg_empty)
    res_temp = execute_agent_workflow(
        strict_custom,
        _build_request(allowed_root, master_ws, worker_ws, expected_out),
    )
    assert res_temp.status == "BLOCKED"
    assert res_temp.error is not None and res_temp.error.error_code == "INVALID_WORKSPACE_ISOLATION"

    # 3. Unregistered provider returns BLOCKED error cleanly without Antigravity fallback.
    opt_custom = CustomAgentRuntime(registry=reg_empty, allow_temp_workspace=True)
    res_no_prov = execute_agent_workflow(
        opt_custom,
        _build_request(
            allowed_root,
            master_ws,
            worker_ws,
            expected_out,
            provider_id="unregistered_provider",
        ),
    )
    assert res_no_prov.status == "BLOCKED"
    assert res_no_prov.error is not None
    assert res_no_prov.error.error_code == "MASTER_PLAN_EXECUTION_FAILED"

    # 4. Missing telemetry stays None instead of zero.
    def _no_usage_handler(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _engine_result(req, response_text=_plan_json(), conv_id="nu-m1", with_usage=False, on_event=cb)
        return _engine_result(
            req,
            status="FAILED",
            conv_id="nu-w1",
            with_usage=False,
            error=EngineError(error_code="PROVIDER_ERROR", error_source="provider", message="Worker fail"),
            on_event=cb,
        )

    reg_no_usage = EngineRegistry()
    reg_no_usage.register_adapter(_ScriptedFakeEngineAdapter(_no_usage_handler))
    res_nu = execute_agent_workflow(
        CustomAgentRuntime(registry=reg_no_usage, allow_temp_workspace=True),
        _build_request(allowed_root, master_ws, worker_ws, expected_out),
    )
    assert res_nu.status == "FAILED"
    assert res_nu.telemetry.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
    assert res_nu.telemetry.input_tokens is None
    assert res_nu.telemetry.output_tokens is None
    assert res_nu.telemetry.total_tokens is None
