"""Master Planning service: intake clarification, DAG validation, routing Governance, and revisions."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    SnakeId,
    ensure_not_secret,
    new_event_id,
    validate_agent_id,
    validate_aware_datetime,
    validate_capability_id,
    validate_safe_payload,
    validate_tool_id_syntax,
    validate_unique_id_list,
)
from core.contracts.references import ChecklistItem
from core.governance.manager import PermissionDeniedError, PermissionManager
from core.governance.models import (
    PolicyDecision,
    PolicyRequest,
    RoutingContext,
    TrustedAuthContext,
    actor_kind_to_event_source as _actor_kind_to_event_source,
)
from core.governance.workspace import validate_workspace_path
from core.orchestration.planning_models import (
    MasterWorkPlan,
    PlanApprovalRequiredError,
    PlanCapabilityAssessment,
    PlanGovernanceDeniedError,
    PlanNotFoundError,
    PlanRepository,
    PlanRevisionRecord,
    PlanRoutingProposal,
    PlanStepSpec,
    PlanningClarification,
    PlanningOutcome,
    PlanValidationError,
    StalePlanVersionError,
    StepId,
    StoredMasterPlan,
    StructuredUserBrief,
    new_plan_id,
)
from core.orchestration.spike_models import parse_strict_json_object
from engines.contract import EngineRequest
from engines.registry import EngineRegistry

if TYPE_CHECKING:
    from core.agents.registry import AgentRegistryRecord, AgentRegistryService

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_ONLY_PROVIDERS: frozenset[str] = frozenset({"ollama", "local_llm", "llama_cpp"})
_VAGUE_OBJECTIVES: frozenset[str] = frozenset(
    {"tbd", "todo", "help", "fix", "work", "do it", "chua ro", "chưa rõ", "n/a", "none"}
)


def _resolve_plan_actor_id(auth_context: TrustedAuthContext) -> str:
    """Resolve a canonical AgentId from a verified authorization context."""
    try:
        return validate_agent_id(auth_context.actor_id)
    except ValueError:
        if auth_context.actor_kind == "human_operator":
            return "operator.local"
        return "core.master"


class MasterEngineStepPayload(BaseModel):
    """Strict JSON schema for a single step proposed by an Engine during planning."""

    model_config = ConfigDict(extra="forbid")

    step_id: StepId
    order: int = Field(ge=0, le=10000)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    depends_on: list[StepId] = Field(default_factory=list, max_length=50)
    required_capabilities: list[CapabilityId] = Field(min_length=1, max_length=16)
    input_formats: list[SnakeId] = Field(default_factory=list, max_length=16)
    output_formats: list[SnakeId] = Field(default_factory=list, max_length=16)
    expected_inputs: list[str] = Field(default_factory=list, max_length=32)
    expected_outputs: list[str] = Field(min_length=1, max_length=32)
    allowed_read_paths: list[str] = Field(default_factory=list, max_length=32)
    allowed_write_paths: list[str] = Field(default_factory=list, max_length=32)
    required_tools: list[str] = Field(default_factory=list, max_length=32)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    proposed_agent_ids: list[AgentId] = Field(default_factory=list, max_length=10)
    requires_human_approval: bool = False

    def to_plan_step_spec(self) -> PlanStepSpec:
        """Convert validated Engine step JSON into canonical ``PlanStepSpec``."""
        return PlanStepSpec.model_validate(self.model_dump(mode="python"))


class MasterEnginePlanPayload(BaseModel):
    """Strict JSON schema required from an Engine adapter for Master Planning."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["VALIDATED", "NEEDS_CLARIFICATION"] = "VALIDATED"
    goal_summary: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=32)
    missing_information: list[PlanningClarification] = Field(default_factory=list, max_length=32)
    steps: list[MasterEngineStepPayload] = Field(default_factory=list, max_length=200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    routing_proposal: PlanRoutingProposal

    @field_validator("goal_summary")
    @classmethod
    def _validate_goal(cls, value: str) -> str:
        """Ensure Engine goal summary is non-blank and secret-free."""
        return ensure_not_secret(value, "goal_summary")

    @field_validator("assumptions", "acceptance_criteria")
    @classmethod
    def _validate_lists(cls, values: list[str], info: object) -> list[str]:
        """Reject secret patterns or blank entries in Engine plan lists."""
        field_name = getattr(info, "field_name", "engine_plan_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @model_validator(mode="after")
    def _validate_payload_completeness(self) -> MasterEnginePlanPayload:
        """Reject empty steps or missing criteria when Engine claims plan is VALIDATED."""
        if self.status == "VALIDATED":
            if not self.steps:
                raise ValueError("Engine VALIDATED plan must include at least one step.")
            if not self.acceptance_criteria:
                raise ValueError("Engine VALIDATED plan must include acceptance_criteria.")
        return self


def parse_master_plan_json(raw_text: str | None) -> MasterEnginePlanPayload:
    """Parse strict JSON object returned by an Engine for Master Planning fail-closed."""
    parsed = parse_strict_json_object(raw_text)
    validate_safe_payload(parsed, "master_engine_plan")
    try:
        return MasterEnginePlanPayload.model_validate(parsed)
    except ValidationError as exc:
        raise PlanValidationError("INVALID_ENGINE_PLAN_SCHEMA", f"Engine plan failed schema validation: {exc}") from exc


def build_master_planning_prompt(
    brief: StructuredUserBrief,
    active_workers: Sequence[AgentRegistryRecord],
) -> str:
    """Build a strict JSON-only prompt summarizing operator brief and active Worker capabilities."""
    safe_workers = [
        {
            "agent_id": rec.agent_id,
            "department": rec.manifest.department,
            "role": rec.manifest.role,
            "capabilities": rec.manifest.capabilities.skills,
            "inputs": rec.manifest.capabilities.inputs,
            "outputs": rec.manifest.capabilities.outputs,
            "default_engine": rec.manifest.engine.default_type,
            "provider_id": rec.manifest.engine.provider_id,
        }
        for rec in active_workers
        if rec.manifest.system_role == "worker" and rec.is_eligible_for_assignment()
    ]
    schema_hint = {
        "status": "VALIDATED",
        "goal_summary": "concise summary of objective",
        "assumptions": ["explicit assumption"],
        "missing_information": [],
        "steps": [
            {
                "step_id": "step_01_execute",
                "order": 0,
                "title": "Step title",
                "description": "Concrete bounded step instructions",
                "depends_on": [],
                "required_capabilities": ["workspace_artifact_authoring"],
                "input_formats": ["text"],
                "output_formats": ["workspace_file_artifact"],
                "expected_inputs": ["workspace/task/input.txt"],
                "expected_outputs": ["workspace/task/output.txt"],
                "allowed_read_paths": ["workspace/task/input.txt"],
                "allowed_write_paths": ["workspace/task/output.txt"],
                "required_tools": [],
                "acceptance_criteria": ["Output file matches expected format"],
                "proposed_agent_ids": [],
                "requires_human_approval": False,
            }
        ],
        "acceptance_criteria": ["Independent QA verifies deliverable artifact"],
        "routing_proposal": {
            "engine_id": brief.preferred_engine_id or "antigravity",
            "provider_id": brief.preferred_provider_id or "antigravity",
            "connection_id": brief.preferred_connection_id,
            "model_id": brief.preferred_model_id,
            "data_residency_policy": brief.data_residency_policy,
            "routing_reason": "Selected active approved routing profile",
        },
    }
    return (
        "You are core.master (system_role=orchestrator, department=orchestration).\n"
        "Decompose the task brief below into a single strict JSON object.\n"
        "Rules:\n"
        "- Do NOT start execution, invoke shell tools, or modify files.\n"
        "- Do NOT mark any step or task COMPLETED or fabricate runtime/QA evidence.\n"
        "- Use ONLY workspace-relative read/write paths without '..' or protected system directories.\n"
        "- Output ONLY valid JSON matching this schema with no markdown prose:\n"
        f"{json.dumps(schema_hint, indent=2)}\n\n"
        f"Active Eligible Worker Directory:\n{json.dumps(safe_workers, indent=2)}\n\n"
        f"Structured Task Brief:\n{brief.model_dump_json(indent=2)}"
    )


def analyze_brief_completeness(
    brief: StructuredUserBrief,
    *,
    has_explicit_steps: bool = False,
    require_explicit_outputs: bool = True,
) -> list[PlanningClarification]:
    """Identify missing required intake fields without guessing unstated goals."""
    clarifications: list[PlanningClarification] = []
    words = [token for token in brief.objective.strip().split() if token]
    lowered_obj = brief.objective.strip().lower()
    if len(words) < 3 or lowered_obj in _VAGUE_OBJECTIVES:
        clarifications.append(
            PlanningClarification(
                missing_field="objective",
                question="Please specify the concrete deliverable goal, scope, and constraints.",
                reason="Brief objective is too short or underspecified to decompose safely.",
                blocking=True,
            )
        )

    metadata_missing = brief.metadata.get("missing_required_inputs")
    if isinstance(metadata_missing, list):
        for raw_missing in metadata_missing:
            field_label = str(raw_missing).strip()
            if field_label:
                clarifications.append(
                    PlanningClarification(
                        missing_field=field_label[:64],
                        question=f"Please provide required input specification for '{field_label}'.",
                        reason="Upstream brief flagged missing required input dependency.",
                        blocking=True,
                    )
                )

    if require_explicit_outputs and not has_explicit_steps:
        if not brief.required_outputs:
            clarifications.append(
                PlanningClarification(
                    missing_field="required_outputs",
                    question="What concrete outputs or deliverable paths must this task produce?",
                    reason="Cannot create verifiable steps or ChecklistItems without expected outputs.",
                    blocking=True,
                )
            )
        if not brief.acceptance_criteria:
            clarifications.append(
                PlanningClarification(
                    missing_field="acceptance_criteria",
                    question="What explicit acceptance criteria should QA use to verify completion?",
                    reason="Plan cannot omit measurable acceptance criteria.",
                    blocking=True,
                )
            )

    return clarifications


class MasterPlanningService:
    """Governance-enforced Master planning coordinator backed by SQLite PlanRepository."""

    def __init__(
        self,
        *,
        plan_repo: PlanRepository,
        agent_registry: AgentRegistryService,
        permission_manager: PermissionManager,
        workspace_root: Path | None = None,
        engine_registry: EngineRegistry | None = None,
        allowed_providers: Collection[str] | None = None,
        allowed_models: Collection[str] | None = None,
        allowed_connections: Collection[str] | None = None,
        registered_tools: Collection[str] | None = None,
        local_only_providers: Collection[str] | None = None,
    ) -> None:
        """Initialize planning service with explicit Registry, Governance, and Storage ports."""
        self._repo = plan_repo
        self._registry = agent_registry
        self._pm = permission_manager
        self._workspace_root = workspace_root
        self._engine_registry = engine_registry
        self._allowed_providers = set(allowed_providers) if allowed_providers is not None else None
        self._allowed_models = set(allowed_models) if allowed_models is not None else None
        self._allowed_connections = set(allowed_connections) if allowed_connections is not None else None
        self._registered_tools = set(registered_tools) if registered_tools is not None else None
        self._local_providers = (
            set(local_only_providers)
            if local_only_providers is not None
            else set(_DEFAULT_LOCAL_ONLY_PROVIDERS)
        )

    def _active_workers(self) -> list[AgentRegistryRecord]:
        """Return ACTIVE non-orchestrator worker records from Registry without fabricating any."""
        from core.agents.registry import AgentSearchQuery

        records = self._registry.search_agents(AgentSearchQuery(eligible_only=True))
        return [
            rec
            for rec in records
            if rec.manifest.system_role == "worker" and rec.agent_id != "core.master"
        ]

    def _assess_capabilities(
        self,
        steps: Sequence[PlanStepSpec],
        active_workers: Sequence[AgentRegistryRecord],
    ) -> tuple[list[PlanCapabilityAssessment], list[str]]:
        """Cross-check step capabilities against real ACTIVE Registry workers."""
        requested: list[str] = []
        seen: set[str] = set()
        for step in steps:
            for cap in step.required_capabilities:
                if cap not in seen:
                    seen.add(cap)
                    requested.append(cap)

        assessments: list[PlanCapabilityAssessment] = []
        missing: list[str] = []
        for cap_id in requested:
            observed_agents: list[str] = []
            validated_agents: list[str] = []
            declared_agents: list[str] = []
            for rec in active_workers:
                entry = next(
                    (item for item in rec.capability_verification if item.capability_id == cap_id),
                    None,
                )
                if entry is None:
                    continue
                if entry.observed_in_execution:
                    observed_agents.append(rec.agent_id)
                elif entry.validated:
                    validated_agents.append(rec.agent_id)
                elif entry.declared:
                    declared_agents.append(rec.agent_id)

            all_matching = sorted(set(observed_agents + validated_agents + declared_agents))
            if observed_agents:
                status: Literal[
                    "COVERED_DECLARED",
                    "COVERED_VALIDATED",
                    "COVERED_OBSERVED",
                    "MISSING_CAPABILITY",
                ] = "COVERED_OBSERVED"
                note = f"Observed execution evidence available for {len(observed_agents)} active worker(s)."
            elif validated_agents:
                status = "COVERED_VALIDATED"
                note = f"Validated static capability across {len(validated_agents)} active worker(s)."
            elif declared_agents:
                status = "COVERED_DECLARED"
                note = f"Declared static capability across {len(declared_agents)} active worker(s)."
            else:
                status = "MISSING_CAPABILITY"
                note = f"No ACTIVE Registry worker declares capability '{cap_id}'."
                missing.append(cap_id)

            assessments.append(
                PlanCapabilityAssessment(
                    capability_id=cap_id,
                    matching_active_agent_ids=all_matching,
                    status=status,
                    notes=note,
                )
            )
        return assessments, missing

    def _validate_steps_runtime_policies(
        self,
        steps: Sequence[PlanStepSpec],
        active_workers: Sequence[AgentRegistryRecord],
    ) -> None:
        """Enforce workspace containment, tool registration, and proposed agent eligibility."""
        active_worker_map = {rec.agent_id: rec for rec in active_workers}
        for step in steps:
            if self._workspace_root is not None:
                for rel in step.allowed_read_paths:
                    check = validate_workspace_path(self._workspace_root, rel, action="files.read")
                    if not check.allowed:
                        raise PlanValidationError(check.reason_code, check.message)
                for rel in step.allowed_write_paths:
                    check = validate_workspace_path(self._workspace_root, rel, action="files.write")
                    if not check.allowed:
                        raise PlanValidationError(check.reason_code, check.message)

            if self._registered_tools is not None:
                for tool_id in step.required_tools:
                    validate_tool_id_syntax(tool_id)
                    if tool_id not in self._registered_tools:
                        raise PlanValidationError(
                            "UNKNOWN_TOOL_ID",
                            f"Step '{step.step_id}' requests unregistered tool '{tool_id}'.",
                        )

            for proposed_id in step.proposed_agent_ids:
                if proposed_id not in active_worker_map:
                    raise PlanValidationError(
                        "INVALID_PROPOSED_AGENT",
                        (
                            f"Step '{step.step_id}' proposes agent '{proposed_id}', which is not an ACTIVE "
                            "worker in Agent Registry."
                        ),
                    )

    def _build_routing_context(self, routing: PlanRoutingProposal) -> RoutingContext:
        """Translate ``PlanRoutingProposal`` to Governance ``RoutingContext``."""
        effective_provider = routing.provider_id or routing.engine_id
        data_residency: Literal["local_only", "cloud"] = (
            "local_only" if routing.data_residency_policy == "local_only" else "cloud"
        )
        target_residency: Literal["local_only", "cloud"] = (
            "local_only" if effective_provider in self._local_providers else "cloud"
        )
        return RoutingContext(
            provider_id=effective_provider,
            connection_id=routing.connection_id,
            model_id=routing.model_id,
            data_residency=data_residency,
            target_residency=target_residency,
            allowed_providers=sorted(self._allowed_providers) if self._allowed_providers else [],
            allowed_connections=sorted(self._allowed_connections) if self._allowed_connections else [],
        )

    def _enforce_planning_governance(
        self,
        *,
        auth_context: TrustedAuthContext | None,
        task_id: str,
        resource: str,
        timestamp: datetime,
        routing_proposal: PlanRoutingProposal | None,
        parameters: dict[str, Any],
        approval_id: str | None,
        correlation_id: str,
    ) -> PolicyDecision:
        """Authorize caller and routing against Governance before mutating plan state."""
        routing_ctx = (
            self._build_routing_context(routing_proposal)
            if routing_proposal is not None
            else None
        )
        req = PolicyRequest(
            auth_context=auth_context,
            action="task.update_plan",
            resource=resource,
            task_id=task_id,
            routing=routing_ctx,
            approval_id=approval_id,
            parameters=parameters,
            timestamp=timestamp,
            correlation_id=correlation_id,
        )
        try:
            decision, _ = self._pm.dispatch_protected_action(req, lambda: True)
        except PermissionDeniedError as exc:
            if exc.decision.decision == "APPROVAL_REQUIRED":
                raise PlanApprovalRequiredError(
                    exc.decision.reason_code,
                    exc.decision.message,
                ) from exc
            raise PlanGovernanceDeniedError(
                exc.decision.reason_code,
                exc.decision.message,
            ) from exc

        if routing_proposal is not None:
            if (
                self._allowed_models is not None
                and routing_proposal.model_id is not None
                and routing_proposal.model_id not in self._allowed_models
            ):
                raise PlanGovernanceDeniedError(
                    "MODEL_NOT_ALLOWED",
                    (
                        f"Proposed model '{routing_proposal.model_id}' is outside configured "
                        "allowed_models policy."
                    ),
                )
            if routing_proposal.data_residency_policy == "strict_approved_only":
                effective_provider = routing_proposal.provider_id or routing_proposal.engine_id
                if (
                    routing_proposal.connection_id is None
                    or (
                        self._allowed_providers is not None
                        and effective_provider not in self._allowed_providers
                    )
                ):
                    raise PlanGovernanceDeniedError(
                        "STRICT_ROUTING_NOT_APPROVED",
                        (
                            "strict_approved_only routing requires both an approved provider_id "
                            "and explicit connection_id."
                        ),
                    )
        return decision

    def _build_deterministic_steps_from_brief(
        self,
        brief: StructuredUserBrief,
    ) -> list[PlanStepSpec]:
        """Generate ordered deterministic PlanStepSpec items from an explicit brief."""
        raw_caps = brief.metadata.get("required_capabilities")
        default_caps: list[str]
        if isinstance(raw_caps, list) and raw_caps:
            default_caps = validate_unique_id_list(
                [str(item) for item in raw_caps],
                validate_capability_id,
                "brief.metadata.required_capabilities",
            )
        else:
            default_caps = ["workspace_artifact_authoring"]

        raw_tools = brief.metadata.get("required_tools")
        step_tools: list[str] = []
        if isinstance(raw_tools, list):
            step_tools = validate_unique_id_list(
                [str(item) for item in raw_tools],
                validate_tool_id_syntax,
                "brief.metadata.required_tools",
            )

        output_targets = list(brief.required_outputs)
        criteria = list(brief.acceptance_criteria)
        write_paths: list[str] = []
        for out_item in output_targets:
            candidate = out_item.strip()
            if "/" in candidate and not candidate.startswith(("/", "\\")):
                write_paths.append(candidate)

        steps: list[PlanStepSpec] = []
        for idx, out_item in enumerate(output_targets):
            step_id = f"step_{idx + 1:02d}_deliver"
            depends = [f"step_{idx:02d}_deliver"] if idx > 0 else []
            step_write = [out_item] if out_item in write_paths else []
            step_criteria = [criteria[idx]] if idx < len(criteria) else [criteria[0]]
            steps.append(
                PlanStepSpec(
                    step_id=step_id,
                    order=idx,
                    title=f"Produce {out_item[:120]}",
                    description=f"{brief.objective} (Deliverable target: {out_item})",
                    depends_on=depends,
                    required_capabilities=default_caps,
                    expected_inputs=list(brief.input_references),
                    expected_outputs=[out_item],
                    allowed_read_paths=list(brief.input_references),
                    allowed_write_paths=step_write,
                    required_tools=step_tools if idx == 0 else [],
                    acceptance_criteria=step_criteria,
                )
            )
        return steps

    def create_plan(
        self,
        *,
        brief: StructuredUserBrief,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        steps: Sequence[PlanStepSpec] | None = None,
        routing_proposal: PlanRoutingProposal | None = None,
        assumptions: Sequence[str] = (),
        require_explicit_outputs: bool = True,
        planning_source: Literal[
            "deterministic_planner",
            "engine_assisted",
            "operator_supplied",
        ] = "deterministic_planner",
        engine_provider_id: str | None = None,
        engine_model_id: str | None = None,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> PlanningOutcome:
        """Analyze brief, enforce Governance/DAG/capability checks, and persist MasterWorkPlan."""
        validate_aware_datetime(timestamp, "timestamp")
        resolved_routing = routing_proposal or PlanRoutingProposal(
            engine_id=brief.preferred_engine_id or "antigravity",
            provider_id=brief.preferred_provider_id or brief.preferred_engine_id or "antigravity",
            connection_id=brief.preferred_connection_id,
            model_id=brief.preferred_model_id,
            data_residency_policy=brief.data_residency_policy,
            routing_reason="Derived from structured task brief preferences",
        )
        plan_id = new_plan_id()
        corr_id = correlation_id or f"corr-plan-create-{brief.task_id}"

        self._enforce_planning_governance(
            auth_context=auth_context,
            task_id=brief.task_id,
            resource=f"task:{brief.task_id}",
            timestamp=timestamp,
            routing_proposal=resolved_routing,
            parameters={
                "plan_id": plan_id,
                "task_id": brief.task_id,
                "operation": "create_plan",
                "planning_source": planning_source,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )
        assert auth_context is not None
        creator_id = _resolve_plan_actor_id(auth_context)

        clarifications = analyze_brief_completeness(
            brief,
            has_explicit_steps=steps is not None and len(steps) > 0,
            require_explicit_outputs=require_explicit_outputs,
        )
        if clarifications:
            clarification_plan = MasterWorkPlan(
                plan_id=plan_id,
                task_id=brief.task_id,
                plan_revision=1,
                status="NEEDS_CLARIFICATION",
                goal_summary=brief.objective,
                assumptions=list(assumptions),
                missing_information=clarifications,
                steps=[],
                acceptance_criteria=list(brief.acceptance_criteria),
                routing_proposal=resolved_routing,
                capability_assessments=[],
                missing_capabilities=[],
                requires_human_approval=False,
                created_by=creator_id,
                created_at=timestamp,
                change_summary="Brief requires operator clarification prior to decomposition",
                planning_source=planning_source,
                engine_provider_id=engine_provider_id,
                engine_model_id=engine_model_id,
            )
            return self._persist_new_plan(
                plan=clarification_plan,
                creator_id=creator_id,
                timestamp=timestamp,
                correlation_id=corr_id,
                event_source=_actor_kind_to_event_source(auth_context.actor_kind),
                outcome_status="NEEDS_CLARIFICATION",
                clarifications=clarifications,
            )

        resolved_steps = (
            list(steps) if steps is not None else self._build_deterministic_steps_from_brief(brief)
        )
        active_workers = self._active_workers()
        self._validate_steps_runtime_policies(resolved_steps, active_workers)
        assessments, missing_caps = self._assess_capabilities(resolved_steps, active_workers)

        plan_criteria = list(brief.acceptance_criteria)
        if not plan_criteria:
            seen_crit: set[str] = set()
            for step in resolved_steps:
                for crit in step.acceptance_criteria:
                    if crit not in seen_crit:
                        seen_crit.add(crit)
                        plan_criteria.append(crit)

        requires_approval = any(step.requires_human_approval for step in resolved_steps)
        try:
            validated_plan = MasterWorkPlan(
                plan_id=plan_id,
                task_id=brief.task_id,
                plan_revision=1,
                status="VALIDATED",
                goal_summary=f"{brief.title}: {brief.objective}",
                assumptions=list(assumptions),
                missing_information=[],
                steps=resolved_steps,
                acceptance_criteria=plan_criteria,
                routing_proposal=resolved_routing,
                capability_assessments=assessments,
                missing_capabilities=missing_caps,
                requires_human_approval=requires_approval,
                created_by=creator_id,
                created_at=timestamp,
                change_summary="Initial structured Master work plan",
                planning_source=planning_source,
                engine_provider_id=engine_provider_id,
                engine_model_id=engine_model_id,
            )
        except (ValidationError, ValueError) as exc:
            raise PlanValidationError("INVALID_PLAN_STRUCTURE", str(exc)) from exc

        return self._persist_new_plan(
            plan=validated_plan,
            creator_id=creator_id,
            timestamp=timestamp,
            correlation_id=corr_id,
            event_source=_actor_kind_to_event_source(auth_context.actor_kind),
            outcome_status="PLAN_READY",
            clarifications=[],
        )

    def _persist_new_plan(
        self,
        *,
        plan: MasterWorkPlan,
        creator_id: str,
        timestamp: datetime,
        correlation_id: str,
        event_source: EventSource,
        outcome_status: Literal["PLAN_READY", "NEEDS_CLARIFICATION"],
        clarifications: list[PlanningClarification],
    ) -> PlanningOutcome:
        """Persist new plan, revision v1, and ``plan.created`` event atomically."""
        plan_sha = plan.compute_sha256()
        audit_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="plan.created",
            source=event_source,
            task_id=plan.task_id,
            actor_id=creator_id,
            provider_id=plan.engine_provider_id or plan.routing_proposal.provider_id,
            connection_id=plan.routing_proposal.connection_id,
            model_id=plan.engine_model_id or plan.routing_proposal.model_id,
            correlation_id=correlation_id,
            redacted_payload={
                "plan_id": plan.plan_id,
                "plan_revision": plan.plan_revision,
                "status": plan.status,
                "step_count": len(plan.steps),
                "missing_capabilities": plan.missing_capabilities,
                "clarification_count": len(clarifications),
                "plan_sha256": plan_sha,
            },
        )
        stored = StoredMasterPlan(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            active_revision=plan.plan_revision,
            record_version=1,
            status=plan.status,
            plan=plan,
            plan_sha256=plan_sha,
            created_by=creator_id,
            created_at=timestamp,
            updated_by=creator_id,
            updated_at=timestamp,
            last_audit_event_id=audit_ev.event_id,
        )
        revision = PlanRevisionRecord(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            plan_revision=plan.plan_revision,
            status=plan.status,
            plan=plan,
            plan_sha256=plan_sha,
            change_summary=plan.change_summary,
            changed_by=creator_id,
            changed_at=timestamp,
            audit_event_id=audit_ev.event_id,
        )
        saved = self._repo.create_plan(stored, revision, audit_ev)
        checklist_preview = (
            plan.to_checklist_items(created_at=timestamp) if plan.steps else []
        )
        return PlanningOutcome(
            outcome_status=outcome_status,
            task_id=plan.task_id,
            plan=plan,
            stored_plan=saved,
            clarifications=clarifications,
            checklist_preview=checklist_preview,
            missing_capabilities=list(plan.missing_capabilities),
            audit_event_id=audit_ev.event_id,
        )

    def create_plan_with_engine(
        self,
        *,
        brief: StructuredUserBrief,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        adapter_id: str | None = None,
        max_json_retries: int = 1,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> PlanningOutcome:
        """Invoke EngineRegistry adapter for Master plan generation, failing closed on invalid JSON."""
        validate_aware_datetime(timestamp, "timestamp")
        if self._engine_registry is None:
            raise PlanningError(
                "ENGINE_REGISTRY_UNAVAILABLE",
                "EngineRegistry is required for engine-assisted Master planning.",
            )

        corr_id = correlation_id or f"corr-plan-engine-{brief.task_id}"
        self._enforce_planning_governance(
            auth_context=auth_context,
            task_id=brief.task_id,
            resource=f"task:{brief.task_id}",
            timestamp=timestamp,
            routing_proposal=None,
            parameters={
                "task_id": brief.task_id,
                "operation": "create_plan_with_engine",
                "adapter_id": adapter_id,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )

        clarifications = analyze_brief_completeness(brief, has_explicit_steps=False)
        if clarifications:
            return self.create_plan(
                brief=brief,
                auth_context=auth_context,
                timestamp=timestamp,
                planning_source="engine_assisted",
                approval_id=approval_id,
                correlation_id=corr_id,
            )

        active_workers = self._active_workers()
        prompt = build_master_planning_prompt(brief, active_workers)

        parsed_payload: MasterEnginePlanPayload | None = None
        last_error: str | None = None
        last_provider_id: str | None = None
        last_model_id: str | None = None

        attempts = max(1, max_json_retries + 1)
        for attempt_idx in range(attempts):
            attempt_prompt = prompt
            if attempt_idx > 0:
                attempt_prompt = (
                    f"{prompt}\n\n"
                    f"Previous attempt failed strict JSON parsing ({last_error}). "
                    "Return strictly valid raw JSON matching the schema with zero prose."
                )
            default_provider = (
                brief.preferred_provider_id
                or brief.preferred_engine_id
                or ("antigravity" if adapter_id is None and brief.preferred_connection_id is None else None)
            )
            req = EngineRequest(
                provider_id=default_provider,
                adapter_id=adapter_id,
                connection_id=brief.preferred_connection_id,
                model_id=brief.preferred_model_id,
                prompt=attempt_prompt,
                capabilities_required=["text_generation"],
                execution_mode="text",
                data_locality=(
                    "local_only" if brief.data_residency_policy == "local_only" else "default"
                ),
                deny_shell_commands=True,
                metadata={"task_id": brief.task_id, "stage": "master_planning"},
            )
            adapter, conn, resolve_err = self._engine_registry.resolve(req)
            if resolve_err is not None or adapter is None:
                msg = (
                    resolve_err.message
                    if resolve_err is not None
                    else "Unable to resolve Engine adapter for Master planning."
                )
                return PlanningOutcome(
                    outcome_status="BLOCKED_GOVERNANCE",
                    task_id=brief.task_id,
                    error_detail=msg,
                )

            result = adapter.execute(req, connection=conn)
            last_provider_id = result.provider_id
            last_model_id = result.model_id
            if result.status != "SUCCEEDED":
                last_error = (
                    result.error.message
                    if result.error is not None
                    else f"Engine execution finished with status={result.status}."
                )
                return PlanningOutcome(
                    outcome_status="FAILED_VALIDATION",
                    task_id=brief.task_id,
                    error_detail=last_error,
                )

            try:
                parsed_payload = parse_master_plan_json(result.response_text)
                break
            except (PlanValidationError, ValidationError, ValueError) as exc:
                last_error = str(exc)
                logger.warning(
                    "Master planning JSON parse attempt %d/%d failed for task '%s': %s",
                    attempt_idx + 1,
                    attempts,
                    brief.task_id,
                    last_error,
                )

        if parsed_payload is None:
            return PlanningOutcome(
                outcome_status="FAILED_VALIDATION",
                task_id=brief.task_id,
                error_detail=last_error or "Engine failed to return a valid JSON Master work plan.",
            )

        if parsed_payload.status == "NEEDS_CLARIFICATION":
            missing_list = parsed_payload.missing_information or [
                PlanningClarification(
                    missing_field="objective",
                    question="Additional specification is required to complete decomposition.",
                    reason="Engine planner reported insufficient information.",
                    blocking=True,
                )
            ]
            brief_with_clarification = brief.model_copy(
                update={
                    "required_outputs": [],
                    "metadata": {
                        **brief.metadata,
                        "missing_required_inputs": [item.missing_field for item in missing_list],
                    },
                }
            )
            return self.create_plan(
                brief=brief_with_clarification,
                auth_context=auth_context,
                timestamp=timestamp,
                planning_source="engine_assisted",
                engine_provider_id=last_provider_id,
                engine_model_id=last_model_id,
                approval_id=approval_id,
                correlation_id=corr_id,
            )

        try:
            converted_steps = [step.to_plan_step_spec() for step in parsed_payload.steps]
            enriched_brief = brief.model_copy(
                update={"acceptance_criteria": parsed_payload.acceptance_criteria}
            )
            return self.create_plan(
                brief=enriched_brief,
                auth_context=auth_context,
                timestamp=timestamp,
                steps=converted_steps,
                routing_proposal=parsed_payload.routing_proposal,
                assumptions=parsed_payload.assumptions,
                require_explicit_outputs=False,
                planning_source="engine_assisted",
                engine_provider_id=last_provider_id,
                engine_model_id=last_model_id,
                approval_id=approval_id,
                correlation_id=corr_id,
            )
        except (PlanValidationError, ValidationError, ValueError) as exc:
            return PlanningOutcome(
                outcome_status="FAILED_VALIDATION",
                task_id=brief.task_id,
                error_detail=str(exc),
            )
        except (PlanGovernanceDeniedError, PlanApprovalRequiredError) as exc:
            return PlanningOutcome(
                outcome_status="BLOCKED_GOVERNANCE",
                task_id=brief.task_id,
                governance_reason=exc.message,
                error_detail=exc.message,
            )

    def revise_plan(
        self,
        plan_id: str,
        *,
        expected_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        change_summary: str,
        steps: Sequence[PlanStepSpec] | None = None,
        goal_summary: str | None = None,
        assumptions: Sequence[str] | None = None,
        acceptance_criteria: Sequence[str] | None = None,
        routing_proposal: PlanRoutingProposal | None = None,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> PlanningOutcome:
        """Create a new immutable plan revision under Governance and optimistic concurrency."""
        validate_aware_datetime(timestamp, "timestamp")
        cleaned_summary = ensure_not_secret(change_summary, "change_summary")

        current = self._repo.get_plan(plan_id)
        if current is None:
            raise PlanNotFoundError("PLAN_NOT_FOUND", f"Plan '{plan_id}' does not exist.")

        next_routing = routing_proposal or current.plan.routing_proposal
        corr_id = correlation_id or f"corr-plan-rev-{plan_id}"
        self._enforce_planning_governance(
            auth_context=auth_context,
            task_id=current.task_id,
            resource=f"plan:{plan_id}",
            timestamp=timestamp,
            routing_proposal=next_routing,
            parameters={
                "plan_id": plan_id,
                "task_id": current.task_id,
                "expected_record_version": expected_record_version,
                "change_summary": cleaned_summary,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )
        assert auth_context is not None
        updater_id = _resolve_plan_actor_id(auth_context)

        next_steps = list(steps) if steps is not None else list(current.plan.steps)
        if not next_steps:
            raise PlanValidationError(
                "EMPTY_PLAN_STEPS",
                "Revised VALIDATED plan must contain at least one step.",
            )
        active_workers = self._active_workers()
        self._validate_steps_runtime_policies(next_steps, active_workers)
        assessments, missing_caps = self._assess_capabilities(next_steps, active_workers)

        next_criteria = (
            list(acceptance_criteria)
            if acceptance_criteria is not None
            else list(current.plan.acceptance_criteria)
        )
        if not next_criteria:
            next_criteria = list(
                dict.fromkeys(crit for st in next_steps for crit in st.acceptance_criteria)
            )

        next_rev = current.active_revision + 1
        try:
            revised_plan = current.plan.model_copy(
                update={
                    "plan_revision": next_rev,
                    "status": "VALIDATED",
                    "goal_summary": (
                        ensure_not_secret(goal_summary, "goal_summary")
                        if goal_summary is not None
                        else current.plan.goal_summary
                    ),
                    "assumptions": (
                        list(assumptions)
                        if assumptions is not None
                        else current.plan.assumptions
                    ),
                    "missing_information": [],
                    "steps": next_steps,
                    "acceptance_criteria": next_criteria,
                    "routing_proposal": next_routing,
                    "capability_assessments": assessments,
                    "missing_capabilities": missing_caps,
                    "requires_human_approval": any(s.requires_human_approval for s in next_steps),
                    "change_summary": cleaned_summary,
                }
            )
            revised_plan = MasterWorkPlan.model_validate(revised_plan.model_dump(mode="python"))
        except (ValidationError, ValueError) as exc:
            raise PlanValidationError("INVALID_PLAN_REVISION", str(exc)) from exc

        plan_sha = revised_plan.compute_sha256()
        audit_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="plan.revised",
            source=_actor_kind_to_event_source(auth_context.actor_kind),
            task_id=current.task_id,
            actor_id=updater_id,
            provider_id=revised_plan.routing_proposal.provider_id,
            connection_id=revised_plan.routing_proposal.connection_id,
            model_id=revised_plan.routing_proposal.model_id,
            correlation_id=corr_id,
            redacted_payload={
                "plan_id": plan_id,
                "previous_revision": current.active_revision,
                "new_revision": next_rev,
                "change_summary": cleaned_summary,
                "step_count": len(revised_plan.steps),
                "missing_capabilities": missing_caps,
                "plan_sha256": plan_sha,
            },
        )
        updated_stored = StoredMasterPlan(
            plan_id=plan_id,
            task_id=current.task_id,
            active_revision=next_rev,
            record_version=current.record_version + 1,
            status="VALIDATED",
            plan=revised_plan,
            plan_sha256=plan_sha,
            created_by=current.created_by,
            created_at=current.created_at,
            updated_by=updater_id,
            updated_at=timestamp,
            last_audit_event_id=audit_ev.event_id,
        )
        revision_row = PlanRevisionRecord(
            plan_id=plan_id,
            task_id=current.task_id,
            plan_revision=next_rev,
            status="VALIDATED",
            plan=revised_plan,
            plan_sha256=plan_sha,
            change_summary=cleaned_summary,
            changed_by=updater_id,
            changed_at=timestamp,
            audit_event_id=audit_ev.event_id,
        )
        saved = self._repo.revise_plan(
            updated_stored,
            revision_row,
            expected_record_version=expected_record_version,
            audit_event=audit_ev,
        )
        return PlanningOutcome(
            outcome_status="PLAN_READY",
            task_id=current.task_id,
            plan=revised_plan,
            stored_plan=saved,
            clarifications=[],
            checklist_preview=revised_plan.to_checklist_items(created_at=timestamp),
            missing_capabilities=missing_caps,
            audit_event_id=audit_ev.event_id,
        )

    def apply_plan_to_task(
        self,
        plan_id: str,
        *,
        expected_plan_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[StoredMasterPlan, list[ChecklistItem]]:
        """Atomically materialize PENDING ChecklistItems into Task without starting execution."""
        validate_aware_datetime(timestamp, "timestamp")
        stored = self._repo.get_plan(plan_id)
        if stored is None:
            raise PlanNotFoundError("PLAN_NOT_FOUND", f"Plan '{plan_id}' does not exist.")

        corr_id = correlation_id or f"corr-plan-apply-{plan_id}"
        self._enforce_planning_governance(
            auth_context=auth_context,
            task_id=stored.task_id,
            resource=f"task:{stored.task_id}",
            timestamp=timestamp,
            routing_proposal=stored.plan.routing_proposal,
            parameters={
                "plan_id": plan_id,
                "task_id": stored.task_id,
                "plan_revision": stored.active_revision,
                "expected_record_version": expected_plan_record_version,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )
        assert auth_context is not None
        actor_agent_id = _resolve_plan_actor_id(auth_context)

        return self._apply_validated_plan(
            stored=stored,
            expected_plan_record_version=expected_plan_record_version,
            actor_agent_id=actor_agent_id,
            event_source=_actor_kind_to_event_source(auth_context.actor_kind),
            timestamp=timestamp,
            correlation_id=corr_id,
        )

    def _apply_validated_plan(
        self,
        *,
        stored: StoredMasterPlan,
        expected_plan_record_version: int,
        actor_agent_id: str,
        event_source: EventSource,
        timestamp: datetime,
        correlation_id: str,
    ) -> tuple[StoredMasterPlan, list[ChecklistItem]]:
        """Bind ChecklistItem IDs and commit APPLIED plan without changing Task execution state."""
        if stored.record_version != expected_plan_record_version:
            raise StalePlanVersionError(
                "STALE_PLAN_VERSION",
                (
                    f"Stale apply for '{stored.plan_id}': expected record_version="
                    f"{expected_plan_record_version}, found {stored.record_version}."
                ),
            )
        if stored.status in {"NEEDS_CLARIFICATION", "REJECTED", "SUPERSEDED", "DRAFT"}:
            raise PlanValidationError(
                "PLAN_NOT_APPLICABLE",
                f"Cannot apply plan '{stored.plan_id}' in status '{stored.status}'.",
            )
        if not stored.plan.steps:
            raise PlanValidationError(
                "EMPTY_PLAN_STEPS",
                f"Plan '{stored.plan_id}' has no executable steps to apply.",
            )

        checklist_items = stored.plan.to_checklist_items(created_at=timestamp)
        ordered_steps = sorted(stored.plan.steps, key=lambda item: (item.order, item.step_id))
        updated_steps = [
            step.model_copy(update={"checklist_item_id": item.checklist_item_id})
            for step, item in zip(ordered_steps, checklist_items)
        ]
        applied_plan = stored.plan.model_copy(
            update={
                "status": "APPLIED",
                "steps": updated_steps,
            }
        )
        applied_plan = MasterWorkPlan.model_validate(applied_plan.model_dump(mode="python"))
        applied_sha = applied_plan.compute_sha256()

        audit_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="plan.applied",
            source=event_source,
            task_id=stored.task_id,
            actor_id=actor_agent_id,
            provider_id=applied_plan.routing_proposal.provider_id,
            connection_id=applied_plan.routing_proposal.connection_id,
            model_id=applied_plan.routing_proposal.model_id,
            correlation_id=correlation_id,
            redacted_payload={
                "plan_id": stored.plan_id,
                "plan_revision": stored.active_revision,
                "checklist_item_ids": [item.checklist_item_id for item in checklist_items],
                "step_ids": [step.step_id for step in updated_steps],
                "plan_sha256": applied_sha,
            },
        )
        updated_stored = StoredMasterPlan(
            plan_id=stored.plan_id,
            task_id=stored.task_id,
            active_revision=stored.active_revision,
            record_version=stored.record_version + 1,
            status="APPLIED",
            plan=applied_plan,
            plan_sha256=applied_sha,
            created_by=stored.created_by,
            created_at=stored.created_at,
            updated_by=actor_agent_id,
            updated_at=timestamp,
            last_audit_event_id=audit_ev.event_id,
        )
        return self._repo.apply_plan_to_task(
            plan_id=stored.plan_id,
            expected_plan_record_version=expected_plan_record_version,
            updated_plan=updated_stored,
            checklist_items=checklist_items,
            audit_event=audit_ev,
            updated_at=timestamp,
        )

    def get_plan(self, plan_id: str) -> StoredMasterPlan | None:
        """Load active plan snapshot by ``plan_id``."""
        return self._repo.get_plan(plan_id)

    def get_active_plan_for_task(self, task_id: str) -> StoredMasterPlan | None:
        """Load active plan snapshot linked to ``task_id``."""
        return self._repo.get_active_plan_for_task(task_id)

    def list_plan_revisions(self, plan_id: str) -> list[PlanRevisionRecord]:
        """List immutable revision history for ``plan_id`` in ascending order."""
        return self._repo.list_plan_revisions(plan_id)
