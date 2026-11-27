"""Shared fail-closed Governance, Event Journal, Tool Runtime, and error helpers for core/departments/."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.contracts.event import Event, EventSource, EventType, sanitize_event_payload
from core.contracts.identifiers import new_event_id, validate_aware_datetime
from core.events.journal import EventJournal
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import PolicyDecision, PolicyRequest, TrustedAuthContext
from core.tools.models import ToolInvocationRequest, ToolResult
from engines.antigravity.diagnostics import redact_secrets


class DepartmentBehaviorError(RuntimeError):
    """Base fail-closed exception raised across Phase 09 department domain services."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)[:500]
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class DepartmentGovernanceError(DepartmentBehaviorError):
    """Raised when Governance policy or Approval Gate denies a department operation."""


@runtime_checkable
class ToolInvokerPort(Protocol):
    """Structural interface decoupled from concrete ``tools/`` matching ``ToolRuntime.invoke``."""

    def invoke(self, request: ToolInvocationRequest) -> ToolResult:
        """Invoke a governed registered tool under Policy and workspace isolation."""


def event_actor_id(auth_context: TrustedAuthContext | None) -> str | None:
    """Return canonical ``AgentId`` (`<dept>.<role>`) for Event attribution, or None."""
    if auth_context is None:
        return None
    if auth_context.actor_kind in {"worker_agent", "master_agent"} and "." in auth_context.actor_id:
        return auth_context.actor_id
    return None


def authorize_and_audit(
    permission_manager: PermissionManager,
    *,
    auth_context: TrustedAuthContext | None,
    action: str,
    resource: str,
    timestamp: datetime,
    task_id: str | None = None,
    run_id: str | None = None,
    workspace_root: str | None = None,
    target_path: str | None = None,
    cross_task_roots: Sequence[str] = (),
    approval_id: str | None = None,
    correlation_id: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> PolicyDecision:
    """Evaluate Governance policy, audit decision, and fail closed unless allowed."""
    validate_aware_datetime(timestamp, "timestamp")
    req = PolicyRequest(
        auth_context=auth_context,
        action=action,
        resource=resource,
        task_id=task_id,
        run_id=run_id,
        workspace_root=workspace_root,
        target_path=target_path,
        cross_task_roots=list(cross_task_roots),
        approval_id=approval_id,
        parameters=dict(parameters or {}),
        timestamp=timestamp,
        correlation_id=correlation_id or f"corr-dept-{action}",
    )
    try:
        decision = permission_manager.evaluate_and_audit(req)
    except PermissionDeniedError as exc:
        raise DepartmentGovernanceError(exc.decision.reason_code, exc.decision.message) from exc
    except GovernanceAuditError as exc:
        raise DepartmentGovernanceError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc

    if decision.decision != "ALLOW":
        raise DepartmentGovernanceError(decision.reason_code, decision.message)
    return decision


def append_department_event(
    journal: EventJournal,
    *,
    event_type: EventType,
    source: EventSource,
    timestamp: datetime,
    payload: dict[str, Any],
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    actor_id: str | None = None,
    provider_id: str | None = None,
    connection_id: str | None = None,
    model_id: str | None = None,
    artifact_id: str | None = None,
    evidence_id: str | None = None,
    correlation_id: str | None = None,
) -> Event:
    """Validate, redact secrets, and append an Event atomically to ``journal`` fail-closed."""
    validate_aware_datetime(timestamp, "timestamp")
    event = Event(
        event_id=new_event_id(),
        timestamp=timestamp,
        event_type=event_type,
        source=source,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        actor_id=actor_id,
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        artifact_id=artifact_id,
        evidence_id=evidence_id,
        correlation_id=correlation_id,
        redacted_payload=sanitize_event_payload(payload),
    )
    res = journal.append(event)
    if res.status == "REJECTED" or res.entry is None:
        msg = res.error.message if res.error else "Event append rejected."
        raise DepartmentGovernanceError("AUDIT_EVENT_REJECTED", msg)
    return event


def invoke_tool_checked(
    tool_invoker: ToolInvokerPort | None,
    *,
    auth_context: TrustedAuthContext,
    tool_id: str,
    parameters: dict[str, Any],
    timestamp: datetime,
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    workspace_root: str | None = None,
    target_path: str | None = None,
    approval_id: str | None = None,
    correlation_id: str | None = None,
) -> ToolResult:
    """Execute a registered tool via ``ToolInvokerPort`` or raise structured failure if unavailable."""
    if tool_invoker is None:
        raise DepartmentBehaviorError(
            "TOOL_RUNTIME_UNAVAILABLE",
            f"ToolInvokerPort is required to invoke '{tool_id}'.",
        )
    req = ToolInvocationRequest(
        auth_context=auth_context,
        tool_id=tool_id,
        parameters=parameters,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        workspace_root=workspace_root,
        target_path=target_path,
        approval_id=approval_id,
        correlation_id=correlation_id,
        timestamp=timestamp,
    )
    result = tool_invoker.invoke(req)
    if result.status != "SUCCESS":
        raise DepartmentBehaviorError(
            result.error_code or f"TOOL_{result.status}",
            result.error_message or f"Tool '{tool_id}' returned status '{result.status}'.",
        )
    return result
