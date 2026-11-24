"""Unit and failure-matrix tests for CrewAI Flow and CrewAI Crew (requires isolated venv)."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

import pytest

pytestmark = pytest.mark.crewai
pytest.importorskip("crewai")

from crewai import Agent, Crew, Process, Task  # noqa: E402
from crewai.flow.persistence import SQLiteFlowPersistence  # noqa: E402

from core.orchestration import AttributedSpikeEvent  # noqa: E402
from core.orchestration.spike_coordinator import _SpikeEventCollector  # noqa: E402
from engines import (  # noqa: E402
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
from tests.benchmarks.p03_02.crewai_crew_prototype import (  # noqa: E402
    AntigravityEngineBaseLLM,
    run_crewai_crew_benchmark,
)
from tests.benchmarks.p03_02.crewai_flow_prototype import (  # noqa: E402
    P0302CrewAIFlow,
    run_crewai_flow_benchmark,
)
from tests.benchmarks.p03_02.custom_harness import run_custom_benchmark  # noqa: E402


class _FakeAdapter(EngineAdapter):
    """Scriptable EngineAdapter tracking calls across CrewAI Flow/Crew tests."""

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


def _make_engine_res(
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
        provider_id="antigravity",
        status="COMPLETED" if status == "SUCCEEDED" else "FAILED",
        redacted_payload={"conversation_id": conv_id} if conv_id else {},
    )
    if on_event is not None:
        on_event(ev)
    telemetry = (
        EngineTelemetry(
            duration_ms=20,
            input_tokens=100,
            output_tokens=25,
            thinking_tokens=5,
            cached_tokens=0,
            total_tokens=125,
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="provider_cli",
        )
        if with_usage
        else EngineTelemetry(
            duration_ms=20,
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
        provider_id="antigravity",
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
        duration_ms=20,
        telemetry=telemetry,
        events=[ev],
        error=error,
    )


def _plan_payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "task_summary": "Delegate artifact creation.",
        "target_worker_id": "it.artifact_worker",
        "worker_objective": "Read package.json and create proof.txt.",
        "allowed_read_paths": ["package.json"],
        "allowed_write_paths": ["proof.txt"],
        "forbidden_actions": ["Run shell commands"],
        "acceptance_criteria": ["package.json untouched", "proof.txt valid"],
        "artifact_expectation": {
            "required_substrings": ["name="],
            "expected_line_count": 2,
            "must_preserve_inputs": True,
        },
    }
    data.update(overrides)
    return data


def _synth_payload(sha_hex: str | None, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
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
    data.update(overrides)
    return data


@pytest.fixture
def env_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, str]:
    """Provide isolated directories including Flow DB and CrewAI storage inside tmp_path."""
    allowed_root = tmp_path / "bench"
    master_dir = allowed_root / "master_ws"
    worker_dir = allowed_root / "worker_ws"
    storage_dir = tmp_path / "crewai_storage"
    flow_db = tmp_path / "flow_persistence" / "states.db"
    master_dir.mkdir(parents=True)
    worker_dir.mkdir(parents=True)
    marker = "mk-crewai-unit-77"
    (worker_dir / "package.json").write_text(
        json.dumps({"name": "ai-village-crewai-unit", "verification_marker": marker}) + "\n",
        encoding="utf-8",
    )
    expected_proof = f"name=ai-village-crewai-unit\nverification_marker={marker}\n"
    return allowed_root, master_dir, worker_dir, flow_db, storage_dir, expected_proof


def _registry(adapter: EngineAdapter) -> EngineRegistry:
    reg = EngineRegistry()
    reg.register_adapter(adapter)
    return reg


def test_option_b_crewai_flow_happy_path_persistence_and_events(
    env_dirs: tuple[Path, Path, Path, Path, Path, str],
) -> None:
    """Option B: CrewAI Flow runs 6 stages, persists SQLite state locally, and streams Flow events."""
    allowed_root, master_dir, worker_dir, flow_db, storage_dir, expected_proof = env_dirs
    expected_sha = compute_sha256_bytes(expected_proof.encode("utf-8"))
    streamed: list[AttributedSpikeEvent] = []

    def _handler(req: EngineRequest, idx: int, cb: Callable[[EngineEvent], None] | None) -> EngineResult:
        if idx == 1:
            return _make_engine_res(req, response_text=json.dumps(_plan_payload()), conv_id="flow-m1", on_event=cb)
        if idx == 2:
            (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8", newline="\n")
            return _make_engine_res(req, response_text="done", conv_id="flow-w1", on_event=cb)
        return _make_engine_res(
            req,
            response_text=json.dumps(_synth_payload(expected_sha)),
            conv_id="flow-m2",
            on_event=cb,
        )

    outcome, flow = run_crewai_flow_benchmark(
        registry=_registry(_FakeAdapter(_handler)),
        user_request="Create proof.txt from package.json",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        persistence_db_path=flow_db,
        crewai_storage_dir=storage_dir,
        expected_artifact_content=expected_proof,
        on_event=streamed.append,
        allow_temp_workspace=True,
    )
    assert outcome.status == "DELIVERED"
    assert outcome.qa_verification is not None and outcome.qa_verification.status == "VERIFIED_PASS"
    assert outcome.timing.provider_call_count == 3
    assert outcome.timing.extra_reasoning_calls == 0
    assert outcome.timing.framework_event_count > 0 and len(outcome.framework_events) > 0
    assert outcome.notes["completed_stages"] == [
        "master_planning",
        "validate_master_plan",
        "worker_execution",
        "qa_verification",
        "master_synthesis",
        "final_handoff",
    ]
    assert flow_db.is_file()
    loader = SQLiteFlowPersistence(db_path=str(flow_db))
    loaded = loader.load_state(flow.state.id)
    assert loaded is not None and loaded["status"] == "DELIVERED"
    # Verify Flow lacks native cancellation API.
    assert not hasattr(P0302CrewAIFlow, "cancel")
    assert not hasattr(P0302CrewAIFlow, "abort")


def test_option_b_crewai_flow_failure_qa_and_persistence_reload(
    env_dirs: tuple[Path, Path, Path, Path, Path, str],
) -> None:
    """Option B: Worker failure halts Flow, marks QA FAIL, and saves recoverable error checkpoint."""
    allowed_root, master_dir, worker_dir, flow_db, storage_dir, expected_proof = env_dirs

    def _fail_handler(req: EngineRequest, idx: int, cb: Callable[[EngineEvent], None] | None) -> EngineResult:
        if idx == 1:
            return _make_engine_res(req, response_text=json.dumps(_plan_payload()), conv_id="flow-m-err", on_event=cb)
        return _make_engine_res(
            req,
            status="FAILED",
            conv_id="flow-w-err",
            error=EngineError(error_code="PROVIDER_ERROR", error_source="provider", message="Worker failed"),
            on_event=cb,
        )

    outcome, flow = run_crewai_flow_benchmark(
        registry=_registry(_FakeAdapter(_fail_handler)),
        user_request="Create proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        persistence_db_path=flow_db,
        crewai_storage_dir=storage_dir,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    assert outcome.status == "FAILED"
    assert outcome.qa_verification is not None and outcome.qa_verification.status == "VERIFIED_FAIL"
    assert outcome.error is not None and outcome.error.error_code == "WORKER_EXECUTION_FAILED"

    loader = SQLiteFlowPersistence(db_path=str(flow_db))
    restored = loader.load_state(flow.state.id)
    assert restored is not None
    assert restored["status"] == "FAILED"
    assert "validate_master_plan" in restored["completed_stages"]
    assert restored["error_payload"]["error_code"] == "WORKER_EXECUTION_FAILED"


def test_option_c_crewai_crew_prompt_overhead_and_telemetry_truth(
    env_dirs: tuple[Path, Path, Path, str] | Any,
) -> None:
    """Option C: CrewAI Crew wraps prompts (+chars), lacks ToolUsage* events, and zeroes UsageMetrics on None telemetry."""
    allowed_root, master_dir, worker_dir, _flow_db, storage_dir, expected_proof = env_dirs
    expected_sha = compute_sha256_bytes(expected_proof.encode("utf-8"))

    def _shared_handler(with_usage: bool) -> Callable[[EngineRequest, int, Callable[[EngineEvent], None] | None], EngineResult]:
        def _fn(req: EngineRequest, idx: int, cb: Callable[[EngineEvent], None] | None) -> EngineResult:
            if idx == 1:
                return _make_engine_res(
                    req,
                    response_text=json.dumps(_plan_payload()),
                    conv_id="c1",
                    with_usage=with_usage,
                    on_event=cb,
                )
            if idx == 2:
                (worker_dir / "proof.txt").write_text(expected_proof, encoding="utf-8", newline="\n")
                return _make_engine_res(
                    req,
                    response_text="Created proof.txt",
                    conv_id="w1",
                    with_usage=with_usage,
                    on_event=cb,
                )
            return _make_engine_res(
                req,
                response_text=json.dumps(_synth_payload(expected_sha)),
                conv_id="c2",
                with_usage=with_usage,
                on_event=cb,
            )

        return _fn

    custom_out, _ = run_custom_benchmark(
        registry=_registry(_FakeAdapter(_shared_handler(with_usage=False))),
        user_request="Create proof.txt from package.json",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    crew_out = run_crewai_crew_benchmark(
        registry=_registry(_FakeAdapter(_shared_handler(with_usage=False))),
        user_request="Create proof.txt from package.json",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        crewai_storage_dir=storage_dir,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )

    assert crew_out.status == "DELIVERED"
    assert crew_out.qa_verification is not None and crew_out.qa_verification.status == "VERIFIED_PASS"
    # CrewAI Agent/Task prompt template strictly expands prompt sizes compared to direct dispatch.
    assert crew_out.timing.prompt_chars_total > custom_out.timing.prompt_chars_total
    # Antigravity CLI handles native tools internally, so CrewAI emits 0 ToolUsage* events.
    assert crew_out.notes["crew_tool_events"] == 0
    # Contrast CrewOutput.token_usage integer 0s vs honest None telemetry on BenchmarkTimingBreakdown.
    assert crew_out.framework_token_usage is not None
    assert crew_out.framework_token_usage["total_tokens"] == 0
    assert crew_out.timing.telemetry_complete is False
    assert crew_out.timing.total_tokens_total is None
    assert not hasattr(Crew, "cancel")
    assert not hasattr(Crew, "abort")


def test_option_c_crewai_crew_guardrail_retry_causes_extra_calls_and_blocks_qa_fail(
    env_dirs: tuple[Path, Path, Path, Path, Path, str],
) -> None:
    """Option C: Task guardrail retry generates extra EngineRegistry calls; QA FAIL prevents false delivery."""
    allowed_root, master_dir, worker_dir, _flow_db, storage_dir, expected_proof = env_dirs

    # 1. Prove guardrail retry adds reasoning calls beyond baseline.
    adapter = _FakeAdapter(
        lambda req, idx, cb: _make_engine_res(
            req,
            response_text="not json" if idx == 1 else json.dumps(_plan_payload()),
            conv_id=f"gr-{idx}",
            on_event=cb,
        )
    )
    collector = _SpikeEventCollector()
    llm = AntigravityEngineBaseLLM(model="antigravity/test")
    llm.bind_execution_context(
        registry=_registry(adapter),
        collector=collector,
        agent_id="core.master",
        role="orchestrator",
        stage="master_plan",
        session_id="sess-m-gr",
        workspace_dir=master_dir,
        denied_dir=worker_dir,
        execution_mode="plan",
        capabilities_required=["text_generation"],
        max_calls=2,
    )
    ag = Agent(
        role="Master",
        goal="Return valid JSON.",
        backstory="Master",
        llm=llm,
        allow_delegation=False,
        max_iter=1,
        max_retry_limit=0,
        use_system_prompt=False,
    )

    def _json_guardrail(output: Any) -> tuple[bool, Any]:
        raw = getattr(output, "raw", "")
        return (raw.strip().startswith("{"), raw if raw.strip().startswith("{") else "Must return JSON.")

    tk = Task(
        description="Produce plan JSON.",
        expected_output="JSON",
        agent=ag,
        guardrail=_json_guardrail,
        guardrail_max_retries=1,
    )
    crew = Crew(agents=[ag], tasks=[tk], process=Process.sequential, memory=False, tracing=False)
    out = crew.kickoff()
    assert out.raw.strip().startswith("{")
    assert len(adapter.requests) == 2

    # 2. Prove Worker false success with missing artifact stays REJECTED_BY_QA in Option C.
    def _bad_artifact_handler(req: EngineRequest, idx: int, cb: Callable[[EngineEvent], None] | None) -> EngineResult:
        if idx == 1:
            return _make_engine_res(req, response_text=json.dumps(_plan_payload()), conv_id="c-m-qa", on_event=cb)
        if idx == 2:
            if (worker_dir / "proof.txt").exists():
                (worker_dir / "proof.txt").unlink()
            return _make_engine_res(req, response_text="All done!", conv_id="c-w-qa", on_event=cb)
        return _make_engine_res(
            req,
            response_text=json.dumps(
                _synth_payload(None, delivery_status="REJECTED", qa_status_observed="VERIFIED_FAIL")
            ),
            conv_id="c-ms-qa",
            on_event=cb,
        )

    qa_fail_outcome = run_crewai_crew_benchmark(
        registry=_registry(_FakeAdapter(_bad_artifact_handler)),
        user_request="Create proof.txt",
        master_workspace_dir=master_dir,
        worker_workspace_dir=worker_dir,
        allowed_root=allowed_root,
        crewai_storage_dir=storage_dir,
        expected_artifact_content=expected_proof,
        allow_temp_workspace=True,
    )
    assert qa_fail_outcome.status == "REJECTED_BY_QA"
    assert qa_fail_outcome.qa_verification is not None
    assert qa_fail_outcome.qa_verification.status == "VERIFIED_FAIL"
