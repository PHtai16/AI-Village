"""Normalized Event contract establishing references for the Phase 04.03 Event Journal."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    EventId,
    EvidenceId,
    RunId,
    SessionId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    new_event_id,
    sanitize_payload,
    validate_aware_datetime,
    validate_safe_payload,
)

EventSource = Literal[
    "core_runtime",
    "orchestrator",
    "agent_runtime",
    "engine_adapter",
    "provider",
    "tool_runtime",
    "qa_verifier",
    "governance",
    "operator",
]
EventType = Literal[
    "task.created",
    "task.status_changed",
    "run.started",
    "run.status_changed",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "run.timed_out",
    "session.initialized",
    "session.started",
    "session.status_changed",
    "session.closed",
    "agent.handoff",
    "tool.registered",
    "tool.call_proposed",
    "tool.call_observed",
    "tool.call_completed",
    "tool.call_denied",
    "tool.call_failed",
    "artifact.recorded",
    "artifact.version_registered",
    "artifact.trashed",
    "artifact.restored",
    "artifact.purged",
    "evidence.recorded",
    "knowledge.recorded",
    "vault.backup_created",
    "vault.backup_restored",
    "qa.verification_recorded",
    "checklist.item_updated",
    "governance.decision_recorded",
    "approval.requested",
    "approval.resolved",
    "notification.created",
    "reception.request_received",
    "reception.task_refined",
    "reception.clarification_requested",
    "reception.master_decision_recorded",
    "reception.interrupt_classified",
    "reception.interrupt_acknowledged",
    "reception.interrupt_escalated",
    "reception.interrupt_resolved",
    "reception.notification_suppressed",
    "workflow.cancel_requested",
    "workflow.fallback_evaluated",
    "workflow.reconciliation_recorded",
    "scheduler.task_enqueued",
    "scheduler.execution_reserved",
    "scheduler.execution_started",
    "scheduler.execution_released",
    "scheduler.execution_blocked",
    "scheduler.lease_expired",
    "scheduler.schedule_registered",
    "scheduler.key_rotation_blocked",
    "agent.registered",
    "agent.updated",
    "agent.disabled",
    "agent.enabled",
    "agent.drift_detected",
    "agent.recruitment_proposed",
    "agent.recruitment_evaluated",
    "agent.recruitment_activated",
    "agent.recruitment_rejected",
    "plan.created",
    "plan.revised",
    "plan.applied",
    "team.proposal_created",
    "team.proposal_revised",
    "team.assignment_evaluated",
    "team.assigned",
    "meeting.skipped",
    "meeting.opened",
    "meeting.turn_recorded",
    "meeting.decision_recorded",
    "meeting.closed",
    "collaboration.conflict_detected",
    "collaboration.conflict_resolved",
    "master.delivery_compiled",
    "process.retrospective_recorded",
    "process.pattern_detected",
    "skill.usage_observed",
    "skill.candidate_created",
    "skill.technical_reviewed",
    "skill.benchmark_recorded",
    "skill.activation_proposed",
    "skill.activated",
    "skill.rejected",
    "skill.deprecated",
    "skill.disabled",
    "skill.rolled_back",
    "skill.improvement_outcome_recorded",
    "team.stale_detected",
    "team.missing_capability",
    "engine.event_forwarded",
    "runtime.error",
]


def sanitize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied dictionary with credential keys and values redacted."""
    scrubbed = sanitize_payload(payload)
    if not isinstance(scrubbed, dict):
        raise ValueError("Sanitized event payload must remain a dictionary.")
    return scrubbed


class Event(BaseModel):
    """Canonical Event schema linking execution facts across Task/Run/Session/Agent/Artifact."""

    model_config = ConfigDict(extra="forbid")

    event_id: EventId = Field(default_factory=new_event_id)
    timestamp: datetime
    event_type: EventType
    source: EventSource
    task_id: TaskId | None = None
    run_id: RunId | None = None
    session_id: SessionId | None = None
    actor_id: AgentId | None = None
    provider_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    artifact_id: ArtifactId | None = None
    evidence_id: EvidenceId | None = None
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)
    redacted_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp; never fabricate timestamps on construction."""
        return validate_aware_datetime(value, "timestamp")

    @field_validator("model_id", "correlation_id")
    @classmethod
    def _validate_optional_labels(cls, value: str | None, info: object) -> str | None:
        """Reject blank or secret-bearing optional event identifiers."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "event_field"))

    @field_validator("redacted_payload")
    @classmethod
    def _validate_redacted_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject secret keys or raw credentials anywhere inside redacted_payload fail-closed."""
        validate_safe_payload(value, "redacted_payload")
        return value

    @model_validator(mode="after")
    def _require_attribution_anchor(self) -> Event:
        """Ensure every Event traces to at least one domain anchor while optional fields stay None."""
        anchors = (
            self.task_id,
            self.run_id,
            self.session_id,
            self.actor_id,
            self.artifact_id,
            self.correlation_id,
        )
        if all(anchor is None for anchor in anchors):
            raise ValueError(
                "Event requires at least one attribution anchor (task_id, run_id, session_id, actor_id, artifact_id, or correlation_id)."
            )
        return self
