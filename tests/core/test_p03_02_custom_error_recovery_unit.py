"""Section XI error, cancellation, and recovery tests for Option A (Custom Orchestrator)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from core.orchestration import run_master_worker_spike
from engines import (
    EngineAdapter,
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
from engines.antigravity.adapter import AntigravityAdapter
from engines.antigravity.lifecycle import AntigravityProcessManager
from engines.antigravity.models import (
    AntigravityCapabilities,
    AntigravityDiscoveryResult,
    AntigravityStreamRequest,
    AntigravityStreamResult,
    AntigravityTelemetry,
)
from tests.benchmarks.p03_02.custom_harness import run_custom_benchmark


class _ScriptedFakeAdapter(EngineAdapter):
    """Deterministic fake EngineAdapter simulating Antigravity CLI behavior."""

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


def _make_result(
    req: EngineRequest,
    *,
    status: str = "SUCCEEDED",
    response_text: str | None = None,
    conv_id: str | None = None,
    error: EngineError | None = None,
    with_usage: bool = True,
    on_event: Callable[[EngineEvent], None] | None = None,
) -> EngineResult:
    now = datetime.now(timezone.utc)
    ev = EngineEvent(
        event_id=f"{req.request_id}-ev-1",
        sequence_number=1,
        timestamp=now,
        event_type="execution_completed" if status == "SUCCEEDED" else "provider_error",
        source="provider",
        provider_id="antigravity",
        status="COMPLETED" if status == "SUCCEEDED" else "FAILED",
        redacted_payload={"conversation_id": conv_id} if conv_id else {},
    )
    if on_event is not None:
        on_event(ev)
    telemetry = (
        EngineTelemetry(
            duration_ms=25,
            input_tokens=120,
            output_tokens=30,
            thinking_tokens=10,
            cached_tokens=0,
            total_tokens=150,
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
    session_id = req.session_reference.session_id if req.session_reference else None
    return EngineResult(
        request_id=req.request_id,
        provider_id="antigravity",
        adapter_id="antigravity_cli",
        platform_kind="agent_execution_platform",
        status=status,  # type: ignore[arg-type]
        business_verification_status="UNVERIFIED",
        response_text=response_text,
        observed_tool_names=["read_file", "write_file"] if status == "SUCCEEDED" else [],
        session_reference=EngineSessionReference(
            session_id=session_id,
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


def _valid_plan_json() -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "task_summary": "Write proof.txt from package.json",
            "target_worker_id": "it.artifact_worker",
            "worker_objective": "Read package.json and write proof.txt.",
            "allowed_read_paths": ["package.json"],
            "allowed_write_paths": ["proof.txt"],
            "forbidden_actions": ["Run shell commands"],
            "acceptance_criteria": ["package.json intact", "proof.txt valid"],
            "artifact_expectation": {
                "required_substrings": ["name="],
                "expected_line_count": 2,
                "must_preserve_inputs": True,
            },
        }
    )


def _valid_synthesis_json(sha_hex: str) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "delivery_status": "DELIVERED",
            "qa_status_observed": "VERIFIED_PASS",
            "worker_status_observed": "SUCCEEDED",
            "artifact_relpath": "proof.txt",
            "artifact_sha256": sha_hex,
            "final_summary": "Verified delivery.",
            "verified_facts": ["SHA-256 verified"],
            "unverified_or_failed_reasons": [],
        }
    )


@pytest.fixture
def custom_dirs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """Provide isolated master/worker directories and fixture content inside tmp_path."""
    allowed_root = tmp_path / "bench_root"
    master_dir = allowed_root / "master_ws"
    worker_dir = allowed_root / "worker_ws"
    master_dir.mkdir(parents=True)
    worker_dir.mkdir(parents=True)
    marker = "mk-p0302-unit-01"
    (worker_dir / "package.json").write_text(
        json.dumps({"name": "ai-village-p0302", "verification_marker": marker}) + "\n",
        encoding="utf-8",
    )
    expected_proof = f"name=ai-village-p0302\nverification_marker={marker}\n"
    return allowed_root, master_dir, worker_dir, expected_proof


def _registry(adapter: EngineAdapter) -> EngineRegistry:
    reg = EngineRegistry()
    reg.register_adapter(adapter)
    return reg


def test_custom_harness_happy_path_metrics_and_partial_none_telemetry(
    custom_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Custom harness preserves exact token sums and keeps missing telemetry as None."""
    allowed_root, master_dir, worker_dir, expected_proof = custom_dirs
    expected_sha = compute_sha256_bytes(expected_proof.encode("utf-8"))

    def _ok_handler(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _make_result(req, response_text=_valid_plan_json(), conv_id="c-m1", on_event=cb)
        if idx == 2:
            (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8", newline="\n")
            return _make_result(
                req,
                response_text="ok",
                conv_id="c-w1",
                with_usage=False,
                on_event=cb,
            )
        return _make_result(
            req,
            response_text=_valid_synthesis_json(expected_sha),
            conv_id="c-m2",
            on_event=cb,
        )

    outcome, spike = run_custom_benchmark(
        registry=_registry(_ScriptedFakeAdapter(_ok_handler)),
        user_request="Read package.json and create proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    assert outcome.status == "DELIVERED"
    assert spike.status == "DELIVERED"
    assert outcome.timing.provider_call_count == 3
    assert outcome.timing.extra_reasoning_calls == 0
    assert outcome.timing.prompt_chars_total > 0
    # Worker had UNAVAILABLE_FROM_PROVIDER, so aggregate must stay None rather than zero/partial.
    assert outcome.timing.telemetry_complete is False
    assert outcome.timing.input_tokens_total is None
    assert outcome.timing.total_tokens_total is None


def test_section_xi_07_provider_disconnect_fails_closed_without_retries(
    custom_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Section XI-07: Provider disconnection halts cleanly without blind retries."""
    allowed_root, master_dir, worker_dir, expected_proof = custom_dirs

    def _disconnect_handler(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _make_result(req, response_text=_valid_plan_json(), conv_id="c-m-net", on_event=cb)
        return _make_result(
            req,
            status="FAILED",
            conv_id="c-w-net",
            error=EngineError(
                error_code="NETWORK_ERROR",
                error_source="network",
                message="Simulated provider disconnect",
                retryable=True,
            ),
            on_event=cb,
        )

    adapter = _ScriptedFakeAdapter(_disconnect_handler)
    outcome, spike = run_custom_benchmark(
        registry=_registry(adapter),
        user_request="Create proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    assert outcome.status == "FAILED"
    assert spike.error is not None and spike.error.error_code == "WORKER_EXECUTION_FAILED"
    assert len(adapter.requests) == 2
    assert spike.qa_verification is not None and spike.qa_verification.status == "VERIFIED_FAIL"


def test_section_xi_05_worker_cancellation_via_process_manager_and_spike(
    monkeypatch: pytest.MonkeyPatch,
    custom_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Section XI-05: Real owned execution cancellation marks CANCELLED and blocks delivery."""
    allowed_root, master_dir, worker_dir, expected_proof = custom_dirs
    base_adapter = AntigravityAdapter()
    monkeypatch.setattr(
        base_adapter,
        "discover",
        lambda: AntigravityDiscoveryResult(found=True, executable_path="C:\\fake\\agy.exe", source="explicit"),
    )
    monkeypatch.setattr(
        base_adapter,
        "probe",
        lambda timeout_seconds=15: AntigravityCapabilities(
            executable_path="C:\\fake\\agy.exe",
            version="1.5.15",
            supports_print_mode=True,
            supports_json_output=True,
            supports_stream_json_flag=True,
            supports_sandbox_flag=True,
            supports_mode_flag=True,
            supports_settings_flag=True,
            supports_conversation_flag=True,
            supports_print_timeout=True,
        ),
    )

    def _cancel_exec(
        req: AntigravityStreamRequest,
        *,
        controller: Any = None,
        execution_id: str = "exec-cancelled",
        **kwargs: Any,
    ) -> AntigravityStreamResult:
        _ = (req, kwargs)
        assert controller is not None
        controller.cancel_event.wait(timeout=2.0)
        now = datetime.now(timezone.utc)
        return AntigravityStreamResult(
            status="CANCELLED" if controller.cancel_event.is_set() else "FAILED",
            execution_id=execution_id,
            command_summary="agy --sandbox",
            started_at=now,
            finished_at=now,
            duration_ms=15,
            telemetry=AntigravityTelemetry(duration_ms=15, telemetry_status="UNAVAILABLE_FROM_PROVIDER"),
            error=None,
        )

    monkeypatch.setattr(base_adapter, "_execute_stream_request", _cancel_exec)
    pm = AntigravityProcessManager(adapter=base_adapter)
    exec_id = pm.start_execution(
        AntigravityStreamRequest(prompt="long work", workspace_path=str(worker_dir)),
        session_id="sess-cancel-check",
    )
    cancel_outcome = pm.request_cancel(exec_id, wait=True, wait_timeout=5.0)
    res_pm = pm.await_result(exec_id, timeout=5.0)
    assert cancel_outcome.accepted is True
    assert res_pm.status == "CANCELLED"
    assert pm.get_execution_status(exec_id).state == "CANCELLED"

    def _cancelled_worker_handler(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _make_result(req, response_text=_valid_plan_json(), conv_id="c-m-c", on_event=cb)
        return _make_result(
            req,
            status="CANCELLED",
            conv_id="c-w-c",
            error=EngineError(
                error_code="CANCELLED",
                error_source="cancellation",
                message="User cancelled worker process",
            ),
            on_event=cb,
        )

    outcome, spike = run_custom_benchmark(
        registry=_registry(_ScriptedFakeAdapter(_cancelled_worker_handler)),
        user_request="Create proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    assert outcome.status == "FAILED"
    assert spike.qa_verification is not None and spike.qa_verification.status == "VERIFIED_FAIL"


def test_section_xi_08_13_14_handoff_interrupted_no_durable_resume_and_callback_error(
    custom_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Sections XI-08, XI-13, XI-14: Interrupted handoff preserves state; callback error surfaces."""
    allowed_root, master_dir, worker_dir, expected_proof = custom_dirs

    def _blocked_worker(
        req: EngineRequest,
        idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if idx == 1:
            return _make_result(req, response_text=_valid_plan_json(), conv_id="c-m-8", on_event=cb)
        return _make_result(
            req,
            status="BLOCKED",
            conv_id="c-w-8",
            error=EngineError(
                error_code="EXECUTION_BLOCKED",
                error_source="adapter",
                message="Worker blocked during interrupted handoff",
            ),
            on_event=cb,
        )

    spike_interrupted = run_master_worker_spike(
        registry=_registry(_ScriptedFakeAdapter(_blocked_worker)),
        user_request="Create proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    assert spike_interrupted.status == "BLOCKED"
    # Section XI-13: Partial state is returned in memory only; no durable SQLite checkpointing exists in Custom spike.
    assert spike_interrupted.master_plan is not None
    assert spike_interrupted.worker_handoff is not None
    assert spike_interrupted.qa_verification is not None
    assert spike_interrupted.qa_verification.status == "VERIFIED_FAIL"

    def _failing_callback(_ev: object) -> None:
        raise RuntimeError("Downstream UI event sink disconnected")

    with pytest.raises(RuntimeError, match="Downstream UI event sink disconnected"):
        run_master_worker_spike(
            registry=_registry(
                _ScriptedFakeAdapter(
                    lambda req, idx, cb: _make_result(
                        req,
                        response_text=_valid_plan_json(),
                        conv_id="c-m-cb",
                        on_event=cb,
                    )
                )
            ),
            user_request="Create proof.txt",
            master_workspace_dir=master_dir,
            worker_workspace_dir=worker_dir,
            allowed_root=allowed_root,
            expected_artifact_content=expected_proof,
            on_event=_failing_callback,
            allow_temp_workspace=True,
        )
