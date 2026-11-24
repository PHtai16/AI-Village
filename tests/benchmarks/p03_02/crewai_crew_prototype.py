"""Option C prototype bridging CrewAI Crew Agents/Tasks to EngineRegistry via BaseLLM."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Literal, Sequence
import uuid

from crewai import Agent, BaseLLM, Crew, Process, Task
from crewai.events import BaseEventListener
from crewai.events.types.agent_events import (
    AgentExecutionCompletedEvent,
    AgentExecutionErrorEvent,
    AgentExecutionStartedEvent,
)
from crewai.events.types.crew_events import (
    CrewKickoffCompletedEvent,
    CrewKickoffFailedEvent,
    CrewKickoffStartedEvent,
)
from crewai.events.types.llm_events import (
    LLMCallCompletedEvent,
    LLMCallFailedEvent,
    LLMCallStartedEvent,
)
from crewai.events.types.task_events import (
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
)
from crewai.events.types.tool_usage_events import (
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)
from pydantic import PrivateAttr, ValidationError

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
    _preflight_validate,
    _snapshot_workspace_tree,
    _validate_synthesis_truth,
)
from engines.antigravity.verification import snapshot_file_hashes
from engines.contract import (
    EngineRequest,
    EngineResult,
    EngineSessionReference,
)
from engines.registry import EngineRegistry
from engines.verification import EngineArtifactVerificationResult, verify_engine_artifact
from tests.benchmarks.p03_02.crewai_flow_prototype import _isolated_crewai_env
from tests.benchmarks.p03_02.models import (
    BenchmarkRunOutcome,
    aggregate_timing_breakdown,
)

logger = logging.getLogger(__name__)


class EngineExecutionHaltedError(RuntimeError):
    """Non-rate-limit exception failing closed without triggering CrewAI retry loops."""


class AntigravityEngineBaseLLM(BaseLLM):
    """Fail-closed CrewAI ``BaseLLM`` bridge dispatching strictly through ``EngineRegistry``."""

    model: str = "antigravity/default"
    _registry: Any = PrivateAttr()
    _collector: Any = PrivateAttr()
    _agent_id: str = PrivateAttr()
    _role: str = PrivateAttr()
    _stage: str = PrivateAttr()
    _session_id: str = PrivateAttr()
    _workspace_dir: Path = PrivateAttr()
    _denied_dir: Path = PrivateAttr()
    _execution_mode: Literal["plan", "accept-edits"] = PrivateAttr()
    _capabilities_required: list[str] = PrivateAttr()
    _deny_shell_commands: bool = PrivateAttr(default=True)
    _provider_id: str = PrivateAttr(default="antigravity")
    _connection_id: str | None = PrivateAttr(default=None)
    _model_id: str | None = PrivateAttr(default=None)
    _timeout_seconds: int = PrivateAttr(default=90)
    _correlation_id: str = PrivateAttr(default="")
    _max_calls: int = PrivateAttr(default=1)
    _track_usage_into_crewai: bool = PrivateAttr(default=True)

    _requests: list[EngineRequest] = PrivateAttr(default_factory=list)
    _results: list[EngineResult] = PrivateAttr(default_factory=list)
    _prompt_chars: int = PrivateAttr(default=0)
    _raw_engine_responses: list[str | None] = PrivateAttr(default_factory=list)

    @property
    def requests(self) -> list[EngineRequest]:
        """Return captured ``EngineRequest`` objects dispatched through this LLM."""
        return self._requests

    @property
    def results(self) -> list[EngineResult]:
        """Return captured ``EngineResult`` objects produced by ``EngineRegistry``."""
        return self._results

    @property
    def prompt_chars(self) -> int:
        """Return total flattened prompt character count forwarded to the engine."""
        return self._prompt_chars

    @property
    def raw_engine_responses(self) -> list[str | None]:
        """Return verbatim response strings returned by ``EngineResult``."""
        return self._raw_engine_responses

    def bind_execution_context(
        self,
        *,
        registry: EngineRegistry,
        collector: _SpikeEventCollector,
        agent_id: str,
        role: str,
        stage: str,
        session_id: str,
        workspace_dir: Path,
        denied_dir: Path,
        execution_mode: Literal["plan", "accept-edits"],
        capabilities_required: list[str],
        deny_shell_commands: bool = True,
        provider_id: str = "antigravity",
        connection_id: str | None = None,
        model_id: str | None = None,
        timeout_seconds: int = 90,
        correlation_id: str = "",
        max_calls: int = 1,
        track_usage_into_crewai: bool = True,
    ) -> None:
        """Bind immutable per-agent session, workspace, and boundary context."""
        self._registry = registry
        self._collector = collector
        self._agent_id = agent_id
        self._role = role
        self._stage = stage
        self._session_id = session_id
        self._workspace_dir = workspace_dir
        self._denied_dir = denied_dir
        self._execution_mode = execution_mode
        self._capabilities_required = list(capabilities_required)
        self._deny_shell_commands = deny_shell_commands
        self._provider_id = provider_id
        self._connection_id = connection_id
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._correlation_id = correlation_id
        self._max_calls = max_calls
        self._track_usage_into_crewai = track_usage_into_crewai

    def supports_function_calling(self) -> bool:
        """Keep CrewAI native function-calling disabled so Antigravity owns tool execution."""
        return False

    def get_context_window_size(self) -> int:
        """Avoid the 4096-token default truncation threshold on BaseLLM."""
        return 32768

    @staticmethod
    def _flatten_messages(messages: str | list[Any]) -> str:
        """Flatten CrewAI message history dicts cleanly into a single CLI prompt."""
        if isinstance(messages, str):
            return messages
        parts: list[str] = []
        for msg in messages:
            if isinstance(msg, dict):
                role_name = str(msg.get("role", "user")).upper()
                content = str(msg.get("content", ""))
                parts.append(f"[{role_name}]\n{content}")
            else:
                parts.append(str(msg))
        return "\n\n".join(parts)

    def call(
        self,
        messages: str | list[Any],
        tools: list[dict[str, Any]] | None = None,
        callbacks: list[Any] | None = None,
        available_functions: dict[str, Any] | None = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
        **kwargs: Any,
    ) -> str:
        """Forward CrewAI Agent LLM invocation through ``EngineRegistry.execute`` fail-closed."""
        _ = (tools, callbacks, available_functions, from_task, from_agent, response_model, kwargs)
        if len(self._results) >= self._max_calls:
            raise EngineExecutionHaltedError(
                f"Call cap exceeded for {self._agent_id} ({self._max_calls} max_calls)."
            )

        flattened_prompt = self._flatten_messages(messages)
        self._prompt_chars += len(flattened_prompt)
        req = EngineRequest(
            provider_id=self._provider_id,
            connection_id=self._connection_id,
            model_id=self._model_id,
            prompt=flattened_prompt,
            workspace_path=str(self._workspace_dir),
            timeout_seconds=self._timeout_seconds,
            capabilities_required=self._capabilities_required,  # type: ignore[arg-type]
            execution_mode=self._execution_mode,
            denied_paths=[str(self._denied_dir)],
            deny_shell_commands=self._deny_shell_commands,
            session_reference=EngineSessionReference(
                session_id=self._session_id,
                binding_source="adapter_logical",
            ),
            metadata={
                "correlation_id": self._correlation_id,
                "agent_id": self._agent_id,
                "system_role": self._role,
                "stage": self._stage,
            },
        )
        self._requests.append(req)
        callback = self._collector.make_callback(
            stage=self._stage,
            agent_id=self._agent_id,
            role=self._role,
            logical_session_id=self._session_id,
        )
        res: EngineResult = self._registry.execute(req, on_event=callback)
        self._results.append(res)
        self._raw_engine_responses.append(res.response_text)
        conv_id = res.session_reference.provider_conversation_id if res.session_reference else None
        self._collector.backfill_conversation_id(stage=self._stage, conversation_id=conv_id)

        if res.status != "SUCCEEDED":
            err_code = res.error.error_code if res.error else res.status
            raise EngineExecutionHaltedError(
                f"EngineExecutionHalted status={res.status} code={err_code}"
            )

        if self._track_usage_into_crewai and res.telemetry.telemetry_status == "VERIFIED_MEASURED":
            if res.telemetry.input_tokens is not None and res.telemetry.output_tokens is not None:
                self._track_token_usage_internal(
                    {
                        "prompt_tokens": res.telemetry.input_tokens,
                        "completion_tokens": res.telemetry.output_tokens,
                        "total_tokens": res.telemetry.total_tokens
                        or (res.telemetry.input_tokens + res.telemetry.output_tokens),
                        "cached_prompt_tokens": res.telemetry.cached_tokens or 0,
                        "reasoning_tokens": res.telemetry.thinking_tokens or 0,
                    }
                )
        return res.response_text or ""


class _ScopedCrewSpy:
    """Capture CrewAI Crew, Agent, Task, LLM, and Tool events scoped to correlation run."""

    def __init__(self, run_tag: str) -> None:
        self.run_tag = run_tag
        self.active = False
        self.records: list[dict[str, Any]] = []
        self._listener = _CrewBusListener(self)

    def record(self, event_name: str, event: Any) -> None:
        """Record events while this benchmark execution holds the active window."""
        if not self.active:
            return
        self.records.append(
            {
                "event_type": event_name,
                "run_tag": self.run_tag,
                "task_name": getattr(getattr(event, "task", None), "name", None),
                "tool_name": getattr(event, "tool_name", None),
            }
        )


class _CrewBusListener(BaseEventListener):
    """Event bus listener capturing Crew/Agent/Task/Tool lifecycle events."""

    def __init__(self, spy: _ScopedCrewSpy) -> None:
        self._spy = spy
        super().__init__()

    def setup_listeners(self, bus: Any) -> None:
        """Register handlers for CrewAI agentic loop events."""
        for cls in (
            CrewKickoffStartedEvent,
            CrewKickoffCompletedEvent,
            CrewKickoffFailedEvent,
            AgentExecutionStartedEvent,
            AgentExecutionCompletedEvent,
            AgentExecutionErrorEvent,
            TaskStartedEvent,
            TaskCompletedEvent,
            TaskFailedEvent,
            LLMCallStartedEvent,
            LLMCallCompletedEvent,
            LLMCallFailedEvent,
            ToolUsageStartedEvent,
            ToolUsageFinishedEvent,
            ToolUsageErrorEvent,
        ):
            name = cls.__name__

            @bus.on(cls)
            def _handler(_source: Any, event: Any, _ev_name: str = name) -> None:
                self._spy.record(_ev_name, event)


def _merge_usage_dicts(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum CrewAI ``UsageMetrics`` dicts across per-stage Crew kickoffs."""
    totals: dict[str, int] = {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "cached_prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cache_creation_tokens": 0,
        "successful_requests": 0,
    }
    for entry in usages:
        for key in totals:
            totals[key] += int(entry.get(key) or 0)
    return totals


def _build_stage_llm(
    *,
    registry: EngineRegistry,
    collector: _SpikeEventCollector,
    agent_spec: SpikeAgentSpec,
    stage: str,
    session_id: str,
    workspace_dir: Path,
    denied_dir: Path,
    capabilities_required: list[str],
    provider_id: str,
    connection_id: str | None,
    model_id: str | None,
    timeout_seconds: int,
    correlation_id: str,
    max_calls: int,
) -> AntigravityEngineBaseLLM:
    """Create a fresh ``AntigravityEngineBaseLLM`` bound to one isolated stage."""
    llm = AntigravityEngineBaseLLM(model=f"{provider_id}/{model_id or 'default'}")
    llm.bind_execution_context(
        registry=registry,
        collector=collector,
        agent_id=agent_spec.agent_id,
        role=agent_spec.system_role,
        stage=stage,
        session_id=session_id,
        workspace_dir=workspace_dir,
        denied_dir=denied_dir,
        execution_mode=agent_spec.execution_mode,
        capabilities_required=capabilities_required,
        deny_shell_commands=not agent_spec.allow_shell,
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        timeout_seconds=timeout_seconds,
        correlation_id=correlation_id,
        max_calls=max_calls,
    )
    return llm


def run_crewai_crew_benchmark(
    *,
    registry: EngineRegistry,
    user_request: str,
    master_workspace_dir: Path,
    worker_workspace_dir: Path,
    allowed_root: Path,
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
    max_calls_per_stage: int = 1,
) -> BenchmarkRunOutcome:
    """Execute Option C (CrewAI Crew + BaseLLM bridge) with explicit policy & QA gates."""
    t0 = time.perf_counter()
    correlation_id = f"corr-crew-{uuid.uuid4().hex[:10]}"
    m_session = (master_session_id or f"sess-master-{uuid.uuid4().hex[:10]}").strip()
    w_session = (worker_session_id or f"sess-worker-{uuid.uuid4().hex[:10]}").strip()
    effective_roster = roster if roster is not None else build_default_spike_roster()

    all_results: list[EngineResult] = []
    all_conv_ids: list[str] = []
    framework_usages: list[dict[str, Any]] = []
    prompt_chars_total = 0
    verification_duration_ms = 0
    qa_verification: EngineArtifactVerificationResult | None = None
    collector = _SpikeEventCollector(downstream=on_event)
    handoff_obj: WorkerHandoff | None = None
    worker_result: EngineResult | None = None

    def _finish(
        status: str,
        error: OrchestrationError | None,
        fw_events: list[dict[str, Any]],
        notes: dict[str, Any],
    ) -> BenchmarkRunOutcome:
        wall_ms = int((time.perf_counter() - t0) * 1000)
        timing = aggregate_timing_breakdown(
            total_duration_ms=wall_ms,
            verification_duration_ms=verification_duration_ms,
            engine_results=all_results,
            prompt_chars_total=prompt_chars_total,
            attributed_event_count=len(collector.items),
            framework_event_count=len(fw_events),
            expected_baseline_calls=3,
        )
        return BenchmarkRunOutcome(
            option="crewai_crew",
            status=status,  # type: ignore[arg-type]
            correlation_id=correlation_id,
            master_session_id=m_session,
            worker_session_id=w_session if handoff_obj is not None else None,
            provider_conversation_ids=all_conv_ids,
            observed_tool_names=(
                list(worker_result.observed_tool_names) if worker_result is not None else []
            ),
            qa_verification=qa_verification,
            engine_results=all_results,
            attributed_events=collector.items,
            framework_events=fw_events,
            framework_token_usage=_merge_usage_dicts(framework_usages) if framework_usages else None,
            persistence_db_path=None,
            flow_id=None,
            error=error,
            timing=timing,
            notes=notes,
        )

    preflight = _preflight_validate(
        master_workspace_dir=master_workspace_dir,
        worker_workspace_dir=worker_workspace_dir,
        allowed_root=allowed_root,
        allow_temp_workspace=allow_temp_workspace,
        roster=effective_roster,
        master_agent_id="core.master",
        permitted_read_relpaths=permitted_read_relpaths,
        permitted_write_relpaths=permitted_write_relpaths,
        expected_artifact_relpath=expected_artifact_relpath,
        master_session_id=m_session,
        worker_session_id=w_session,
    )
    if isinstance(preflight, OrchestrationError):
        return _finish(
            "BLOCKED"
            if preflight.error_code
            in {"INVALID_WORKSPACE_ISOLATION", "SESSION_ISOLATION_VIOLATION", "POLICY_VIOLATION"}
            else "FAILED",
            preflight,
            [],
            {"stage_reached": "preflight"},
        )

    master_ws, worker_ws, master_spec, clean_reads, clean_writes, clean_expected = preflight
    spy = _ScopedCrewSpy(correlation_id)

    with _isolated_crewai_env(crewai_storage_dir):
        spy.active = True
        try:
            # Stage 1: Master planning Crew
            pre_master_worker_tree = _snapshot_workspace_tree(worker_ws)
            plan_llm = _build_stage_llm(
                registry=registry,
                collector=collector,
                agent_spec=master_spec,
                stage="master_plan",
                session_id=m_session,
                workspace_dir=master_ws,
                denied_dir=worker_ws,
                capabilities_required=["text_generation", "streaming"],
                provider_id=provider_id,
                connection_id=connection_id,
                model_id=model_id,
                timeout_seconds=timeout_seconds,
                correlation_id=correlation_id,
                max_calls=max_calls_per_stage,
            )
            plan_agent = Agent(
                role="Orchestrator Master",
                goal="Decompose the user task into a strict JSON delegation plan without editing files.",
                backstory="You are core.master operating strictly in plan mode.",
                llm=plan_llm,
                allow_delegation=False,
                max_iter=1,
                max_retry_limit=0,
                verbose=False,
                use_system_prompt=False,
            )
            plan_prompt = _build_master_plan_prompt(
                user_request=user_request,
                master_spec=master_spec,
                permitted_read_relpaths=clean_reads,
                permitted_write_relpaths=clean_writes,
                expected_artifact_relpath=clean_expected,
            )
            plan_task = Task(
                name="master_plan_task",
                description=plan_prompt,
                expected_output="A single raw JSON object strictly matching the MasterPlan schema.",
                agent=plan_agent,
            )
            plan_crew = Crew(
                agents=[plan_agent],
                tasks=[plan_task],
                process=Process.sequential,
                memory=False,
                tracing=False,
                verbose=False,
            )
            try:
                plan_output = plan_crew.kickoff()
                all_results.extend(plan_llm.results)
                prompt_chars_total += plan_llm.prompt_chars
                framework_usages.append(plan_output.token_usage.model_dump())
                plan_text = plan_llm.raw_engine_responses[-1] if plan_llm.raw_engine_responses else plan_output.raw
            except Exception as exc:
                all_results.extend(plan_llm.results)
                prompt_chars_total += plan_llm.prompt_chars
                err_status = (
                    "BLOCKED"
                    if plan_llm.results and plan_llm.results[-1].status == "BLOCKED"
                    else "FAILED"
                )
                return _finish(
                    err_status,
                    OrchestrationError(
                        error_code="MASTER_PLAN_EXECUTION_FAILED",
                        stage="master_plan",
                        message=str(exc),
                    ),
                    spy.records,
                    {"crew_tool_events": 0, "stage_reached": "master_plan"},
                )

            master_plan_conv_id = (
                plan_llm.results[-1].session_reference.provider_conversation_id
                if plan_llm.results and plan_llm.results[-1].session_reference
                else None
            )
            if master_plan_conv_id:
                all_conv_ids.append(master_plan_conv_id)

            if _snapshot_workspace_tree(worker_ws) != pre_master_worker_tree:
                return _finish(
                    "BLOCKED",
                    OrchestrationError(
                        error_code="POLICY_VIOLATION",
                        stage="master_plan",
                        message="Master Crew stage mutated worker workspace directly; failing closed.",
                    ),
                    spy.records,
                    {"stage_reached": "master_plan"},
                )

            try:
                plan_dict = parse_strict_json_object(plan_text)
                master_plan = MasterPlan.model_validate(plan_dict)
            except (ValueError, ValidationError) as exc:
                return _finish(
                    "FAILED",
                    OrchestrationError(
                        error_code="MASTER_PLAN_PARSE_FAILED",
                        stage="master_plan",
                        message=f"Master Crew plan violated strict JSON schema: {exc}",
                    ),
                    spy.records,
                    {"stage_reached": "master_plan_parse"},
                )

            worker_spec, policy_err = validate_master_plan_policy(
                plan=master_plan,
                master_spec=master_spec,
                roster=effective_roster,
                worker_ws=worker_ws,
                permitted_reads=clean_reads,
                permitted_writes=clean_writes,
                expected_artifact_relpath=clean_expected,
            )
            if policy_err is not None or worker_spec is None:
                return _finish(
                    "BLOCKED",
                    policy_err,
                    spy.records,
                    {"stage_reached": "handoff_policy"},
                )

            handoff_obj = WorkerHandoff(
                correlation_id=correlation_id,
                from_agent_id=master_spec.agent_id,
                to_agent_id=worker_spec.agent_id,
                worker_session_id=w_session,
                worker_workspace_path=str(worker_ws),
                denied_paths=[str(master_ws)],
                deny_shell_commands=True,
                plan=master_plan,
                worker_instructions=master_plan.worker_objective,
            )

            # Stage 2: Worker Crew
            existing_protected = [
                rel
                for rel in sorted(set(clean_reads) | set(master_plan.allowed_read_paths))
                if (worker_ws / rel).is_file()
            ]
            protected_hashes = snapshot_file_hashes(worker_ws, existing_protected)
            pre_worker_master_tree = _snapshot_workspace_tree(master_ws)

            worker_llm = _build_stage_llm(
                registry=registry,
                collector=collector,
                agent_spec=worker_spec,
                stage="worker_execute",
                session_id=w_session,
                workspace_dir=worker_ws,
                denied_dir=master_ws,
                capabilities_required=["streaming", "native_tool_execution", "workspace_access"],
                provider_id=provider_id,
                connection_id=connection_id,
                model_id=model_id,
                timeout_seconds=timeout_seconds,
                correlation_id=correlation_id,
                max_calls=max_calls_per_stage,
            )
            worker_agent = Agent(
                role="IT Artifact Worker",
                goal="Execute the delegated file task inside the worker workspace using Antigravity native file tools.",
                backstory="You are it.artifact_worker restricted to explicit read/write paths without shell commands.",
                llm=worker_llm,
                allow_delegation=False,
                max_iter=1,
                max_retry_limit=0,
                verbose=False,
                use_system_prompt=False,
            )
            worker_task = Task(
                name="worker_execute_task",
                description=_build_worker_prompt(handoff_obj),
                expected_output=f"Verified artifact created at {clean_expected}.",
                agent=worker_agent,
            )
            worker_crew = Crew(
                agents=[worker_agent],
                tasks=[worker_task],
                process=Process.sequential,
                memory=False,
                tracing=False,
                verbose=False,
            )
            worker_exc: Exception | None = None
            try:
                worker_output = worker_crew.kickoff()
                framework_usages.append(worker_output.token_usage.model_dump())
            except Exception as exc:
                worker_exc = exc

            all_results.extend(worker_llm.results)
            prompt_chars_total += worker_llm.prompt_chars
            worker_result = worker_llm.results[-1] if worker_llm.results else None
            worker_conv_id = (
                worker_result.session_reference.provider_conversation_id
                if worker_result and worker_result.session_reference
                else None
            )
            if worker_conv_id:
                all_conv_ids.append(worker_conv_id)

            if _snapshot_workspace_tree(master_ws) != pre_worker_master_tree:
                return _finish(
                    "BLOCKED",
                    OrchestrationError(
                        error_code="SESSION_ISOLATION_VIOLATION",
                        stage="worker_execute",
                        message="Worker Crew stage mutated Master workspace directly; failing closed.",
                    ),
                    spy.records,
                    {"stage_reached": "worker_execute"},
                )

            returned_w_sid = (
                worker_result.session_reference.session_id
                if worker_result and worker_result.session_reference
                else None
            )
            if returned_w_sid == m_session or (
                master_plan_conv_id and worker_conv_id and master_plan_conv_id == worker_conv_id
            ):
                return _finish(
                    "BLOCKED",
                    OrchestrationError(
                        error_code="SESSION_ISOLATION_VIOLATION",
                        stage="worker_execute",
                        message="Provider returned colliding Master and Worker session/conversation IDs.",
                    ),
                    spy.records,
                    {"stage_reached": "worker_execute"},
                )

            if worker_result is not None:
                t_qa = time.perf_counter()
                qa_verification = verify_engine_artifact(
                    workspace_dir=worker_ws,
                    artifact_relpath=clean_expected,
                    engine_result=worker_result,
                    expected_content=expected_artifact_content,
                    required_substrings=tuple(required_artifact_substrings),
                    protected_source_hashes=protected_hashes,
                )
                verification_duration_ms += int((time.perf_counter() - t_qa) * 1000)

            if worker_exc is not None or worker_result is None or worker_result.status != "SUCCEEDED":
                msg = (
                    worker_result.error.message
                    if worker_result and worker_result.error
                    else str(worker_exc or "Worker Crew failed.")
                )
                return _finish(
                    "BLOCKED"
                    if worker_result and worker_result.status == "BLOCKED"
                    else "FAILED",
                    OrchestrationError(
                        error_code="WORKER_EXECUTION_FAILED",
                        stage="worker_execute",
                        message=msg,
                    ),
                    spy.records,
                    {"stage_reached": "worker_execute"},
                )

            # Stage 3: Master synthesis Crew
            synth_llm = _build_stage_llm(
                registry=registry,
                collector=collector,
                agent_spec=master_spec,
                stage="master_synthesize",
                session_id=m_session,
                workspace_dir=master_ws,
                denied_dir=worker_ws,
                capabilities_required=["text_generation", "streaming"],
                provider_id=provider_id,
                connection_id=connection_id,
                model_id=model_id,
                timeout_seconds=timeout_seconds,
                correlation_id=correlation_id,
                max_calls=max_calls_per_stage,
            )
            synth_agent = Agent(
                role="Orchestrator Master",
                goal="Report final delivery truthfully based strictly on independent QA verification.",
                backstory="You are core.master operating strictly in plan mode.",
                llm=synth_llm,
                allow_delegation=False,
                max_iter=1,
                max_retry_limit=0,
                verbose=False,
                use_system_prompt=False,
            )
            synth_task = Task(
                name="master_synthesis_task",
                description=_build_master_synthesis_prompt(
                    user_request=user_request,
                    master_spec=master_spec,
                    handoff=handoff_obj,
                    worker_result=worker_result,
                    qa_verification=qa_verification,
                ),
                expected_output="A single raw JSON object strictly matching the MasterSynthesis schema.",
                agent=synth_agent,
            )
            synth_crew = Crew(
                agents=[synth_agent],
                tasks=[synth_task],
                process=Process.sequential,
                memory=False,
                tracing=False,
                verbose=False,
            )
            try:
                synth_output = synth_crew.kickoff()
                framework_usages.append(synth_output.token_usage.model_dump())
                synth_text = (
                    synth_llm.raw_engine_responses[-1]
                    if synth_llm.raw_engine_responses
                    else synth_output.raw
                )
            except Exception as exc:
                all_results.extend(synth_llm.results)
                prompt_chars_total += synth_llm.prompt_chars
                return _finish(
                    "FAILED",
                    OrchestrationError(
                        error_code="MASTER_SYNTHESIS_EXECUTION_FAILED",
                        stage="master_synthesize",
                        message=str(exc),
                    ),
                    spy.records,
                    {"stage_reached": "master_synthesize"},
                )

            all_results.extend(synth_llm.results)
            prompt_chars_total += synth_llm.prompt_chars
            synth_conv_id = (
                synth_llm.results[-1].session_reference.provider_conversation_id
                if synth_llm.results and synth_llm.results[-1].session_reference
                else None
            )
            if synth_conv_id:
                all_conv_ids.append(synth_conv_id)

            if synth_conv_id and worker_conv_id and synth_conv_id == worker_conv_id:
                return _finish(
                    "BLOCKED",
                    OrchestrationError(
                        error_code="SESSION_ISOLATION_VIOLATION",
                        stage="master_synthesize",
                        message="Master synthesis provider conversation collided with Worker.",
                    ),
                    spy.records,
                    {"stage_reached": "master_synthesize"},
                )

            try:
                synth_dict = parse_strict_json_object(synth_text)
                synthesis = MasterSynthesis.model_validate(synth_dict)
            except (ValueError, ValidationError) as exc:
                return _finish(
                    "FAILED",
                    OrchestrationError(
                        error_code="MASTER_SYNTHESIS_PARSE_FAILED",
                        stage="master_synthesize",
                        message=f"Master Crew synthesis violated strict JSON schema: {exc}",
                    ),
                    spy.records,
                    {"stage_reached": "master_synthesize"},
                )

            truth_err = _validate_synthesis_truth(
                synthesis=synthesis,
                worker_result=worker_result,
                qa_verification=qa_verification,
            )
            if truth_err is not None:
                return _finish(
                    "FAILED",
                    truth_err,
                    spy.records,
                    {"stage_reached": "master_synthesize_truth"},
                )

            if qa_verification.status == "VERIFIED_FAIL":
                return _finish(
                    "REJECTED_BY_QA",
                    OrchestrationError(
                        error_code="QA_VERIFICATION_FAILED",
                        stage="qa_verify",
                        message="; ".join(qa_verification.failure_reasons)
                        or "Independent QA failed.",
                    ),
                    spy.records,
                    {"stage_reached": "qa_verify"},
                )

            crew_tool_events = sum(
                1 for ev in spy.records if str(ev.get("event_type", "")).startswith("ToolUsage")
            )
            return _finish(
                "DELIVERED",
                None,
                spy.records,
                {
                    "stage_reached": "delivered",
                    "crew_tool_events": crew_tool_events,
                    "cancel_api_available": False,
                    "prompt_wrapped_by_crewai": True,
                },
            )
        finally:
            spy.active = False
