"""Unit and policy contract tests for the P03.01 Master -> Worker -> QA spike."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

import pytest
from pydantic import ValidationError

from core.orchestration import (
    AttributedSpikeEvent,
    MasterWorkerSpikeResult,
    SpikeAgentSpec,
    WorkerHandoff,
    build_default_spike_roster,
    parse_strict_json_object,
    run_master_worker_spike,
)
from engines import (
    AntigravityEngineAdapter,
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
from engines.antigravity.models import (
    AntigravityCapabilities,
    AntigravityDiscoveryResult,
    AntigravityStreamRequest,
    AntigravityStreamResult,
    AntigravityTelemetry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class _ScriptedFakeAdapter(EngineAdapter):
    """Deterministic EngineAdapter simulating separate Master and Worker invocations."""

    def __init__(self, handler: Callable[[EngineRequest, int, Callable[[EngineEvent], None] | None], EngineResult]) -> None:
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
        call_index = len(self.requests)
        return self._handler(request, call_index, on_event)


def _make_telemetry(*, with_usage: bool = True) -> EngineTelemetry:
    if not with_usage:
        return EngineTelemetry(
            duration_ms=15,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        )
    return EngineTelemetry(
        duration_ms=25,
        input_tokens=100,
        output_tokens=40,
        total_tokens=140,
        telemetry_status="VERIFIED_MEASURED",
        telemetry_source="provider_cli",
    )


def _emit_standard_events(
    request: EngineRequest,
    conv_id: str,
    on_event: Callable[[EngineEvent], None] | None,
    *,
    tool_names: list[str] | None = None,
) -> list[EngineEvent]:
    now = datetime.now(timezone.utc)
    events: list[EngineEvent] = [
        EngineEvent(
            event_id=f"{request.request_id}-ev-1",
            sequence_number=1,
            timestamp=now,
            event_type="execution_started",
            source="adapter",
            provider_id="antigravity",
            status="STARTED",
            redacted_payload={"conversation_id": conv_id},
        )
    ]
    seq = 2
    for tool_name in tool_names or []:
        events.append(
            EngineEvent(
                event_id=f"{request.request_id}-ev-{seq}",
                sequence_number=seq,
                timestamp=now,
                event_type="tool_execution_observed",
                source="provider",
                provider_id="antigravity",
                status="COMPLETED",
                tool_name=tool_name,
                redacted_payload={"tool_name": tool_name},
            )
        )
        seq += 1
    events.append(
        EngineEvent(
            event_id=f"{request.request_id}-ev-{seq}",
            sequence_number=seq,
            timestamp=now,
            event_type="execution_completed",
            source="provider",
            provider_id="antigravity",
            status="COMPLETED",
            redacted_payload={"conversation_id": conv_id},
        )
    )
    if on_event is not None:
        for ev in events:
            on_event(ev)
    return events


def _make_engine_result(
    request: EngineRequest,
    *,
    status: str = "SUCCEEDED",
    response_text: str | None = None,
    conv_id: str | None = None,
    observed_tool_names: list[str] | None = None,
    with_usage: bool = True,
    error: EngineError | None = None,
    on_event: Callable[[EngineEvent], None] | None = None,
) -> EngineResult:
    now = datetime.now(timezone.utc)
    session_id = request.session_reference.session_id if request.session_reference else None
    events = _emit_standard_events(
        request,
        conv_id or "conv-default",
        on_event,
        tool_names=observed_tool_names,
    )
    return EngineResult(
        request_id=request.request_id,
        provider_id="antigravity",
        adapter_id="antigravity_cli",
        platform_kind="agent_execution_platform",
        status=status,  # type: ignore[arg-type]
        business_verification_status="UNVERIFIED",
        response_text=response_text,
        observed_tool_names=observed_tool_names or [],
        session_reference=EngineSessionReference(
            session_id=session_id,
            provider_conversation_id=conv_id,
            binding_source="provider_cli" if conv_id else "adapter_logical",
        ),
        started_at=now,
        finished_at=now,
        duration_ms=20,
        telemetry=_make_telemetry(with_usage=with_usage),
        events=events,
        error=error,
    )


def _valid_plan_dict(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "task_summary": "Delegate artifact generation to it.artifact_worker.",
        "target_worker_id": "it.artifact_worker",
        "worker_objective": "Read package.json and write proof.txt with name and verification_marker.",
        "allowed_read_paths": ["package.json"],
        "allowed_write_paths": ["proof.txt"],
        "forbidden_actions": ["Run shell commands", "Modify package.json"],
        "acceptance_criteria": ["package.json untouched", "proof.txt valid"],
        "artifact_expectation": {
            "required_substrings": ["name=", "verification_marker="],
            "expected_line_count": 2,
            "must_preserve_inputs": True,
        },
    }
    payload.update(overrides)
    return payload


def _valid_synthesis_dict(expected_sha256: str | None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "delivery_status": "DELIVERED",
        "qa_status_observed": "VERIFIED_PASS",
        "worker_status_observed": "SUCCEEDED",
        "artifact_relpath": "proof.txt",
        "artifact_sha256": expected_sha256,
        "final_summary": "Artifact proof.txt verified by independent QA.",
        "verified_facts": ["package.json hash unchanged", "proof.txt SHA-256 verified"],
        "unverified_or_failed_reasons": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def spike_dirs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """Create isolated master/worker dirs and package.json inside an allowed root."""
    allowed_root = tmp_path / "allowed_workspace_root"
    master_dir = allowed_root / "master_ws"
    worker_dir = allowed_root / "worker_ws"
    master_dir.mkdir(parents=True)
    worker_dir.mkdir(parents=True)
    marker = "mk-unit-9f8e7d6c"
    (worker_dir / "package.json").write_text(
        json.dumps({"name": "ai-village-unit-pkg", "verification_marker": marker}, indent=2) + "\n",
        encoding="utf-8",
    )
    expected_proof = f"name=ai-village-unit-pkg\nverification_marker={marker}\n"
    return allowed_root, master_dir, worker_dir, expected_proof


def _registry_with(adapter: EngineAdapter) -> EngineRegistry:
    registry = EngineRegistry()
    registry.register_adapter(adapter)
    return registry


def _run_unit_spike(**kwargs: Any) -> MasterWorkerSpikeResult:
    """Invoke ``run_master_worker_spike`` allowing pytest OS temp fixtures cleanly."""
    kwargs.setdefault("allow_temp_workspace", True)
    return run_master_worker_spike(**kwargs)


def test_parse_strict_json_object_rejects_prose_and_broken_json() -> None:
    """Scenario 1 helper check: rejects prose, arrays, and syntax errors."""
    with pytest.raises(ValueError):
        parse_strict_json_object("Sure! Here is the JSON:\n{\"ok\": true}")
    with pytest.raises(ValueError):
        parse_strict_json_object("[1, 2, 3]")
    with pytest.raises(ValueError):
        parse_strict_json_object("{bad json")
    assert parse_strict_json_object("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_scenario_01_master_garbage_text_fails_closed(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenario 1: Non-JSON Master output stops pipeline before Worker."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    adapter = _ScriptedFakeAdapter(
        lambda req, _idx, cb: _make_engine_result(
            req,
            response_text="Let us read package.json and write proof.txt.",
            conv_id="conv-m-1",
            on_event=cb,
        )
    )
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "FAILED"
    assert res.error is not None
    assert res.error.error_code == "MASTER_PLAN_PARSE_FAILED"
    assert res.worker_execution is None
    assert res.qa_verification is None
    assert len(adapter.requests) == 1


@pytest.mark.parametrize(
    "bad_worker_id",
    ["it.unapproved_worker", "core.master", "unknown.agent"],
)
def test_scenario_02_unauthorized_target_worker_blocked(
    spike_dirs: tuple[Path, Path, Path, str],
    bad_worker_id: str,
) -> None:
    """Scenario 2: Unauthorized, missing, or non-worker target IDs fail closed."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    adapter = _ScriptedFakeAdapter(
        lambda req, _idx, cb: _make_engine_result(
            req,
            response_text=json.dumps(_valid_plan_dict(target_worker_id=bad_worker_id)),
            conv_id="conv-m-2",
            on_event=cb,
        )
    )
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "BLOCKED"
    assert res.error is not None
    assert res.error.error_code == "POLICY_VIOLATION"
    assert res.worker_execution is None
    assert len(adapter.requests) == 1


@pytest.mark.parametrize(
    "bad_path",
    ["../secret.txt", "/etc/passwd", "C:\\Windows\\win.ini", "*.json"],
)
def test_scenario_03_path_traversal_and_wildcards_rejected(
    spike_dirs: tuple[Path, Path, Path, str],
    bad_path: str,
) -> None:
    """Scenario 3: Path traversal, absolute paths, and wildcards fail closed before Worker."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    adapter = _ScriptedFakeAdapter(
        lambda req, _idx, cb: _make_engine_result(
            req,
            response_text=json.dumps(_valid_plan_dict(allowed_read_paths=[bad_path])),
            conv_id="conv-m-3",
            on_event=cb,
        )
    )
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "FAILED"
    assert res.error is not None
    assert res.error.error_code == "MASTER_PLAN_PARSE_FAILED"
    assert res.worker_execution is None
    assert len(adapter.requests) == 1


def test_scenario_04_disallowed_file_and_protected_overwrite_blocked(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenario 4: Reading unpermitted files or writing protected files is blocked."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    (worker_dir / "other.txt").write_text("extra\n", encoding="utf-8")
    adapter = _ScriptedFakeAdapter(
        lambda req, _idx, cb: _make_engine_result(
            req,
            response_text=json.dumps(
                _valid_plan_dict(
                    allowed_read_paths=["package.json", "other.txt"],
                    allowed_write_paths=["package.json"],
                )
            ),
            conv_id="conv-m-4",
            on_event=cb,
        )
    )
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "BLOCKED"
    assert res.error is not None
    assert res.error.error_code == "POLICY_VIOLATION"
    assert res.worker_execution is None


def test_scenario_05_sensitive_context_and_shell_bypass_rejected(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenario 5: Credential material in objective, allow_shell worker, or WorkerHandoff bypass rejected."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    leaked_token = "sk-" + ("z" * 24)
    adapter = _ScriptedFakeAdapter(
        lambda req, _idx, cb: _make_engine_result(
            req,
            response_text=json.dumps(
                _valid_plan_dict(worker_objective=f"Read package.json using {leaked_token}")
            ),
            conv_id="conv-m-5",
            on_event=cb,
        )
    )
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "BLOCKED"
    assert res.error is not None
    assert res.error.error_code == "POLICY_VIOLATION"
    assert res.worker_execution is None

    roster_with_shell = build_default_spike_roster()
    roster_with_shell["it.artifact_worker"] = SpikeAgentSpec(
        agent_id="it.artifact_worker",
        department="it",
        system_role="worker",
        execution_mode="accept-edits",
        allow_workspace_write=True,
        allow_shell=True,
    )
    clean_adapter = _ScriptedFakeAdapter(
        lambda req, _idx, cb: _make_engine_result(
            req,
            response_text=json.dumps(_valid_plan_dict()),
            conv_id="conv-m-5b",
            on_event=cb,
        )
    )
    res_shell = _run_unit_spike(
        registry=_registry_with(clean_adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        roster=roster_with_shell,
    )
    assert res_shell.status == "BLOCKED"
    assert res_shell.error is not None
    assert res_shell.error.error_code == "POLICY_VIOLATION"

    with pytest.raises(ValidationError):
        WorkerHandoff(
            correlation_id="corr-1",
            from_agent_id="core.master",
            to_agent_id="it.artifact_worker",
            worker_session_id="sess-w-1",
            worker_workspace_path=str(worker_dir),
            denied_paths=[str(master_dir)],
            deny_shell_commands=False,  # type: ignore[arg-type]
            plan=json.loads(json.dumps(_valid_plan_dict())),
            worker_instructions="Write proof.txt",
        )


def test_scenario_06_master_mutating_worker_workspace_blocked(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenario 6: Direct mutation of worker workspace during Master stage fails closed."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs

    def _cheating_master(
        req: EngineRequest,
        _idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8")
        return _make_engine_result(
            req,
            response_text=json.dumps(_valid_plan_dict()),
            conv_id="conv-m-cheat",
            on_event=cb,
        )

    adapter = _ScriptedFakeAdapter(_cheating_master)
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "BLOCKED"
    assert res.error is not None
    assert res.error.error_code == "POLICY_VIOLATION"
    assert res.worker_execution is None
    assert len(adapter.requests) == 1
    assert adapter.requests[0].execution_mode == "plan"
    assert adapter.requests[0].deny_shell_commands is True
    assert str(worker_dir.resolve()) in adapter.requests[0].denied_paths


def test_scenarios_07_08_11_15_full_flow_qa_separation_and_isolation(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenarios 7, 8, 11, 15: Happy path, UNVERIFIED pre-QA status, session isolation, boundaries."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    expected_sha = compute_sha256_bytes(expected_proof.encode("utf-8"))
    streamed: list[AttributedSpikeEvent] = []

    def _handler(
        req: EngineRequest,
        call_idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if call_idx == 1:
            return _make_engine_result(
                req,
                response_text=json.dumps(_valid_plan_dict()),
                conv_id="conv-master-plan-01",
                on_event=cb,
            )
        if call_idx == 2:
            raw = json.loads((Path(req.workspace_path or "") / "package.json").read_text(encoding="utf-8"))
            content = f"name={raw['name']}\nverification_marker={raw['verification_marker']}\n"
            (Path(req.workspace_path or "") / "proof.txt").write_text(content, encoding="utf-8", newline="\n")
            return _make_engine_result(
                req,
                response_text="Verified proof.txt after reading package.json.",
                conv_id="conv-worker-exec-01",
                observed_tool_names=["read_file", "write_file", "read_file"],
                on_event=cb,
            )
        return _make_engine_result(
            req,
            response_text=json.dumps(_valid_synthesis_dict(expected_sha)),
            conv_id="conv-master-synth-01",
            on_event=cb,
        )

    adapter = _ScriptedFakeAdapter(_handler)
    res = _run_unit_spike(
        registry=_registry_with(adapter),
        user_request="Coordinate worker to inspect package.json and write proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        required_artifact_substrings=["name=ai-village-unit-pkg"],
        on_event=streamed.append,
        master_session_id="sess-master-fixed",
        worker_session_id="sess-worker-fixed",
    )

    assert res.status == "DELIVERED"
    assert res.error is None
    assert res.master_plan is not None
    assert res.worker_handoff is not None
    assert res.worker_execution is not None
    assert res.qa_verification is not None
    assert res.master_synthesis is not None

    # Scenario 8: Worker's EngineResult always stays UNVERIFIED prior to independent QA.
    assert res.worker_execution.engine_result.business_verification_status == "UNVERIFIED"
    assert res.qa_verification.status == "VERIFIED_PASS"
    assert res.qa_verification.business_verified is True
    assert res.qa_verification.actual_sha256 == expected_sha
    assert res.qa_verification.source_integrity_verified is True

    # Scenario 11 & 15: Distinct logical sessions, distinct conversation IDs, mutual denied_paths.
    assert res.master_session_id == "sess-master-fixed"
    assert res.worker_session_id == "sess-worker-fixed"
    assert res.master_plan_conversation_id == "conv-master-plan-01"
    assert res.worker_conversation_id == "conv-worker-exec-01"
    assert res.master_synthesis_conversation_id == "conv-master-synth-01"
    assert len(adapter.requests) == 3
    assert adapter.requests[0].execution_mode == "plan"
    assert adapter.requests[1].execution_mode == "accept-edits"
    assert adapter.requests[2].execution_mode == "plan"
    assert adapter.requests[0].denied_paths == [str(worker_dir.resolve())]
    assert adapter.requests[1].denied_paths == [str(master_dir.resolve())]
    assert adapter.requests[2].denied_paths == [str(worker_dir.resolve())]
    assert all(req.deny_shell_commands is True for req in adapter.requests)

    # Attributed events reflect all three stages accurately and stream in order.
    assert [ev.engine_event for ev in streamed] == [ev.engine_event for ev in res.attributed_events]
    assert [ev.sequence for ev in res.attributed_events] == list(
        range(1, len(res.attributed_events) + 1)
    )
    stages_seen = {ev.stage for ev in res.attributed_events}
    assert stages_seen == {"master_plan", "worker_execute", "master_synthesize"}
    worker_events = [ev for ev in res.attributed_events if ev.stage == "worker_execute"]
    assert all(ev.agent_id == "it.artifact_worker" and ev.role == "worker" for ev in worker_events)
    assert all(ev.logical_session_id == "sess-worker-fixed" for ev in worker_events)
    assert all(ev.provider_conversation_id == "conv-worker-exec-01" for ev in worker_events)


@pytest.mark.parametrize("worker_status", ["FAILED", "TIMED_OUT", "CANCELLED"])
def test_scenario_09_worker_failure_timeout_cancel_prevents_delivery(
    spike_dirs: tuple[Path, Path, Path, str],
    worker_status: str,
) -> None:
    """Scenario 9: Worker failure/timeout/cancellation fails QA and blocks synthesis."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs

    def _handler(
        req: EngineRequest,
        call_idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if call_idx == 1:
            return _make_engine_result(
                req,
                response_text=json.dumps(_valid_plan_dict()),
                conv_id="conv-m-9",
                on_event=cb,
            )
        return _make_engine_result(
            req,
            status=worker_status,
            response_text=None,
            conv_id="conv-w-9",
            error=EngineError(
                error_code="TIMED_OUT" if worker_status == "TIMED_OUT" else "PROVIDER_ERROR",
                error_source="adapter",
                message=f"Simulated {worker_status}",
            ),
            on_event=cb,
        )

    res = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_handler)),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "FAILED"
    assert res.qa_verification is not None
    assert res.qa_verification.status == "VERIFIED_FAIL"
    assert res.qa_verification.engine_succeeded is False
    assert res.master_synthesis is None
    assert res.error is not None
    assert res.error.error_code == "WORKER_EXECUTION_FAILED"


@pytest.mark.parametrize("tamper_mode", ["missing_file", "wrong_content", "corrupted_source"])
def test_scenario_10_qa_catches_bad_artifact_and_blocks_master_override(
    spike_dirs: tuple[Path, Path, Path, str],
    tamper_mode: str,
) -> None:
    """Scenario 10: Worker false SUCCEEDED caught by QA; compromised Master cannot overwrite."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    expected_sha = compute_sha256_bytes(expected_proof.encode("utf-8"))

    def _make_handler(*, cheating_master: bool) -> Callable[[EngineRequest, int, Callable[[EngineEvent], None] | None], EngineResult]:
        def _handler(
            req: EngineRequest,
            call_idx: int,
            cb: Callable[[EngineEvent], None] | None,
        ) -> EngineResult:
            if call_idx == 1:
                return _make_engine_result(
                    req,
                    response_text=json.dumps(_valid_plan_dict()),
                    conv_id="conv-m-10",
                    on_event=cb,
                )
            if call_idx == 2:
                if tamper_mode == "wrong_content":
                    (worker_dir / "proof.txt").write_text("tampered\n", encoding="utf-8")
                elif tamper_mode == "corrupted_source":
                    (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8", newline="\n")
                    (worker_dir / "package.json").write_text('{"name":"hacked"}\n', encoding="utf-8")
                return _make_engine_result(
                    req,
                    status="SUCCEEDED",
                    response_text="All done!",
                    conv_id="conv-w-10",
                    on_event=cb,
                )
            if cheating_master:
                payload = _valid_synthesis_dict(expected_sha, delivery_status="DELIVERED", qa_status_observed="VERIFIED_PASS")
            else:
                payload = _valid_synthesis_dict(
                    None,
                    delivery_status="REJECTED",
                    qa_status_observed="VERIFIED_FAIL",
                    unverified_or_failed_reasons=["QA failed"],
                )
            return _make_engine_result(
                req,
                response_text=json.dumps(payload),
                conv_id="conv-m-synth-10",
                on_event=cb,
            )

        return _handler

    honest_res = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_make_handler(cheating_master=False))),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert honest_res.status == "REJECTED_BY_QA"
    assert honest_res.qa_verification is not None
    assert honest_res.qa_verification.status == "VERIFIED_FAIL"
    assert honest_res.error is not None
    assert honest_res.error.error_code == "QA_VERIFICATION_FAILED"

    # Reset workspace package.json if modified during test.
    if tamper_mode == "corrupted_source":
        marker = "mk-unit-9f8e7d6c"
        (worker_dir / "package.json").write_text(
            json.dumps({"name": "ai-village-unit-pkg", "verification_marker": marker}, indent=2) + "\n",
            encoding="utf-8",
        )

    cheating_res = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_make_handler(cheating_master=True))),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert cheating_res.status == "FAILED"
    assert cheating_res.error is not None
    assert cheating_res.error.error_code == "SYNTHESIS_TRUTH_MISMATCH"


def test_scenario_11b_conversation_collision_and_master_dir_tamper_blocked(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenario 11 collision: Same provider_conversation_id or worker writing master_ws fails closed."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs

    def _collision_handler(
        req: EngineRequest,
        call_idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if call_idx == 1:
            return _make_engine_result(
                req,
                response_text=json.dumps(_valid_plan_dict()),
                conv_id="conv-shared-bug",
                on_event=cb,
            )
        return _make_engine_result(
            req,
            response_text="ok",
            conv_id="conv-shared-bug",
            on_event=cb,
        )

    res_coll = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_collision_handler)),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res_coll.status == "BLOCKED"
    assert res_coll.error is not None
    assert res_coll.error.error_code == "SESSION_ISOLATION_VIOLATION"

    def _worker_tamper_master_handler(
        req: EngineRequest,
        call_idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if call_idx == 1:
            return _make_engine_result(
                req,
                response_text=json.dumps(_valid_plan_dict()),
                conv_id="conv-m-11b-tamper",
                on_event=cb,
            )
        (master_dir / "intrusion.txt").write_text("worker broke into master ws\n", encoding="utf-8")
        return _make_engine_result(
            req,
            response_text="ok",
            conv_id="conv-w-11b-tamper",
            on_event=cb,
        )

    res_tamper = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_worker_tamper_master_handler)),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res_tamper.status == "BLOCKED"
    assert res_tamper.error is not None
    assert res_tamper.error.error_code == "SESSION_ISOLATION_VIOLATION"
    assert res_tamper.qa_verification is None


def test_scenario_12_workspace_isolation_violations_blocked_preflight(
    spike_dirs: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    """Scenario 12: OS temp default rejection, overlapping dirs, outside root, or identical session IDs block preflight."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs
    adapter = _ScriptedFakeAdapter(lambda req, _idx, cb: _make_engine_result(req, on_event=cb))
    registry = _registry_with(adapter)

    res_temp_rejected = run_master_worker_spike(
        registry=registry,
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res_temp_rejected.status == "BLOCKED"
    assert res_temp_rejected.error is not None
    assert res_temp_rejected.error.error_code == "INVALID_WORKSPACE_ISOLATION"

    nested_worker = master_dir / "nested"
    nested_worker.mkdir()
    res_overlap = _run_unit_spike(
        registry=registry,
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=nested_worker,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res_overlap.status == "BLOCKED"
    assert res_overlap.error is not None
    assert res_overlap.error.error_code == "INVALID_WORKSPACE_ISOLATION"

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    res_outside = _run_unit_spike(
        registry=registry,
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=outside_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res_outside.status == "BLOCKED"
    assert res_outside.error is not None
    assert res_outside.error.error_code == "INVALID_WORKSPACE_ISOLATION"

    res_same_sess = _run_unit_spike(
        registry=registry,
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        master_session_id="same-id",
        worker_session_id="same-id",
    )
    assert res_same_sess.status == "BLOCKED"
    assert res_same_sess.error is not None
    assert res_same_sess.error.error_code == "SESSION_ISOLATION_VIOLATION"
    assert not adapter.requests


def test_scenarios_13_14_16_synthesis_errors_honest_telemetry_and_partial_state(
    spike_dirs: tuple[Path, Path, Path, str],
) -> None:
    """Scenarios 13, 14, 16: Malformed/mismatched synthesis, None tokens preserved, partial records kept."""
    allowed_root, master_dir, worker_dir, expected_proof = spike_dirs

    def _bad_synth_handler(
        req: EngineRequest,
        call_idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if call_idx == 1:
            return _make_engine_result(
                req,
                response_text=json.dumps(_valid_plan_dict()),
                conv_id="conv-m-13",
                with_usage=False,
                on_event=cb,
            )
        if call_idx == 2:
            (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8", newline="\n")
            return _make_engine_result(
                req,
                response_text="Done.",
                conv_id="conv-w-13",
                with_usage=False,
                on_event=cb,
            )
        return _make_engine_result(
            req,
            response_text="Unstructured text instead of JSON synthesis",
            conv_id="conv-ms-13",
            with_usage=False,
            on_event=cb,
        )

    res = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_bad_synth_handler)),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res.status == "FAILED"
    assert res.error is not None
    assert res.error.error_code == "MASTER_SYNTHESIS_PARSE_FAILED"
    # Scenario 16: Partial evidence retained cleanly on failure.
    assert res.master_plan is not None
    assert res.worker_handoff is not None
    assert res.worker_execution is not None
    assert res.qa_verification is not None and res.qa_verification.status == "VERIFIED_PASS"
    assert len(res.attributed_events) > 0
    assert res.duration_ms >= 0
    # Scenario 14: Honest None telemetry is never fabricated as 0.
    assert res.master_plan_result is not None
    assert res.master_plan_result.telemetry.input_tokens is None
    assert res.master_plan_result.telemetry.output_tokens is None
    assert res.master_plan_result.telemetry.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"

    def _wrong_hash_synth_handler(
        req: EngineRequest,
        call_idx: int,
        cb: Callable[[EngineEvent], None] | None,
    ) -> EngineResult:
        if call_idx == 1:
            return _make_engine_result(
                req,
                response_text=json.dumps(_valid_plan_dict()),
                conv_id="conv-m-13b",
                on_event=cb,
            )
        if call_idx == 2:
            (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8", newline="\n")
            return _make_engine_result(
                req,
                response_text="Done.",
                conv_id="conv-w-13b",
                on_event=cb,
            )
        return _make_engine_result(
            req,
            response_text=json.dumps(_valid_synthesis_dict("0" * 64)),
            conv_id="conv-ms-13b",
            on_event=cb,
        )

    res_wrong_sha = _run_unit_spike(
        registry=_registry_with(_ScriptedFakeAdapter(_wrong_hash_synth_handler)),
        user_request="Build proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
    )
    assert res_wrong_sha.status == "FAILED"
    assert res_wrong_sha.error is not None
    assert res_wrong_sha.error.error_code == "SYNTHESIS_TRUTH_MISMATCH"
    assert res_wrong_sha.qa_verification is not None
    assert res_wrong_sha.qa_verification.status == "VERIFIED_PASS"
    assert res_wrong_sha.master_synthesis is not None


def test_scenario_15b_antigravity_adapter_forwards_sandbox_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify EngineRequest denied_paths and deny_shell_commands reach AntigravityStreamRequest."""
    captured: list[AntigravityStreamRequest] = []
    now = datetime.now(timezone.utc)
    fake_base = AntigravityAdapter()

    monkeypatch.setattr(
        fake_base,
        "discover",
        lambda: AntigravityDiscoveryResult(
            found=True,
            executable_path="C:\\fake\\agy.exe",
            source="explicit",
        ),
    )
    monkeypatch.setattr(
        fake_base,
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

    def _fake_stream(
        req: AntigravityStreamRequest,
        *,
        on_event: object = None,
        cancel_event: object = None,
    ) -> AntigravityStreamResult:
        _ = (on_event, cancel_event)
        captured.append(req)
        return AntigravityStreamResult(
            status="SUCCEEDED",
            execution_id="exec-fake",
            command_summary="agy --sandbox",
            started_at=now,
            finished_at=now,
            duration_ms=10,
            response_text="{}",
            conversation_id="conv-forwarded",
            telemetry=AntigravityTelemetry(duration_ms=10, telemetry_status="VERIFIED_MEASURED"),
        )

    monkeypatch.setattr(fake_base, "stream_task", _fake_stream)

    registry = EngineRegistry()
    registry.register_adapter(AntigravityEngineAdapter(adapter=fake_base))

    ws = tmp_path / "ws_master"
    denied = tmp_path / "ws_worker"
    ws.mkdir()
    denied.mkdir()

    res = registry.execute(
        EngineRequest(
            provider_id="antigravity",
            prompt="Plan work",
            workspace_path=str(ws),
            execution_mode="plan",
            denied_paths=[str(denied)],
            deny_shell_commands=True,
            capabilities_required=["text_generation", "streaming"],
            session_reference=EngineSessionReference(
                session_id="sess-master-1",
                binding_source="adapter_logical",
            ),
        )
    )
    assert res.status == "SUCCEEDED"
    assert len(captured) == 1
    assert captured[0].execution_mode == "plan"
    assert captured[0].require_sandbox is True
    assert captured[0].denied_read_write_paths == [str(denied)]
    assert captured[0].deny_shell_commands is True
