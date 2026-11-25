"""Policy-governed retry and fallback evaluation without unauthorized Provider switching."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    RunId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
)

DataResidency = Literal["local_only", "cloud"]
FallbackDecisionOutcome = Literal["ALLOW_RETRY", "ALLOW_FALLBACK", "APPROVAL_REQUIRED", "DENY"]


class FallbackTarget(BaseModel):
    """Explicit target configuration eligible for retry or policy-controlled fallback."""

    model_config = ConfigDict(extra="forbid")

    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    data_residency: DataResidency = "cloud"
    billing_scope: str | None = Field(default=None, min_length=1, max_length=128)
    estimated_cost_usd: float | None = Field(default=None, ge=0)

    @field_validator("model_id", "billing_scope")
    @classmethod
    def _validate_non_secret(cls, value: str | None, info: object) -> str | None:
        """Ensure optional model or billing labels never hold credentials."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "fallback_target"))


class FallbackPolicy(BaseModel):
    """Explicit fail-closed fallback boundaries bound to a Task/Run workflow."""

    model_config = ConfigDict(extra="forbid")

    fallback_allowed: bool = False
    max_retries: int = Field(default=0, ge=0, le=5)
    allowed_targets: list[FallbackTarget] = Field(default_factory=list, max_length=16)
    allow_provider_switch: bool = False
    allow_data_residency_change: bool = False
    allow_billing_scope_change: bool = False
    max_budget_usd: float | None = Field(default=None, ge=0)
    require_idempotent_operation: bool = True


class FallbackEvaluationRequest(BaseModel):
    """Input evaluated before retrying or failing over to an alternate target."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId
    actor_id: AgentId = "core.master"
    current_target: FallbackTarget
    candidate_target: FallbackTarget | None = None
    attempt_number: int = Field(ge=1)
    operation_is_idempotent: bool
    failure_reason_code: str = Field(min_length=1, max_length=64)
    available_target_capabilities: list[str] = Field(default_factory=list, max_length=64)
    is_key_rotation_attempt: bool = False
    requested_at: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("requested_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "requested_at")

    @field_validator("failure_reason_code", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secrets or blank labels."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "fallback_request"))


class FallbackDecision(BaseModel):
    """Structured fail-closed decision plus traceable audit Event."""

    model_config = ConfigDict(extra="forbid")

    decision: FallbackDecisionOutcome
    reason_code: str = Field(min_length=1, max_length=64)
    selected_target: FallbackTarget | None = None
    message: str = Field(min_length=1, max_length=1000)
    audit_event: Event


def _build_fallback_event(
    request: FallbackEvaluationRequest,
    *,
    decision: FallbackDecisionOutcome,
    reason_code: str,
    selected: FallbackTarget | None,
) -> Event:
    """Create a secret-safe ``workflow.fallback_evaluated`` Event."""
    return Event(
        timestamp=request.requested_at,
        event_type="workflow.fallback_evaluated",
        source="core_runtime",
        task_id=request.task_id,
        run_id=request.run_id,
        actor_id=request.actor_id,
        provider_id=selected.provider_id if selected else request.current_target.provider_id,
        connection_id=selected.connection_id if selected else request.current_target.connection_id,
        model_id=selected.model_id if selected else request.current_target.model_id,
        correlation_id=request.correlation_id,
        redacted_payload={
            "decision": decision,
            "reason_code": reason_code,
            "attempt_number": request.attempt_number,
            "failure_reason_code": request.failure_reason_code,
            "from_provider_id": request.current_target.provider_id,
            "from_connection_id": request.current_target.connection_id,
            "to_provider_id": selected.provider_id if selected else None,
            "to_connection_id": selected.connection_id if selected else None,
        },
    )


def evaluate_fallback(
    policy: FallbackPolicy,
    request: FallbackEvaluationRequest,
) -> FallbackDecision:
    """Evaluate retry or fallback fail-closed without silent key rotation or residency breach."""
    if request.is_key_rotation_attempt:
        code = "KEY_ROTATION_FORBIDDEN"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message="Rotating API keys/connections to bypass provider rate or quota limits is forbidden.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    if policy.require_idempotent_operation and not request.operation_is_idempotent:
        code = "NON_IDEMPOTENT_OPERATION"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message="Refusing retry/fallback for side-effecting non-idempotent operation.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    if request.attempt_number > policy.max_retries:
        code = "RETRY_BUDGET_EXHAUSTED"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message=f"Attempt {request.attempt_number} exceeds max_retries={policy.max_retries}.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    candidate = request.candidate_target
    if candidate is None or (
        candidate.provider_id == request.current_target.provider_id
        and candidate.connection_id == request.current_target.connection_id
        and candidate.model_id == request.current_target.model_id
    ):
        missing_caps = set(request.current_target.required_capabilities) - set(
            request.available_target_capabilities
        )
        if missing_caps:
            code = "UNSUPPORTED_CAPABILITY"
            return FallbackDecision(
                decision="DENY",
                reason_code=code,
                message=f"Target lacks required capabilities: {sorted(missing_caps)}.",
                audit_event=_build_fallback_event(
                    request, decision="DENY", reason_code=code, selected=None
                ),
            )
        code = "SAME_TARGET_RETRY_ALLOWED"
        return FallbackDecision(
            decision="ALLOW_RETRY",
            reason_code=code,
            selected_target=request.current_target,
            message="Retry allowed within max_retries on existing target.",
            audit_event=_build_fallback_event(
                request,
                decision="ALLOW_RETRY",
                reason_code=code,
                selected=request.current_target,
            ),
        )

    if not policy.fallback_allowed:
        code = "FALLBACK_DISABLED_BY_POLICY"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message="Fallback to an alternate target is disabled by policy.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    if (
        candidate.provider_id == request.current_target.provider_id
        and candidate.connection_id != request.current_target.connection_id
        and request.failure_reason_code in {"RATE_LIMITED", "QUOTA_EXCEEDED"}
    ):
        code = "KEY_ROTATION_FORBIDDEN"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message="Switching connections within the same provider on quota/rate-limit is forbidden.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    matched = [
        target
        for target in policy.allowed_targets
        if target.provider_id == candidate.provider_id
        and target.connection_id == candidate.connection_id
        and target.model_id == candidate.model_id
    ]
    if not matched:
        code = "TARGET_NOT_CONFIGURED"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message=(
                f"Candidate target {candidate.provider_id}/{candidate.connection_id}/{candidate.model_id} "
                "is not in allowed_targets."
            ),
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    if (
        candidate.provider_id != request.current_target.provider_id
        and not policy.allow_provider_switch
    ):
        code = "PROVIDER_SWITCH_FORBIDDEN"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message="Policy forbids automatic switching to a different provider.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    missing_caps = set(candidate.required_capabilities) - set(request.available_target_capabilities)
    if missing_caps:
        code = "UNSUPPORTED_CAPABILITY"
        return FallbackDecision(
            decision="DENY",
            reason_code=code,
            message=f"Fallback target lacks required capabilities: {sorted(missing_caps)}.",
            audit_event=_build_fallback_event(request, decision="DENY", reason_code=code, selected=None),
        )

    if (
        request.current_target.data_residency == "local_only"
        and candidate.data_residency != "local_only"
        and not policy.allow_data_residency_change
    ):
        code = "DATA_RESIDENCY_CHANGE_REQUIRES_APPROVAL"
        return FallbackDecision(
            decision="APPROVAL_REQUIRED",
            reason_code=code,
            selected_target=candidate,
            message="Fallback moves local_only workload to cloud residency; operator approval required.",
            audit_event=_build_fallback_event(
                request, decision="APPROVAL_REQUIRED", reason_code=code, selected=candidate
            ),
        )

    if (
        candidate.billing_scope != request.current_target.billing_scope
        and not policy.allow_billing_scope_change
    ):
        code = "BILLING_SCOPE_CHANGE_REQUIRES_APPROVAL"
        return FallbackDecision(
            decision="APPROVAL_REQUIRED",
            reason_code=code,
            selected_target=candidate,
            message="Fallback alters billing scope; operator approval required.",
            audit_event=_build_fallback_event(
                request, decision="APPROVAL_REQUIRED", reason_code=code, selected=candidate
            ),
        )

    if (
        policy.max_budget_usd is not None
        and candidate.estimated_cost_usd is not None
        and candidate.estimated_cost_usd > policy.max_budget_usd
    ):
        code = "BUDGET_EXCEEDED_REQUIRES_APPROVAL"
        return FallbackDecision(
            decision="APPROVAL_REQUIRED",
            reason_code=code,
            selected_target=candidate,
            message=(
                f"Fallback estimated cost ${candidate.estimated_cost_usd:.4f} exceeds "
                f"budget limit ${policy.max_budget_usd:.4f}."
            ),
            audit_event=_build_fallback_event(
                request, decision="APPROVAL_REQUIRED", reason_code=code, selected=candidate
            ),
        )

    code = "POLICY_APPROVED_FALLBACK"
    return FallbackDecision(
        decision="ALLOW_FALLBACK",
        reason_code=code,
        selected_target=candidate,
        message="Fallback allowed under explicit policy allowlist and constraints.",
        audit_event=_build_fallback_event(
            request, decision="ALLOW_FALLBACK", reason_code=code, selected=candidate
        ),
    )
