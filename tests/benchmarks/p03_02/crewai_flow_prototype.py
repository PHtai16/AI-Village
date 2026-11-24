"""Option B prototype orchestrating P03.01 workflow via CrewAI Flow and SQLiteFlowPersistence."""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Sequence
import uuid

from crewai.events import BaseEventListener, crewai_event_bus
from crewai.events.types.flow_events import (
    FlowCreatedEvent,
    FlowFailedEvent,
    FlowFinishedEvent,
    FlowStartedEvent,
    MethodExecutionFailedEvent,
    MethodExecutionFinishedEvent,
    MethodExecutionStartedEvent,
)
from crewai.flow.flow import Flow, listen, router, start
from crewai.flow.persistence import SQLiteFlowPersistence
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.orchestration import (
    AttributedSpikeEvent,
    MasterPlan,
    MasterSynthesis,
    OrchestrationError,
    SpikeAgentSpec,
    WorkerHandoff,
    build_default_spike_roster,
    parse_strict_json_object,
    validate_master_plan_policy,
)
from core.orchestration.spike_coordinator import (
    _SpikeEventCollector,
    _build_master_plan_prompt,
    _build_master_synthesis_prompt,
    _build_worker_prompt,
    _execute_stage,
    _preflight_validate,
    _snapshot_workspace_tree,
    _validate_synthesis_truth,
)
from engines.antigravity.diagnostics import redact_secrets
from engines.antigravity.verification import snapshot_file_hashes
from engines.contract import EngineResult
from engines.registry import EngineRegistry
from engines.verification import EngineArtifactVerificationResult, verify_engine_artifact
from tests.benchmarks.p03_02.models import (
    BenchmarkRunOutcome,
    aggregate_timing_breakdown,
)

logger = logging.getLogger(__name__)


class P0302FlowState(BaseModel):
    """Serializable CrewAI Flow state persisted after each milestone step."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: f"flow-{uuid.uuid4().hex[:12]}")
    correlation_id: str = ""
    status: str = "RUNNING"
    completed_stages: list[str] = Field(default_factory=list)
    master_plan_payload: dict[str, Any] | None = None
    worker_handoff_payload: dict[str, Any] | None = None
    qa_verification_payload: dict[str, Any] | None = None
    master_synthesis_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None


class _ScopedFlowEventSpy:
    """Capture CrewAI Flow bus events scoped to a single benchmark flow instance."""

    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id
        self.records: list[dict[str, Any]] = []
        self._listener: _FlowBusListener | None = None

    def attach(self) -> None:
        """Attach listener instance to global ``crewai_event_bus``."""
        self._listener = _FlowBusListener(self)

    def maybe_record(self, event_name: str, event: Any) -> None:
        """Record only events belonging to ``self.flow_id`` when present."""
        ev_flow_id = getattr(event, "flow_id", None) or getattr(event, "flow_name", None)
        if hasattr(event, "flow_id") and getattr(event, "flow_id") != self.flow_id:
            return
        self.records.append(
            {
                "event_type": event_name,
                "flow_id": ev_flow_id,
                "method_name": getattr(event, "method_name", None),
            }
        )


class _FlowBusListener(BaseEventListener):
    """Listener hooking CrewAI Flow lifecycle events for observability benchmarking."""

    def __init__(self, spy: _ScopedFlowEventSpy) -> None:
        self._spy = spy
        super().__init__()

    def setup_listeners(self, bus: Any) -> None:
        """Subscribe to CrewAI Flow lifecycle events."""
        for cls in (
            FlowCreatedEvent,
            FlowStartedEvent,
            MethodExecutionStartedEvent,
            MethodExecutionFinishedEvent,
            MethodExecutionFailedEvent,
            FlowFinishedEvent,
            FlowFailedEvent,
        ):
            name = cls.__name__

            @bus.on(cls)
            def _handler(_source: Any, event: Any, _ev_name: str = name) -> None:
                self._spy.maybe_record(_ev_name, event)


@contextmanager
def _isolated_crewai_env(storage_dir: Path) -> Iterator[None]:
    """Keep CrewAI telemetry disabled and route storage into an isolated benchmark dir."""
    keys = ("CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED", "CREWAI_STORAGE_DIR")
    previous = {k: os.environ.get(k) for k in keys}
    storage_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ["CREWAI_STORAGE_DIR"] = str(storage_dir)
    try:
        yield
    finally:
        for key, old_val in previous.items():
            if old_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_val


class P0302CrewAIFlow(Flow[P0302FlowState]):
    """Six-stage CrewAI Flow prototype delegating all AI work to ``EngineRegistry``."""

    def __init__(
        self,
        *,
        registry: EngineRegistry,
        user_request: str,
        master_workspace_dir: Path,
        worker_workspace_dir: Path,
        allowed_root: Path,
        persistence_db_path: Path,
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
        flow_id: str | None = None,
    ) -> None:
        sqlite_persistence = SQLiteFlowPersistence(db_path=str(persistence_db_path))
        corr_id = f"corr-flow-{uuid.uuid4().hex[:10]}"
        super().__init__(persistence=sqlite_persistence)
        self.state.correlation_id = corr_id
        if flow_id is not None:
            self.state.id = flow_id

        self._registry = registry
        self._user_request = user_request
        self._master_ws_input = master_workspace_dir
        self._worker_ws_input = worker_workspace_dir
        self._allowed_root = allowed_root
        self._expected_artifact_relpath = expected_artifact_relpath
        self._permitted_read_relpaths = tuple(permitted_read_relpaths)
        self._permitted_write_relpaths = tuple(permitted_write_relpaths)
        self._expected_artifact_content = expected_artifact_content
        self._required_artifact_substrings = tuple(required_artifact_substrings)
        self._provider_id = provider_id
        self._connection_id = connection_id
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._roster = roster if roster is not None else build_default_spike_roster()
        self._collector = _SpikeEventCollector(downstream=on_event)
        self._m_session = (master_session_id or f"sess-master-{uuid.uuid4().hex[:10]}").strip()
        self._w_session = (worker_session_id or f"sess-worker-{uuid.uuid4().hex[:10]}").strip()
        self._allow_temp_workspace = allow_temp_workspace
        self._persistence_db_path = persistence_db_path

        self.engine_results: list[EngineResult] = []
        self.prompt_chars_total: int = 0
        self.verification_duration_ms: int = 0
        self.qa_verification: EngineArtifactVerificationResult | None = None
        self.conversation_ids: list[str] = []

        self._master_ws: Path | None = None
        self._worker_ws: Path | None = None
        self._master_spec: SpikeAgentSpec | None = None
        self._worker_spec: SpikeAgentSpec | None = None
        self._clean_reads: list[str] = []
        self._clean_writes: list[str] = []
        self._clean_expected: str = expected_artifact_relpath
        self._pre_master_worker_tree: dict[str, str] = {}
        self._pre_worker_master_tree: dict[str, str] = {}
        self._protected_hashes: dict[str, str] = {}
        self._master_plan: MasterPlan | None = None
        self._handoff: WorkerHandoff | None = None
        self._worker_result: EngineResult | None = None
        self._master_plan_conv_id: str | None = None
        self._worker_conv_id: str | None = None

    def _save_step(self, stage_name: str) -> None:
        """Persist current Flow state into isolated SQLite after a milestone completes."""
        self.state.completed_stages.append(stage_name)
        if self.persistence is not None:
            self.persistence.save_state(self.state.id, stage_name, self.state)

    def _fail(self, status: str, error: OrchestrationError, stage_name: str) -> str:
        """Record structured fail-closed state and persist before halting Flow."""
        self.state.status = status
        self.state.error_payload = error.model_dump(mode="json")
        self._save_step(stage_name)
        return "abort_halt"

    @start()
    def master_planning(self) -> str:
        """Stage 1: Preflight isolation checks and Master planning turn via Antigravity."""
        preflight = _preflight_validate(
            master_workspace_dir=self._master_ws_input,
            worker_workspace_dir=self._worker_ws_input,
            allowed_root=self._allowed_root,
            allow_temp_workspace=self._allow_temp_workspace,
            roster=self._roster,
            master_agent_id="core.master",
            permitted_read_relpaths=self._permitted_read_relpaths,
            permitted_write_relpaths=self._permitted_write_relpaths,
            expected_artifact_relpath=self._expected_artifact_relpath,
            master_session_id=self._m_session,
            worker_session_id=self._w_session,
        )
        if isinstance(preflight, OrchestrationError):
            early_status = (
                "BLOCKED"
                if preflight.error_code
                in {"INVALID_WORKSPACE_ISOLATION", "SESSION_ISOLATION_VIOLATION", "POLICY_VIOLATION"}
                else "FAILED"
            )
            return self._fail(early_status, preflight, "master_planning")

        (
            self._master_ws,
            self._worker_ws,
            self._master_spec,
            self._clean_reads,
            self._clean_writes,
            self._clean_expected,
        ) = preflight
        self._pre_master_worker_tree = _snapshot_workspace_tree(self._worker_ws)

        prompt = _build_master_plan_prompt(
            user_request=self._user_request,
            master_spec=self._master_spec,
            permitted_read_relpaths=self._clean_reads,
            permitted_write_relpaths=self._clean_writes,
            expected_artifact_relpath=self._clean_expected,
        )
        self.prompt_chars_total += len(prompt)
        plan_res = _execute_stage(
            registry=self._registry,
            collector=self._collector,
            stage="master_plan",
            agent_spec=self._master_spec,
            session_id=self._m_session,
            workspace_dir=self._master_ws,
            denied_dir=self._worker_ws,
            prompt=prompt,
            provider_id=self._provider_id,
            connection_id=self._connection_id,
            model_id=self._model_id,
            timeout_seconds=self._timeout_seconds,
            capabilities_required=["text_generation", "streaming"],
            correlation_id=self.state.correlation_id,
        )
        self.engine_results.append(plan_res)
        self._master_plan_conv_id = (
            plan_res.session_reference.provider_conversation_id
            if plan_res.session_reference
            else None
        )
        if self._master_plan_conv_id:
            self.conversation_ids.append(self._master_plan_conv_id)

        if _snapshot_workspace_tree(self._worker_ws) != self._pre_master_worker_tree:
            return self._fail(
                "BLOCKED",
                OrchestrationError(
                    error_code="POLICY_VIOLATION",
                    stage="master_plan",
                    message="Master stage mutated worker workspace directly; failing closed.",
                ),
                "master_planning",
            )

        if plan_res.status != "SUCCEEDED":
            err_msg = (
                plan_res.error.message
                if plan_res.error
                else f"Master planning returned status={plan_res.status}."
            )
            return self._fail(
                "BLOCKED" if plan_res.status == "BLOCKED" else "FAILED",
                OrchestrationError(
                    error_code="MASTER_PLAN_EXECUTION_FAILED",
                    stage="master_plan",
                    message=err_msg,
                    details={"engine_status": plan_res.status},
                ),
                "master_planning",
            )

        self.state.master_plan_payload = {"raw_text": plan_res.response_text or ""}
        self._save_step("master_planning")
        return "validate_plan"

    @router(master_planning)
    def route_after_planning(self, branch: str) -> str:
        """Route Flow linearly or halt fail-closed."""
        return branch

    @listen("validate_plan")
    def validate_master_plan(self) -> str:
        """Stage 2: Parse strict JSON MasterPlan and validate policy bounds."""
        assert self._worker_ws is not None and self._master_ws is not None
        assert self._master_spec is not None
        raw_text = (self.state.master_plan_payload or {}).get("raw_text")
        try:
            parsed = parse_strict_json_object(raw_text)
            self._master_plan = MasterPlan.model_validate(parsed)
        except (ValueError, ValidationError) as exc:
            return self._fail(
                "FAILED",
                OrchestrationError(
                    error_code="MASTER_PLAN_PARSE_FAILED",
                    stage="master_plan",
                    message=f"Master plan output violated strict JSON schema: {exc}",
                ),
                "validate_master_plan",
            )

        worker_spec, policy_err = validate_master_plan_policy(
            plan=self._master_plan,
            master_spec=self._master_spec,
            roster=self._roster,
            worker_ws=self._worker_ws,
            permitted_reads=self._clean_reads,
            permitted_writes=self._clean_writes,
            expected_artifact_relpath=self._clean_expected,
        )
        if policy_err is not None or worker_spec is None:
            return self._fail("BLOCKED", policy_err or OrchestrationError(
                error_code="POLICY_VIOLATION",
                stage="handoff_policy",
                message="Policy validation failed.",
            ), "validate_master_plan")

        self._worker_spec = worker_spec
        self._handoff = WorkerHandoff(
            correlation_id=self.state.correlation_id,
            from_agent_id=self._master_spec.agent_id,
            to_agent_id=worker_spec.agent_id,
            worker_session_id=self._w_session,
            worker_workspace_path=str(self._worker_ws),
            denied_paths=[str(self._master_ws)],
            deny_shell_commands=True,
            plan=self._master_plan,
            worker_instructions=self._master_plan.worker_objective,
        )
        self.state.master_plan_payload = self._master_plan.model_dump(mode="json")
        self.state.worker_handoff_payload = self._handoff.model_dump(mode="json")
        self._save_step("validate_master_plan")
        return "execute_worker"

    @router(validate_master_plan)
    def route_after_validation(self, branch: str) -> str:
        """Route Flow to worker execution or halt fail-closed."""
        return branch

    @listen("execute_worker")
    def worker_execution(self) -> str:
        """Stage 3: Execute delegated Worker stage with isolated workspace and session."""
        assert self._worker_ws is not None and self._master_ws is not None
        assert self._worker_spec is not None and self._handoff is not None
        assert self._master_plan is not None
        existing_protected = [
            rel
            for rel in sorted(set(self._clean_reads) | set(self._master_plan.allowed_read_paths))
            if (self._worker_ws / rel).is_file()
        ]
        self._protected_hashes = snapshot_file_hashes(self._worker_ws, existing_protected)
        self._pre_worker_master_tree = _snapshot_workspace_tree(self._master_ws)

        worker_prompt = _build_worker_prompt(self._handoff)
        self.prompt_chars_total += len(worker_prompt)
        worker_res = _execute_stage(
            registry=self._registry,
            collector=self._collector,
            stage="worker_execute",
            agent_spec=self._worker_spec,
            session_id=self._w_session,
            workspace_dir=self._worker_ws,
            denied_dir=self._master_ws,
            prompt=worker_prompt,
            provider_id=self._provider_id,
            connection_id=self._connection_id,
            model_id=self._model_id,
            timeout_seconds=self._timeout_seconds,
            capabilities_required=["streaming", "native_tool_execution", "workspace_access"],
            correlation_id=self.state.correlation_id,
        )
        self.engine_results.append(worker_res)
        self._worker_result = worker_res
        self._worker_conv_id = (
            worker_res.session_reference.provider_conversation_id
            if worker_res.session_reference
            else None
        )
        if self._worker_conv_id:
            self.conversation_ids.append(self._worker_conv_id)

        if _snapshot_workspace_tree(self._master_ws) != self._pre_worker_master_tree:
            return self._fail(
                "BLOCKED",
                OrchestrationError(
                    error_code="SESSION_ISOLATION_VIOLATION",
                    stage="worker_execute",
                    message="Worker stage mutated Master workspace directly; failing closed.",
                ),
                "worker_execution",
            )

        returned_sid = worker_res.session_reference.session_id if worker_res.session_reference else None
        if returned_sid == self._m_session or (
            self._master_plan_conv_id
            and self._worker_conv_id
            and self._master_plan_conv_id == self._worker_conv_id
        ):
            return self._fail(
                "BLOCKED",
                OrchestrationError(
                    error_code="SESSION_ISOLATION_VIOLATION",
                    stage="worker_execute",
                    message="Provider returned colliding Master and Worker session/conversation IDs.",
                ),
                "worker_execution",
            )

        self._save_step("worker_execution")
        return "verify_qa"

    @router(worker_execution)
    def route_after_worker(self, branch: str) -> str:
        """Route Flow to independent QA or halt on isolation violation."""
        return branch

    @listen("verify_qa")
    def qa_verification(self) -> str:
        """Stage 4: Run independent Python QA disk and SHA-256 verification."""
        assert self._worker_ws is not None and self._worker_result is not None
        t_qa = time.perf_counter()
        self.qa_verification = verify_engine_artifact(
            workspace_dir=self._worker_ws,
            artifact_relpath=self._clean_expected,
            engine_result=self._worker_result,
            expected_content=self._expected_artifact_content,
            required_substrings=self._required_artifact_substrings,
            protected_source_hashes=self._protected_hashes,
        )
        self.verification_duration_ms += int((time.perf_counter() - t_qa) * 1000)
        self.state.qa_verification_payload = self.qa_verification.model_dump(mode="json")

        if self._worker_result.status != "SUCCEEDED":
            err_msg = (
                self._worker_result.error.message
                if self._worker_result.error
                else f"Worker execution returned status={self._worker_result.status}."
            )
            return self._fail(
                "BLOCKED" if self._worker_result.status == "BLOCKED" else "FAILED",
                OrchestrationError(
                    error_code="WORKER_EXECUTION_FAILED",
                    stage="worker_execute",
                    message=err_msg,
                    details={"engine_status": self._worker_result.status},
                ),
                "qa_verification",
            )

        self._save_step("qa_verification")
        return "synthesize"

    @router(qa_verification)
    def route_after_qa(self, branch: str) -> str:
        """Route Flow to Master synthesis or halt on worker execution failure."""
        return branch

    @listen("synthesize")
    def master_synthesis(self) -> str:
        """Stage 5: Synthesize final delivery status strictly checked against QA truth."""
        assert self._master_spec is not None and self._master_ws is not None
        assert self._worker_ws is not None and self._handoff is not None
        assert self._worker_result is not None and self.qa_verification is not None

        synth_prompt = _build_master_synthesis_prompt(
            user_request=self._user_request,
            master_spec=self._master_spec,
            handoff=self._handoff,
            worker_result=self._worker_result,
            qa_verification=self.qa_verification,
        )
        self.prompt_chars_total += len(synth_prompt)
        synth_res = _execute_stage(
            registry=self._registry,
            collector=self._collector,
            stage="master_synthesize",
            agent_spec=self._master_spec,
            session_id=self._m_session,
            workspace_dir=self._master_ws,
            denied_dir=self._worker_ws,
            prompt=synth_prompt,
            provider_id=self._provider_id,
            connection_id=self._connection_id,
            model_id=self._model_id,
            timeout_seconds=self._timeout_seconds,
            capabilities_required=["text_generation", "streaming"],
            correlation_id=self.state.correlation_id,
        )
        self.engine_results.append(synth_res)
        synth_conv_id = (
            synth_res.session_reference.provider_conversation_id
            if synth_res.session_reference
            else None
        )
        if synth_conv_id:
            self.conversation_ids.append(synth_conv_id)

        if synth_conv_id and self._worker_conv_id and synth_conv_id == self._worker_conv_id:
            return self._fail(
                "BLOCKED",
                OrchestrationError(
                    error_code="SESSION_ISOLATION_VIOLATION",
                    stage="master_synthesize",
                    message="Master synthesis provider conversation collided with Worker conversation.",
                ),
                "master_synthesis",
            )

        if synth_res.status != "SUCCEEDED":
            err_msg = (
                synth_res.error.message
                if synth_res.error
                else f"Master synthesis returned status={synth_res.status}."
            )
            return self._fail(
                "FAILED",
                OrchestrationError(
                    error_code="MASTER_SYNTHESIS_EXECUTION_FAILED",
                    stage="master_synthesize",
                    message=err_msg,
                    details={"engine_status": synth_res.status},
                ),
                "master_synthesis",
            )

        try:
            synth_payload = parse_strict_json_object(synth_res.response_text)
            synthesis = MasterSynthesis.model_validate(synth_payload)
        except (ValueError, ValidationError) as exc:
            return self._fail(
                "FAILED",
                OrchestrationError(
                    error_code="MASTER_SYNTHESIS_PARSE_FAILED",
                    stage="master_synthesize",
                    message=f"Master synthesis output violated strict JSON schema: {exc}",
                ),
                "master_synthesis",
            )

        self.state.master_synthesis_payload = synthesis.model_dump(mode="json")
        truth_err = _validate_synthesis_truth(
            synthesis=synthesis,
            worker_result=self._worker_result,
            qa_verification=self.qa_verification,
        )
        if truth_err is not None:
            return self._fail("FAILED", truth_err, "master_synthesis")

        if self.qa_verification.status == "VERIFIED_FAIL":
            return self._fail(
                "REJECTED_BY_QA",
                OrchestrationError(
                    error_code="QA_VERIFICATION_FAILED",
                    stage="qa_verify",
                    message="; ".join(self.qa_verification.failure_reasons)
                    or "Independent QA failed.",
                ),
                "master_synthesis",
            )

        self._save_step("master_synthesis")
        return "finalize"

    @router(master_synthesis)
    def route_after_synthesis(self, branch: str) -> str:
        """Route Flow to final handoff or halt on QA rejection / truth mismatch."""
        return branch

    @listen("finalize")
    def final_handoff(self) -> str:
        """Stage 6: Finalize verified delivery and persist terminal checkpoint."""
        self.state.status = "DELIVERED"
        self._save_step("final_handoff")
        return "DELIVERED"

    @listen("abort_halt")
    def handle_abort_halt(self) -> str:
        """Terminal halt sink preserving already-recorded failure/block status."""
        if self.persistence is not None:
            self.persistence.save_state(self.state.id, "abort_halt", self.state)
        return self.state.status


def run_crewai_flow_benchmark(
    *,
    registry: EngineRegistry,
    user_request: str,
    master_workspace_dir: Path,
    worker_workspace_dir: Path,
    allowed_root: Path,
    persistence_db_path: Path,
    crewai_storage_dir: Path,
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
) -> tuple[BenchmarkRunOutcome, P0302CrewAIFlow]:
    """Execute Option B (CrewAI Flow) inside an isolated environment and return metrics."""
    persistence_db_path.parent.mkdir(parents=True, exist_ok=True)
    with _isolated_crewai_env(crewai_storage_dir):
        flow = P0302CrewAIFlow(
            registry=registry,
            user_request=user_request,
            master_workspace_dir=master_workspace_dir,
            worker_workspace_dir=worker_workspace_dir,
            allowed_root=allowed_root,
            persistence_db_path=persistence_db_path,
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
        spy = _ScopedFlowEventSpy(flow.state.id)
        spy.attach()
        t0 = time.perf_counter()
        flow.kickoff()
        wall_ms = int((time.perf_counter() - t0) * 1000)

    error_obj = (
        OrchestrationError.model_validate(flow.state.error_payload)
        if flow.state.error_payload is not None
        else None
    )
    observed_tools = (
        list(flow._worker_result.observed_tool_names)
        if flow._worker_result is not None
        else []
    )
    timing = aggregate_timing_breakdown(
        total_duration_ms=wall_ms,
        verification_duration_ms=flow.verification_duration_ms,
        engine_results=flow.engine_results,
        prompt_chars_total=flow.prompt_chars_total,
        attributed_event_count=len(flow._collector.items),
        framework_event_count=len(spy.records),
        expected_baseline_calls=3,
    )
    outcome = BenchmarkRunOutcome(
        option="crewai_flow",
        status=flow.state.status,  # type: ignore[arg-type]
        correlation_id=flow.state.correlation_id,
        master_session_id=flow._m_session,
        worker_session_id=flow._w_session if flow._handoff is not None else None,
        provider_conversation_ids=flow.conversation_ids,
        observed_tool_names=observed_tools,
        qa_verification=flow.qa_verification,
        engine_results=flow.engine_results,
        attributed_events=flow._collector.items,
        framework_events=spy.records,
        framework_token_usage=None,
        persistence_db_path=str(persistence_db_path),
        flow_id=flow.state.id,
        error=error_obj,
        timing=timing,
        notes={
            "completed_stages": list(flow.state.completed_stages),
            "persisted_db_exists": persistence_db_path.is_file(),
            "flow_event_bus_attached": bool(crewai_event_bus),
            "cancel_api_available": False,
        },
    )
    logger.info(
        "Option B benchmark correlation=%s status=%s total_ms=%s framework_ms=%s flow_events=%s",
        outcome.correlation_id,
        outcome.status,
        timing.total_duration_ms,
        timing.framework_overhead_ms,
        len(spy.records),
    )
    return outcome, flow
