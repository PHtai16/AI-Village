"""P03.01 spike modules and Phase 05 Master Planning / Dynamic Team Builder exports."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from core.orchestration.spike_coordinator import (
    run_master_worker_spike,
    validate_master_plan_policy,
)
from core.orchestration.spike_models import (
    ArtifactExpectation,
    AttributedSpikeEvent,
    MasterPlan,
    MasterSynthesis,
    MasterWorkerSpikeResult,
    OrchestrationError,
    SpikeAgentSpec,
    WorkerExecutionRecord,
    WorkerHandoff,
    build_default_spike_roster,
    parse_strict_json_object,
)

if TYPE_CHECKING:
    from core.orchestration.planning import (
        MasterEnginePlanPayload,
        MasterEngineStepPayload,
        MasterPlanningService,
        analyze_brief_completeness,
        build_master_planning_prompt,
        parse_master_plan_json,
    )
    from core.orchestration.planning_models import (
        MasterWorkPlan,
        PlanApprovalRequiredError,
        PlanCapabilityAssessment,
        PlanGovernanceDeniedError,
        PlanId,
        PlanNotFoundError,
        PlanRepository,
        PlanRevisionRecord,
        PlanRoutingProposal,
        PlanStatus,
        PlanStepSpec,
        PlanValidationError,
        PlanningClarification,
        PlanningError,
        PlanningOutcome,
        PlanningOutcomeStatus,
        StalePlanVersionError,
        StepId,
        StoredMasterPlan,
        StructuredUserBrief,
        new_plan_id,
        validate_plan_id,
        validate_step_id,
    )

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "MasterEnginePlanPayload": ("core.orchestration.planning", "MasterEnginePlanPayload"),
    "MasterEngineStepPayload": ("core.orchestration.planning", "MasterEngineStepPayload"),
    "MasterPlanningService": ("core.orchestration.planning", "MasterPlanningService"),
    "analyze_brief_completeness": ("core.orchestration.planning", "analyze_brief_completeness"),
    "build_master_planning_prompt": ("core.orchestration.planning", "build_master_planning_prompt"),
    "parse_master_plan_json": ("core.orchestration.planning", "parse_master_plan_json"),
    "MasterWorkPlan": ("core.orchestration.planning_models", "MasterWorkPlan"),
    "PlanApprovalRequiredError": ("core.orchestration.planning_models", "PlanApprovalRequiredError"),
    "PlanCapabilityAssessment": ("core.orchestration.planning_models", "PlanCapabilityAssessment"),
    "PlanGovernanceDeniedError": ("core.orchestration.planning_models", "PlanGovernanceDeniedError"),
    "PlanId": ("core.orchestration.planning_models", "PlanId"),
    "PlanNotFoundError": ("core.orchestration.planning_models", "PlanNotFoundError"),
    "PlanRepository": ("core.orchestration.planning_models", "PlanRepository"),
    "PlanRevisionRecord": ("core.orchestration.planning_models", "PlanRevisionRecord"),
    "PlanRoutingProposal": ("core.orchestration.planning_models", "PlanRoutingProposal"),
    "PlanStatus": ("core.orchestration.planning_models", "PlanStatus"),
    "PlanStepSpec": ("core.orchestration.planning_models", "PlanStepSpec"),
    "PlanValidationError": ("core.orchestration.planning_models", "PlanValidationError"),
    "PlanningClarification": ("core.orchestration.planning_models", "PlanningClarification"),
    "PlanningError": ("core.orchestration.planning_models", "PlanningError"),
    "PlanningOutcome": ("core.orchestration.planning_models", "PlanningOutcome"),
    "PlanningOutcomeStatus": ("core.orchestration.planning_models", "PlanningOutcomeStatus"),
    "StalePlanVersionError": ("core.orchestration.planning_models", "StalePlanVersionError"),
    "StepId": ("core.orchestration.planning_models", "StepId"),
    "StoredMasterPlan": ("core.orchestration.planning_models", "StoredMasterPlan"),
    "StructuredUserBrief": ("core.orchestration.planning_models", "StructuredUserBrief"),
    "new_plan_id": ("core.orchestration.planning_models", "new_plan_id"),
    "validate_plan_id": ("core.orchestration.planning_models", "validate_plan_id"),
    "validate_step_id": ("core.orchestration.planning_models", "validate_step_id"),
}


def __getattr__(name: str) -> Any:
    """Lazily resolve Phase 05 orchestration exports to avoid import cycles with core.contracts."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'core.orchestration' has no attribute {name!r}")
    module_name, attr_name = target
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


__all__ = [
    "ArtifactExpectation",
    "AttributedSpikeEvent",
    "MasterEnginePlanPayload",
    "MasterEngineStepPayload",
    "MasterPlan",
    "MasterPlanningService",
    "MasterSynthesis",
    "MasterWorkPlan",
    "MasterWorkerSpikeResult",
    "OrchestrationError",
    "PlanApprovalRequiredError",
    "PlanCapabilityAssessment",
    "PlanGovernanceDeniedError",
    "PlanId",
    "PlanNotFoundError",
    "PlanRepository",
    "PlanRevisionRecord",
    "PlanRoutingProposal",
    "PlanStatus",
    "PlanStepSpec",
    "PlanValidationError",
    "PlanningClarification",
    "PlanningError",
    "PlanningOutcome",
    "PlanningOutcomeStatus",
    "SpikeAgentSpec",
    "StalePlanVersionError",
    "StepId",
    "StoredMasterPlan",
    "StructuredUserBrief",
    "WorkerExecutionRecord",
    "WorkerHandoff",
    "analyze_brief_completeness",
    "build_default_spike_roster",
    "build_master_planning_prompt",
    "new_plan_id",
    "parse_master_plan_json",
    "parse_strict_json_object",
    "run_master_worker_spike",
    "validate_master_plan_policy",
    "validate_plan_id",
    "validate_step_id",
]
