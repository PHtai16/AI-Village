"""Pydantic v2 Governance, Policy, Routing, and Human Approval Gate contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.event import EventSource
from core.contracts.identifiers import (
    EventId,
    RunId,
    TaskId,
    ensure_not_secret,
    reject_secret_text,
    validate_aware_datetime,
    validate_safe_payload,
    validate_semver,
)

ActorKind = Literal[
    "human_operator",
    "master_agent",
    "worker_agent",
    "runtime_service",
    "trusted_verifier",
]
AuthSource = Literal["application_boundary", "operator_session", "test_fixture", "agent_claimed"]
RuleCategory = Literal[
    "invariant",
    "system_policy",
    "permission",
    "agent_behavior",
    "task_constraint",
    "skill_execution",
    "approval",
    "verification",
]
RuleEffect = Literal["ALLOW", "DENY", "REQUIRE_APPROVAL", "AUDIT_ONLY"]
RuleStatus = Literal["DRAFT", "UNDER_REVIEW", "ACTIVE", "DEPRECATED", "DISABLED"]
PolicyDecisionOutcome = Literal["ALLOW", "DENY", "APPROVAL_REQUIRED", "ERROR"]
ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CONSUMED"]


def new_approval_id() -> str:
    """Generate a unique canonical ``apr_*`` ApprovalRequest identifier."""
    return f"apr_{uuid.uuid4().hex[:16]}"


class TrustedAuthContext(BaseModel):
    """Caller authorization context verified at an application boundary or fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str = Field(min_length=1, max_length=128)
    actor_kind: ActorKind
    auth_source: AuthSource
    authenticated: bool
    allowed_actions: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    assigned_task_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("actor_id")
    @classmethod
    def _validate_actor_id(cls, value: str) -> str:
        """Ensure actor identifier is non-blank and free of credentials."""
        return ensure_not_secret(value, "actor_id")

    @model_validator(mode="after")
    def _prevent_spoofed_operator_context(self) -> TrustedAuthContext:
        """Reject agent-claimed operator identity claims fail-closed."""
        if self.actor_kind == "human_operator" and self.auth_source == "agent_claimed":
            raise ValueError(
                "Agent-claimed context cannot authenticate as a trusted human_operator."
            )
        return self


_ACTOR_KIND_TO_SOURCE: dict[str, EventSource] = {
    "operator": "operator",
    "human_operator": "operator",
    "orchestrator": "orchestrator",
    "master_agent": "orchestrator",
    "core_runtime": "core_runtime",
    "runtime_service": "core_runtime",
    "qa_verifier": "qa_verifier",
    "trusted_verifier": "qa_verifier",
    "worker_agent": "agent_runtime",
}

_TO_GOV_ACTOR_KIND: dict[str, ActorKind] = {
    "operator": "human_operator",
    "human_operator": "human_operator",
    "orchestrator": "master_agent",
    "master_agent": "master_agent",
    "core_runtime": "runtime_service",
    "runtime_service": "runtime_service",
    "governance": "runtime_service",
    "qa_verifier": "trusted_verifier",
    "trusted_verifier": "trusted_verifier",
    "worker_agent": "worker_agent",
}


def actor_kind_to_event_source(actor_kind: str) -> EventSource:
    """Map workflow or governance ``actor_kind`` into canonical ``EventSource``."""
    return _ACTOR_KIND_TO_SOURCE.get(actor_kind, "governance")


def resolve_trusted_auth_context(
    *,
    auth_context: TrustedAuthContext | None,
    actor_id: str,
    actor_kind: str,
) -> TrustedAuthContext:
    """Return explicit caller ``auth_context`` or build bounded context without minting operator auth."""
    if auth_context is not None:
        return auth_context
    gov_kind = _TO_GOV_ACTOR_KIND.get(actor_kind)
    if gov_kind is None:
        return TrustedAuthContext(
            actor_id=actor_id,
            actor_kind="worker_agent",
            auth_source="agent_claimed",
            authenticated=False,
        )
    if gov_kind == "human_operator":
        # Never mint authenticated operator session from unverified string request fields.
        return TrustedAuthContext(
            actor_id=actor_id,
            actor_kind="human_operator",
            auth_source="application_boundary",
            authenticated=False,
        )
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind=gov_kind,
        auth_source="application_boundary",
        authenticated=True,
    )


class PolicyRule(BaseModel):
    """Structured governance rule matching ``RULE_SPEC.md``."""

    model_config = ConfigDict(extra="forbid")

    spec_version: str = Field(default="1.0.0")
    rule_id: str = Field(min_length=3, max_length=128)
    version: str = Field(default="1.0.0")
    name: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    category: RuleCategory
    targets: list[str] = Field(min_length=1, max_length=50)
    resources: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=50)
    priority: int = Field(default=100, ge=1, le=1000)
    effect: RuleEffect
    actions: list[str] = Field(min_length=1, max_length=50)
    approval_required: bool = False
    status: RuleStatus = "ACTIVE"

    @field_validator("spec_version", "version")
    @classmethod
    def _validate_versions(cls, value: str) -> str:
        """Enforce SemVer on rule versions."""
        return validate_semver(value)

    @model_validator(mode="after")
    def _validate_approval_flag(self) -> PolicyRule:
        """Enforce RULE_SPEC invariant that REQUIRE_APPROVAL requires approval_required=True."""
        if self.effect == "REQUIRE_APPROVAL" and not self.approval_required:
            raise ValueError("PolicyRule with effect='REQUIRE_APPROVAL' must set approval_required=True.")
        return self


class RoutingContext(BaseModel):
    """Provider, connection, residency, and budget attributes evaluated before routing."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=64)
    connection_id: str | None = Field(default=None, min_length=1, max_length=64)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    data_residency: Literal["local_only", "cloud"] = "cloud"
    target_residency: Literal["local_only", "cloud"] = "cloud"
    billing_scope: str | None = Field(default=None, min_length=1, max_length=128)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    budget_limit_usd: float | None = Field(default=None, ge=0)
    allowed_providers: list[str] = Field(default_factory=list, max_length=32)
    allowed_connections: list[str] = Field(default_factory=list, max_length=64)
    quota_remaining: int | None = Field(default=None, ge=0)

    @field_validator("provider_id", "connection_id", "model_id", "billing_scope")
    @classmethod
    def _validate_non_secret(cls, value: str | None, info: object) -> str | None:
        """Ensure routing metadata never stores credential values."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "routing"))


class PolicyRequest(BaseModel):
    """Normalized action request evaluated by ``PolicyEvaluator`` and ``PermissionManager``."""

    model_config = ConfigDict(extra="forbid")

    auth_context: TrustedAuthContext | None = None
    action: str = Field(min_length=1, max_length=64)
    resource: str = Field(min_length=1, max_length=512)
    task_id: TaskId | None = None
    run_id: RunId | None = None
    workspace_root: str | None = None
    target_path: str | None = None
    cross_task_roots: list[str] = Field(default_factory=list, max_length=20)
    tool_id: str | None = None
    routing: RoutingContext | None = None
    approval_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware evaluation timestamp."""
        return validate_aware_datetime(value, "timestamp")

    @field_validator("action", "resource", "tool_id", "approval_id", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject blank or secret-bearing request labels."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "policy_request"))

    @field_validator("parameters")
    @classmethod
    def _validate_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject secrets inside arbitrary action parameters."""
        validate_safe_payload(value, "policy_request.parameters")
        return value


class PolicyDecision(BaseModel):
    """Structured fail-closed Governance decision returned by ``PolicyEvaluator``."""

    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecisionOutcome
    actor_id: str | None = None
    action: str
    resource: str
    matched_rule_ids: list[str] = Field(default_factory=list)
    rule_versions: dict[str, str] = Field(default_factory=dict)
    reason_code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=1000)
    evaluated_at: datetime
    correlation_id: str | None = None
    audit_event_id: EventId | None = None
    system_deny: bool = False
    request_fingerprint: str = Field(min_length=64, max_length=64)


class ApprovalPolicyContext(BaseModel):
    """Immutable snapshot of policy versions active when an ApprovalRequest was created."""

    model_config = ConfigDict(extra="forbid")

    matched_rule_ids: list[str] = Field(default_factory=list)
    rule_versions: dict[str, str] = Field(default_factory=dict)
    reason_code: str = Field(min_length=1, max_length=80)


class ApprovalRequest(BaseModel):
    """Persistent Human Approval Gate record bound to a specific fingerprint and resource."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(default_factory=new_approval_id, min_length=6, max_length=64)
    requester_id: str = Field(min_length=1, max_length=128)
    requester_kind: ActorKind
    action: str = Field(min_length=1, max_length=64)
    target_resource: str = Field(min_length=1, max_length=512)
    task_id: TaskId | None = None
    run_id: RunId | None = None
    request_fingerprint: str = Field(min_length=64, max_length=64)
    policy_snapshot: ApprovalPolicyContext
    risk_summary: str = Field(min_length=1, max_length=1000)
    status: ApprovalStatus = "PENDING"
    requested_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    consumed_at: datetime | None = None
    request_event_id: EventId | None = None
    decision_event_id: EventId | None = None
    consumed_event_id: EventId | None = None
    version: int = Field(default=1, ge=1)

    @field_validator("risk_summary", "decision_reason")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        """Reject secret patterns in human-visible approval summaries."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, getattr(info, "field_name", "approval_text"))
        return cleaned or None

    @field_validator("requested_at", "expires_at", "decided_at", "consumed_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Enforce timezone-aware timestamps."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "approval_ts"))


class ApprovalDecision(BaseModel):
    """Human operator decision outcome returned by ``ApprovalService.decide_approval``."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    status: ApprovalStatus
    decided_by: str
    decided_at: datetime
    reason: str
    decision_event_id: EventId
