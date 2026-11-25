"""Permission Manager integrating PolicyEvaluator, ApprovalService, EventJournal, and Workflow boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, TypeVar

from core.contracts.event import Event
from core.contracts.identifiers import new_event_id
from core.events.journal import EventJournal
from core.governance.approvals import ApprovalError, ApprovalService
from core.governance.evaluator import PolicyEvaluator
from core.governance.models import (
    PolicyDecision,
    PolicyRequest,
    TrustedAuthContext,
)
from engines.antigravity.diagnostics import redact_secrets

T = TypeVar("T")


class GovernanceAuditError(RuntimeError):
    """Raised when mandatory governance audit trail fails fail-closed."""


class PermissionDeniedError(RuntimeError):
    """Raised when Governance denies or blocks a protected action."""

    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.message)
        self.decision = decision


class PermissionManager:
    """Enforces policies, mandatory audit logging, and Human Approval consumption."""

    def __init__(
        self,
        *,
        evaluator: PolicyEvaluator,
        journal: EventJournal,
        approval_service: ApprovalService | None = None,
    ) -> None:
        """Compose PolicyEvaluator, EventJournal, and optional ApprovalService."""
        self._evaluator = evaluator
        self._journal = journal
        self._approvals = approval_service

    @property
    def evaluator(self) -> PolicyEvaluator:
        """Expose underlying deterministic PolicyEvaluator."""
        return self._evaluator

    @property
    def approval_service(self) -> ApprovalService | None:
        """Expose bound ApprovalService if configured."""
        return self._approvals

    def evaluate_and_audit(self, request: PolicyRequest) -> PolicyDecision:
        """Evaluate ``request`` and write mandatory Audit Event before allowing action."""
        decision = self._evaluator.evaluate(request)
        audit_ev = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="governance.decision_recorded",
            source="governance",
            task_id=request.task_id,
            run_id=request.run_id,
            correlation_id=request.correlation_id or f"corr-{decision.request_fingerprint[:12]}",
            redacted_payload={
                "decision": decision.decision,
                "actor_id": decision.actor_id,
                "actor_kind": request.auth_context.actor_kind if request.auth_context else None,
                "action": decision.action,
                "resource": decision.resource,
                "reason_code": decision.reason_code,
                "matched_rule_ids": decision.matched_rule_ids,
                "rule_versions": decision.rule_versions,
                "system_deny": decision.system_deny,
                "request_fingerprint": decision.request_fingerprint,
                "message": redact_secrets(decision.message),
            },
        )
        try:
            append_res = self._journal.append(audit_ev)
        except Exception as exc:
            raise GovernanceAuditError(
                f"Mandatory governance audit failed; blocking action '{request.action}': {exc}"
            ) from exc

        if append_res.status == "REJECTED" or append_res.entry is None:
            msg = append_res.error.message if append_res.error else "Audit append rejected."
            raise GovernanceAuditError(
                f"Mandatory governance audit rejected; blocking action '{request.action}': {msg}"
            )
        return decision.model_copy(update={"audit_event_id": audit_ev.event_id})

    def dispatch_protected_action(
        self,
        request: PolicyRequest,
        handler: Callable[[], T],
    ) -> tuple[PolicyDecision, T]:
        """Evaluate policy, consume single-use approval if required, and run ``handler``."""
        decision = self.evaluate_and_audit(request)
        if decision.decision == "DENY" or decision.decision == "ERROR":
            raise PermissionDeniedError(decision)

        if decision.decision == "APPROVAL_REQUIRED":
            if self._approvals is None or request.approval_id is None:
                raise PermissionDeniedError(
                    decision.model_copy(
                        update={
                            "message": (
                                f"{decision.message} Supply a valid approved approval_id before execution."
                            )
                        }
                    )
                )
            _, result = self._approvals.verify_and_consume(
                request.approval_id,
                expected_action=request.action,
                expected_resource=request.resource,
                expected_task_id=request.task_id,
                expected_run_id=request.run_id,
                expected_fingerprint=decision.request_fingerprint,
                timestamp=request.timestamp,
                system_deny=decision.system_deny,
                action_fn=handler,
                correlation_id=request.correlation_id,
            )
            return decision, result

        return decision, handler()

    def build_lifecycle_hook(
        self,
        auth_context: TrustedAuthContext,
    ) -> Callable[[str, str, str | None, str | None, datetime, str | None], tuple[bool, str]]:
        """Create a permission hook compatible with ``WorkflowLifecycleController``."""

        def _hook(
            action: str,
            actor_id: str,
            task_id: str | None,
            run_id: str | None,
            timestamp: datetime,
            resource: str | None,
        ) -> tuple[bool, str]:
            # Enforce exact caller identity match with bound trusted context.
            if actor_id != auth_context.actor_id:
                return False, f"Actor mismatch: '{actor_id}' does not match trusted context '{auth_context.actor_id}'."
            req = PolicyRequest(
                auth_context=auth_context,
                action=action,
                resource=resource or f"task:{task_id or 'none'}",
                task_id=task_id,
                run_id=run_id,
                timestamp=timestamp,
            )
            try:
                dec = self.evaluate_and_audit(req)
            except GovernanceAuditError as exc:
                return False, str(exc)
            if dec.decision != "ALLOW":
                return False, f"{dec.reason_code}: {dec.message}"
            return True, dec.message

        return _hook
