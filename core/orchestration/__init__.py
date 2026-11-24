"""P03.01 Master -> Worker -> QA -> Master integration spike modules."""

from __future__ import annotations

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

__all__ = [
    "ArtifactExpectation",
    "AttributedSpikeEvent",
    "MasterPlan",
    "MasterSynthesis",
    "MasterWorkerSpikeResult",
    "OrchestrationError",
    "SpikeAgentSpec",
    "WorkerExecutionRecord",
    "WorkerHandoff",
    "build_default_spike_roster",
    "parse_strict_json_object",
    "run_master_worker_spike",
    "validate_master_plan_policy",
]
