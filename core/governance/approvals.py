"""Human Approval Gate service preventing self-approval, expiration bypass, tampering, and replay."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol, TypeVar, runtime_checkable

from core.contracts.event import Event
from core.contracts.identifiers import new_event_id
from core.events.journal import EventJournal
from core.governance.models import (
    ApprovalDecision,
    ApprovalPolicyContext,
    ApprovalRequest,
    TrustedAuthContext,
)
from engines.antigravity.diagnostics import redact_secrets

T = TypeVar("T")


class ApprovalError(RuntimeError):
    """Structured fail-closed exception raised during approval creation, decision, or consumption."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


@runtime_checkable
class ApprovalRepository(Protocol):
    """Durable persistence port for ``ApprovalRequest`` records."""

    def create_approval(self, approval: ApprovalRequest) -> ApprovalRequest:
        """Persist a new PENDING ``ApprovalRequest``."""

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        """Load an ``ApprovalRequest`` by identifier."""

    def update_decision(
        self,
        approval: ApprovalRequest,
        *,
        expected_version: int,
    ) -> ApprovalRequest:
        """Persist APPROVED, REJECTED, or EXPIRED under optimistic locking."""

    def consume_and_execute(
        self,
        *,
        approval_id: str,
        expected_version: int,
        consumed_at: datetime,
        consumed_event: Event,
        action_fn: Callable[[], T],
    ) -> tuple[ApprovalRequest, T]:
        """Atomically mark approval CONSUMED, run ``action_fn()``, and append audit Event or rollback."""


class ApprovalService:
    """Manages lifecycle, security checks, and single-use consumption of Human Approvals."""

    def __init__(
        self,
        repository: ApprovalRepository,
        journal: EventJournal,
    ) -> None:
        """Bind service to a durable ``ApprovalRepository`` and ``EventJournal``."""
        self._repo = repository
        self._journal = journal

    def create_approval_request(
        self,
        *,
        requester: TrustedAuthContext,
        action: str,
        target_resource: str,
        task_id: str | None,
        run_id: str | None,
        request_fingerprint: str,
        policy_snapshot: ApprovalPolicyContext,
        risk_summary: str,
        requested_at: datetime,
        expires_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> ApprovalRequest:
        """Create and persist a PENDING ``ApprovalRequest`` plus ``approval.requested`` Event."""
        if expires_at is not None and expires_at <= requested_at:
            raise ApprovalError(
                "INVALID_EXPIRATION",
                "Approval expires_at must be strictly after requested_at.",
            )

        req_event = Event(
            event_id=new_event_id(),
            timestamp=requested_at,
            event_type="approval.requested",
            source="governance",
            task_id=task_id,
            run_id=run_id,
            correlation_id=correlation_id or f"corr-{request_fingerprint[:12]}",
            redacted_payload={
                "requester_id": requester.actor_id,
                "requester_kind": requester.actor_kind,
                "action": action,
                "target_resource": target_resource,
                "request_fingerprint": request_fingerprint,
            },
        )
        append_res = self._journal.append(req_event)
        if append_res.status == "REJECTED":
            raise ApprovalError(
                "AUDIT_APPEND_FAILED",
                append_res.error.message if append_res.error else "Failed to audit approval request.",
            )

        record = ApprovalRequest(
            requester_id=requester.actor_id,
            requester_kind=requester.actor_kind,
            action=action,
            target_resource=target_resource,
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=request_fingerprint,
            policy_snapshot=policy_snapshot,
            risk_summary=redact_secrets(risk_summary),
            status="PENDING",
            requested_at=requested_at,
            expires_at=expires_at,
            request_event_id=req_event.event_id,
        )
        return self._repo.create_approval(record)

    def decide_approval(
        self,
        approval_id: str,
        *,
        decider: TrustedAuthContext,
        approved: bool,
        reason: str,
        timestamp: datetime,
        correlation_id: str | None = None,
    ) -> ApprovalDecision:
        """Approve or reject a PENDING ``ApprovalRequest`` strictly by an authenticated human operator."""
        if not decider.authenticated or decider.actor_kind != "human_operator":
            raise ApprovalError(
                "UNAUTHORIZED_APPROVER",
                "Only an authenticated human_operator may decide an ApprovalRequest.",
            )
        if decider.auth_source == "agent_claimed":
            raise ApprovalError(
                "UNAUTHORIZED_APPROVER",
                "Untrusted agent-claimed operator identity cannot decide approvals.",
            )

        current = self._repo.get_approval(approval_id)
        if current is None:
            raise ApprovalError("APPROVAL_NOT_FOUND", f"Approval '{approval_id}' does not exist.")
        if current.requester_id == decider.actor_id:
            raise ApprovalError(
                "SELF_APPROVAL_FORBIDDEN",
                f"Requester '{current.requester_id}' cannot approve their own request.",
            )
        if current.status != "PENDING":
            raise ApprovalError(
                "APPROVAL_NOT_PENDING",
                f"Approval '{approval_id}' is already in status '{current.status}'.",
            )

        is_expired = current.expires_at is not None and timestamp >= current.expires_at
        next_status = "EXPIRED" if is_expired else ("APPROVED" if approved else "REJECTED")
        resolved_event = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="approval.resolved",
            source="operator" if not is_expired else "governance",
            task_id=current.task_id,
            run_id=current.run_id,
            correlation_id=correlation_id or f"corr-{current.request_fingerprint[:12]}",
            redacted_payload={
                "approval_id": approval_id,
                "decision": next_status,
                "decided_by": decider.actor_id,
                "reason": redact_secrets(reason),
            },
        )
        append_res = self._journal.append(resolved_event)
        if append_res.status == "REJECTED":
            raise ApprovalError(
                "AUDIT_APPEND_FAILED",
                append_res.error.message if append_res.error else "Failed to record approval decision.",
            )

        updated = current.model_copy(
            update={
                "status": next_status,
                "decided_at": timestamp,
                "decided_by": decider.actor_id,
                "decision_reason": redact_secrets(reason),
                "decision_event_id": resolved_event.event_id,
            }
        )
        saved = self._repo.update_decision(updated, expected_version=current.version)
        if is_expired:
            raise ApprovalError(
                "APPROVAL_EXPIRED",
                f"Approval '{approval_id}' expired at {current.expires_at.isoformat() if current.expires_at else ''}.",
            )

        return ApprovalDecision(
            approval_id=saved.approval_id,
            status=saved.status,
            decided_by=decider.actor_id,
            decided_at=timestamp,
            reason=redact_secrets(reason),
            decision_event_id=resolved_event.event_id,
        )

    def verify_and_consume(
        self,
        approval_id: str,
        *,
        expected_action: str,
        expected_resource: str,
        expected_task_id: str | None,
        expected_run_id: str | None,
        expected_fingerprint: str,
        timestamp: datetime,
        system_deny: bool,
        action_fn: Callable[[], T],
        correlation_id: str | None = None,
    ) -> tuple[ApprovalRequest, T]:
        """Verify approval validity and atomically consume it once to run ``action_fn``."""
        if system_deny:
            raise ApprovalError(
                "SYSTEM_DENY_NOT_OVERRIDABLE",
                "Human approvals cannot override System Policies or Architectural Invariants.",
            )

        current = self._repo.get_approval(approval_id)
        if current is None:
            raise ApprovalError("APPROVAL_NOT_FOUND", f"Approval '{approval_id}' does not exist.")
        if current.status == "CONSUMED":
            raise ApprovalError(
                "APPROVAL_ALREADY_CONSUMED",
                f"Single-use approval '{approval_id}' has already been consumed.",
            )
        if current.status == "REJECTED":
            raise ApprovalError(
                "APPROVAL_REJECTED",
                f"Approval '{approval_id}' was rejected and cannot execute.",
            )
        if current.status == "EXPIRED":
            raise ApprovalError("APPROVAL_EXPIRED", f"Approval '{approval_id}' is expired.")
        if current.status != "APPROVED":
            raise ApprovalError(
                "APPROVAL_NOT_APPROVED",
                f"Approval '{approval_id}' is in status '{current.status}', not APPROVED.",
            )
        if current.expires_at is not None and timestamp >= current.expires_at:
            exp_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="approval.resolved",
                source="governance",
                task_id=current.task_id,
                run_id=current.run_id,
                correlation_id=correlation_id or f"corr-{current.request_fingerprint[:12]}",
                redacted_payload={"approval_id": approval_id, "decision": "EXPIRED"},
            )
            self._journal.append(exp_ev)
            self._repo.update_decision(
                current.model_copy(
                    update={
                        "status": "EXPIRED",
                        "decided_at": timestamp,
                        "decision_event_id": exp_ev.event_id,
                    }
                ),
                expected_version=current.version,
            )
            raise ApprovalError("APPROVAL_EXPIRED", f"Approval '{approval_id}' expired before use.")

        if current.action != expected_action or current.target_resource != expected_resource:
            raise ApprovalError(
                "APPROVAL_SCOPE_MISMATCH",
                f"Approval '{approval_id}' scope ({current.action}, {current.target_resource}) does not match request.",
            )
        if current.task_id != expected_task_id or current.run_id != expected_run_id:
            raise ApprovalError(
                "APPROVAL_TASK_RUN_MISMATCH",
                f"Approval '{approval_id}' belongs to Task/Run ({current.task_id}, {current.run_id}), not ({expected_task_id}, {expected_run_id}).",
            )
        if current.request_fingerprint != expected_fingerprint:
            raise ApprovalError(
                "APPROVAL_FINGERPRINT_MISMATCH",
                f"Approval '{approval_id}' parameters changed since approval was requested.",
            )

        consumed_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="governance.decision_recorded",
            source="governance",
            task_id=expected_task_id,
            run_id=expected_run_id,
            correlation_id=correlation_id or f"corr-{expected_fingerprint[:12]}",
            redacted_payload={
                "approval_id": approval_id,
                "action": expected_action,
                "target_resource": expected_resource,
                "outcome": "APPROVAL_CONSUMED",
            },
        )
        return self._repo.consume_and_execute(
            approval_id=approval_id,
            expected_version=current.version,
            consumed_at=timestamp,
            consumed_event=consumed_ev,
            action_fn=action_fn,
        )
