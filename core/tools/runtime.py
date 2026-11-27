"""Governed 10-step Tool Runtime implementing schema validation, PermissionManager, Approval Gate, and audit."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError

from core.contracts.event import Event, EventType
from core.contracts.identifiers import new_event_id, sanitize_payload
from core.events.journal import EventJournal
from core.governance.approvals import ApprovalError
from core.governance.evaluator import compute_request_fingerprint
from core.governance.manager import PermissionManager
from core.governance.models import PolicyDecision, PolicyRequest
from core.tools.models import (
    ToolCancelledError,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExecutionRecord,
    ToolHandler,
    ToolHandlerOutput,
    ToolInvocationRequest,
    ToolManifest,
    ToolRepository,
    ToolResult,
    ToolResultStatus,
    ToolUsageBucket,
    ToolUsageSummary,
    new_tool_execution_id,
)
from core.tools.registry import ToolRegistry
from engines.antigravity.diagnostics import redact_secrets

logger = logging.getLogger(__name__)

_PATH_PARAM_KEYS: tuple[str, ...] = (
    "path",
    "output_path",
    "screenshot_path",
    "script_path",
    "test_path",
)


def _derive_target_path(manifest: ToolManifest, request: ToolInvocationRequest) -> str | None:
    """Resolve candidate workspace path for Governance check before running handler."""
    if request.target_path is not None:
        return request.target_path
    for key in _PATH_PARAM_KEYS:
        val = request.parameters.get(key)
        if isinstance(val, str) and val.strip():
            return val
    if manifest.id in {"tools.files.list", "tools.code.git_inspect"}:
        return "."
    if manifest.id == "tools.code.run_sandbox" and request.workspace_root is not None:
        return "."
    return None


def _event_actor_id(request: ToolInvocationRequest) -> str | None:
    """Only emit AgentId onto Event.actor_id when formatted `<dept>.<role>`."""
    actor = request.auth_context.actor_id
    if request.auth_context.actor_kind in {"worker_agent", "master_agent"} and "." in actor:
        return actor
    return None


def _extract_artifact_ids(artifacts: list[dict[str, Any]]) -> list[str]:
    """Collect string artifact identifiers or relative path references cleanly."""
    refs: list[str] = []
    for item in artifacts:
        art_id = item.get("artifact_id") or item.get("path")
        if isinstance(art_id, str) and art_id.strip():
            refs.append(art_id)
    return refs


class ToolRuntime:
    """Executes registered tools strictly through the 10-step Fail-Closed lifecycle."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        permission_manager: PermissionManager,
        journal: EventJournal,
        tool_repo: ToolRepository | None = None,
    ) -> None:
        """Compose ToolRegistry, Governance PermissionManager, EventJournal, and ToolRepository."""
        self._registry = registry
        self._permissions = permission_manager
        self._journal = journal
        self._repo = tool_repo
        self._records: list[ToolExecutionRecord] = []

    def invoke(
        self,
        request: ToolInvocationRequest,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ToolResult:
        """Execute one governed tool invocation following all 10 lifecycle steps."""
        execution_id = new_tool_execution_id()
        started = perf_counter()
        sanitized_input = sanitize_payload(request.parameters)
        if not isinstance(sanitized_input, dict):
            sanitized_input = {}
        corr_id = request.correlation_id or f"tex-{execution_id}"

        def _finalize(
            *,
            status: ToolResultStatus,
            action: str,
            data: dict[str, Any] | None = None,
            artifacts: list[dict[str, Any]] | None = None,
            evidence_refs: list[str] | None = None,
            untrusted_content: bool = False,
            estimated_cost_usd: float | None = None,
            error_code: str | None = None,
            error_message: str | None = None,
            audit_event_id: str | None = None,
            approval_id: str | None = None,
            event_type: EventType | None = None,
        ) -> ToolResult:
            duration_ms = max(0, int((perf_counter() - started) * 1000))
            clean_data = data or {}
            clean_artifacts = artifacts or []
            clean_evidence = evidence_refs or []
            clean_err = redact_secrets(error_message)[:500] if error_message else None

            record = ToolExecutionRecord(
                execution_id=execution_id,
                tool_id=request.tool_id,
                task_id=request.task_id,
                run_id=request.run_id,
                session_id=request.session_id,
                actor_id=request.auth_context.actor_id,
                action=action,
                status=status,
                business_verification_status="UNVERIFIED",
                duration_ms=duration_ms,
                estimated_cost_usd=estimated_cost_usd,
                approval_id=approval_id,
                audit_event_id=audit_event_id,
                redacted_input=sanitized_input,
                redacted_output=clean_data,
                artifact_ids=_extract_artifact_ids(clean_artifacts),
                evidence_refs=clean_evidence,
                error_code=error_code,
                error_message=clean_err,
                executed_at=request.timestamp,
            )
            self._persist_record(record)
            if event_type is not None:
                self._emit_event(
                    request=request,
                    corr_id=corr_id,
                    event_type=event_type,
                    payload={
                        "execution_id": execution_id,
                        "tool_id": request.tool_id,
                        "actor_id": request.auth_context.actor_id,
                        "status": status,
                        "action": action,
                        "duration_ms": duration_ms,
                        "error_code": error_code,
                        "business_verification_status": "UNVERIFIED",
                        "untrusted_content": untrusted_content,
                        "redacted_input": record.redacted_input,
                    },
                )
            return ToolResult(
                execution_id=execution_id,
                tool_id=request.tool_id,
                status=status,
                data=clean_data,
                artifacts=clean_artifacts,
                evidence_refs=clean_evidence,
                business_verification_status="UNVERIFIED",
                untrusted_content=untrusted_content,
                duration_ms=duration_ms,
                estimated_cost_usd=estimated_cost_usd,
                error_code=error_code,
                error_message=clean_err,
                audit_event_id=audit_event_id,
                approval_id=approval_id,
            )

        # Step 1: Registry lookup & availability gate.
        manifest = self._registry.get(request.tool_id)
        handler = self._registry.get_handler(request.tool_id)
        if manifest is None or handler is None:
            return _finalize(
                status="DENIED",
                action="tool.invoke",
                error_code="TOOL_NOT_FOUND",
                error_message=f"Tool '{request.tool_id}' is not registered or has no bound handler.",
                event_type="tool.call_denied",
            )
        if manifest.status != "AVAILABLE":
            return _finalize(
                status="DENIED",
                action="tool.invoke",
                error_code="TOOL_UNAVAILABLE",
                error_message=f"Tool '{manifest.id}' is currently '{manifest.status}', not AVAILABLE.",
                event_type="tool.call_denied",
            )

        # Step 2: Validate input_schema before Governance/execution.
        try:
            Draft202012Validator(manifest.input_schema).validate(request.parameters)
        except JsonSchemaValidationError as exc:
            return _finalize(
                status="FAILED",
                action="tool.invoke",
                error_code="INVALID_INPUT_SCHEMA",
                error_message=f"Input validation failed for '{manifest.id}': {exc.message}",
                event_type="tool.call_failed",
            )

        # Resolve workspace scope path and canonical resource string.
        derived_target_path = _derive_target_path(manifest, request)
        resource = f"tool:{manifest.id}"
        if derived_target_path is not None and request.workspace_root is not None:
            norm_path = str(Path(derived_target_path)).replace("\\", "/")
            resource = f"tool:{manifest.id}:{norm_path}"

        # Step 3: Evaluate `tool.invoke` against allowlist, tool registry, and workspace bounds.
        invoke_req = PolicyRequest(
            auth_context=request.auth_context,
            action="tool.invoke",
            resource=resource,
            tool_id=manifest.id,
            task_id=request.task_id,
            run_id=request.run_id,
            workspace_root=request.workspace_root,
            target_path=derived_target_path,
            cross_task_roots=request.cross_task_roots,
            parameters=sanitized_input,
            approval_id=request.approval_id,
            correlation_id=corr_id,
            timestamp=request.timestamp,
        )
        invoke_decision = self._permissions.evaluate_and_audit(invoke_req)
        if invoke_decision.decision in {"DENY", "ERROR"}:
            return _finalize(
                status="DENIED",
                action="tool.invoke",
                error_code=invoke_decision.reason_code,
                error_message=invoke_decision.message,
                audit_event_id=invoke_decision.audit_event_id,
                event_type="tool.call_denied",
            )

        # Step 4: Evaluate all required Action IDs in manifest.permissions_required.
        perm_decisions: list[tuple[str, PolicyDecision]] = []
        approval_trigger: tuple[str, PolicyDecision] | None = None
        for perm_action in manifest.permissions_required:
            perm_req = PolicyRequest(
                auth_context=request.auth_context,
                action=perm_action,
                resource=resource,
                tool_id=manifest.id,
                task_id=request.task_id,
                run_id=request.run_id,
                workspace_root=request.workspace_root,
                target_path=derived_target_path,
                cross_task_roots=request.cross_task_roots,
                parameters=sanitized_input,
                approval_id=request.approval_id,
                correlation_id=corr_id,
                timestamp=request.timestamp,
            )
            decision = self._permissions.evaluate_and_audit(perm_req)
            perm_decisions.append((perm_action, decision))
            if decision.decision in {"DENY", "ERROR"}:
                return _finalize(
                    status="DENIED",
                    action=perm_action,
                    error_code=decision.reason_code,
                    error_message=decision.message,
                    audit_event_id=decision.audit_event_id,
                    event_type="tool.call_denied",
                )
            if decision.decision == "APPROVAL_REQUIRED" and approval_trigger is None:
                approval_trigger = (perm_action, decision)

        last_audit_id = perm_decisions[-1][1].audit_event_id if perm_decisions else invoke_decision.audit_event_id
        needs_approval = manifest.approval_required or (approval_trigger is not None) or (
            invoke_decision.decision == "APPROVAL_REQUIRED"
        )
        if approval_trigger is None and needs_approval:
            approval_trigger = ("tool.invoke", invoke_decision)

        # Step 5 (if approval required): enforce single-use Approval Gate before running handler.
        exec_ctx = ToolExecutionContext(
            execution_id=execution_id,
            tool_id=manifest.id,
            actor_id=request.auth_context.actor_id,
            actor_kind=request.auth_context.actor_kind,
            task_id=request.task_id,
            run_id=request.run_id,
            session_id=request.session_id,
            workspace_root=request.workspace_root,
            cross_task_roots=request.cross_task_roots,
            timeout_seconds=manifest.timeout_seconds,
            approved_by_operator=False,
            approval_id=request.approval_id,
        )

        if is_cancelled is not None and is_cancelled():
            return _finalize(
                status="CANCELLED",
                action=manifest.permissions_required[0],
                error_code="CANCELLED",
                error_message=f"Tool '{manifest.id}' execution cancelled prior to launch.",
                audit_event_id=last_audit_id,
                event_type="tool.call_failed",
            )

        if needs_approval and approval_trigger is not None:
            approval_action, trigger_decision = approval_trigger
            fingerprint = compute_request_fingerprint(
                action=approval_action,
                resource=resource,
                task_id=request.task_id,
                run_id=request.run_id,
                parameters=sanitized_input,
            )
            approval_svc = self._permissions.approval_service
            if request.approval_id is None or approval_svc is None:
                return _finalize(
                    status="APPROVAL_REQUIRED",
                    action=approval_action,
                    data={
                        "approval_action": approval_action,
                        "resource": resource,
                        "request_fingerprint": fingerprint,
                    },
                    error_code="APPROVAL_REQUIRED",
                    error_message=(
                        f"Tool '{manifest.id}' action '{approval_action}' requires human approval."
                    ),
                    audit_event_id=trigger_decision.audit_event_id,
                    event_type="tool.call_denied",
                )

            # Emit proposed event before consuming single-use approval and executing.
            self._emit_proposed(request=request, corr_id=corr_id, execution_id=execution_id, manifest=manifest, sanitized_input=sanitized_input)
            approved_ctx = exec_ctx.model_copy(update={"approved_by_operator": True})
            try:
                _, outcome_or_err = approval_svc.verify_and_consume(
                    request.approval_id,
                    expected_action=approval_action,
                    expected_resource=resource,
                    expected_task_id=request.task_id,
                    expected_run_id=request.run_id,
                    expected_fingerprint=fingerprint,
                    timestamp=request.timestamp,
                    system_deny=trigger_decision.system_deny,
                    action_fn=lambda: self._run_handler(
                        manifest,
                        handler,
                        request.parameters,
                        approved_ctx,
                        is_cancelled=is_cancelled,
                    ),
                    correlation_id=corr_id,
                )
            except ApprovalError as exc:
                return _finalize(
                    status="DENIED",
                    action=approval_action,
                    error_code=exc.error_code,
                    error_message=exc.message,
                    audit_event_id=trigger_decision.audit_event_id,
                    approval_id=request.approval_id,
                    event_type="tool.call_denied",
                )
            status, handler_output, err_code, err_msg = outcome_or_err
            return self._complete_execution(
                _finalize=_finalize,
                manifest=manifest,
                action=approval_action,
                status=status,
                handler_output=handler_output,
                err_code=err_code,
                err_msg=err_msg,
                audit_event_id=trigger_decision.audit_event_id,
                approval_id=request.approval_id,
            )

        # Standard approved path (no extra human approval required).
        self._emit_proposed(
            request=request,
            corr_id=corr_id,
            execution_id=execution_id,
            manifest=manifest,
            sanitized_input=sanitized_input,
        )
        status, handler_output, err_code, err_msg = self._run_handler(
            manifest,
            handler,
            request.parameters,
            exec_ctx,
            is_cancelled=is_cancelled,
        )
        primary_action = manifest.permissions_required[0]
        return self._complete_execution(
            _finalize=_finalize,
            manifest=manifest,
            action=primary_action,
            status=status,
            handler_output=handler_output,
            err_code=err_code,
            err_msg=err_msg,
            audit_event_id=last_audit_id,
            approval_id=None,
        )

    def _run_handler(
        self,
        manifest: ToolManifest,
        handler: ToolHandler,
        parameters: dict[str, Any],
        ctx: ToolExecutionContext,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[ToolResultStatus, ToolHandlerOutput | None, str | None, str | None]:
        """Invoke handler safely catching cancellation, timeouts, and tool errors cleanly."""
        try:
            if is_cancelled is not None and is_cancelled():
                raise ToolCancelledError(f"Tool '{manifest.id}' cancelled prior to handler invocation.")
            output = handler(dict(parameters), ctx)
            if is_cancelled is not None and is_cancelled():
                raise ToolCancelledError(f"Tool '{manifest.id}' cancelled before committing output.")
            return ("SUCCESS", output, None, None)
        except (ToolCancelledError, asyncio.CancelledError) as exc:
            return (
                "CANCELLED",
                None,
                "CANCELLED",
                str(exc) or f"Tool '{manifest.id}' execution cancelled.",
            )
        except TimeoutError as exc:
            return ("TIMEOUT", None, "TIMED_OUT", str(exc) or f"Tool '{manifest.id}' timed out.")
        except ToolExecutionError as exc:
            err_status: ToolResultStatus = "TIMEOUT" if exc.error_code == "TIMED_OUT" else "FAILED"
            return (err_status, None, exc.error_code, exc.message)
        except Exception as exc:
            logger.exception("Unexpected error in tool '%s': %s", manifest.id, redact_secrets(str(exc)))
            return ("FAILED", None, "TOOL_EXECUTION_ERROR", f"{type(exc).__name__}: {exc}")

    def _complete_execution(
        self,
        *,
        _finalize: Any,
        manifest: ToolManifest,
        action: str,
        status: ToolResultStatus,
        handler_output: ToolHandlerOutput | None,
        err_code: str | None,
        err_msg: str | None,
        audit_event_id: str | None,
        approval_id: str | None,
    ) -> ToolResult:
        """Steps 7-10: validate output_schema, sanitize telemetry/cost, record & emit completion."""
        if status != "SUCCESS" or handler_output is None:
            return _finalize(
                status=status,
                action=action,
                error_code=err_code,
                error_message=err_msg,
                audit_event_id=audit_event_id,
                approval_id=approval_id,
                event_type="tool.call_failed",
            )

        try:
            Draft202012Validator(manifest.output_schema).validate(handler_output.data)
        except JsonSchemaValidationError as exc:
            return _finalize(
                status="FAILED",
                action=action,
                error_code="OUTPUT_INVALID",
                error_message=f"Tool '{manifest.id}' output failed output_schema validation: {exc.message}",
                audit_event_id=audit_event_id,
                approval_id=approval_id,
                event_type="tool.call_failed",
            )

        cost = (
            handler_output.estimated_cost_usd
            if handler_output.estimated_cost_usd is not None
            else manifest.cost_profile.estimated_unit_cost_usd
        )
        return _finalize(
            status="SUCCESS",
            action=action,
            data=handler_output.data,
            artifacts=handler_output.artifacts,
            evidence_refs=handler_output.evidence_refs,
            untrusted_content=handler_output.untrusted_content,
            estimated_cost_usd=cost,
            audit_event_id=audit_event_id,
            approval_id=approval_id,
            event_type="tool.call_completed",
        )

    def _emit_proposed(
        self,
        *,
        request: ToolInvocationRequest,
        corr_id: str,
        execution_id: str,
        manifest: ToolManifest,
        sanitized_input: dict[str, Any],
    ) -> None:
        self._emit_event(
            request=request,
            corr_id=corr_id,
            event_type="tool.call_proposed",
            payload={
                "execution_id": execution_id,
                "tool_id": manifest.id,
                "version": manifest.version,
                "actor_id": request.auth_context.actor_id,
                "risk_level": manifest.risk_level,
                "execution_mode": manifest.execution_mode,
                "redacted_input": sanitized_input,
            },
        )

    def _emit_event(
        self,
        *,
        request: ToolInvocationRequest,
        corr_id: str,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> None:
        ev = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type=event_type,
            source="tool_runtime",
            task_id=request.task_id,
            run_id=request.run_id,
            session_id=request.session_id,
            actor_id=_event_actor_id(request),
            correlation_id=corr_id,
            redacted_payload=payload,
        )
        res = self._journal.append(ev)
        if res.status == "REJECTED":
            logger.warning("Failed to append %s event: %s", event_type, res.error)

    def _persist_record(self, record: ToolExecutionRecord) -> None:
        self._records.append(record)
        if self._repo is not None:
            self._repo.record_execution(record)

    def summarize_usage(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        actor_id: str | None = None,
    ) -> ToolUsageSummary:
        """Aggregate tool usage statistics per Task, Run, or Actor with null-safe cost reporting."""
        if self._repo is not None:
            return self._repo.summarize_usage(task_id=task_id, run_id=run_id, actor_id=actor_id)
        filtered = [
            rec
            for rec in self._records
            if (task_id is None or rec.task_id == task_id)
            and (run_id is None or rec.run_id == run_id)
            and (actor_id is None or rec.actor_id == actor_id)
        ]
        return aggregate_tool_usage(filtered, task_id=task_id, run_id=run_id, actor_id=actor_id)


def aggregate_tool_usage(
    records: list[ToolExecutionRecord],
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    actor_id: str | None = None,
) -> ToolUsageSummary:
    """Compute canonical ToolUsageSummary over an ordered sequence of ToolExecutionRecords."""
    by_status: dict[str, int] = defaultdict(int)
    per_tool: dict[str, list[ToolExecutionRecord]] = defaultdict(list)
    total_duration = 0
    known_cost_total: float | None = None
    unknown_cost_calls = 0

    for rec in records:
        by_status[rec.status] += 1
        per_tool[rec.tool_id].append(rec)
        total_duration += rec.duration_ms
        if rec.estimated_cost_usd is None:
            unknown_cost_calls += 1
        else:
            known_cost_total = (known_cost_total or 0.0) + rec.estimated_cost_usd

    buckets: list[ToolUsageBucket] = []
    for tid in sorted(per_tool):
        group = per_tool[tid]
        t_known: float | None = None
        t_unknown = 0
        for rec in group:
            if rec.estimated_cost_usd is None:
                t_unknown += 1
            else:
                t_known = (t_known or 0.0) + rec.estimated_cost_usd
        buckets.append(
            ToolUsageBucket(
                tool_id=tid,
                call_count=len(group),
                success_count=sum(1 for r in group if r.status == "SUCCESS"),
                failed_count=sum(1 for r in group if r.status == "FAILED"),
                denied_count=sum(1 for r in group if r.status == "DENIED"),
                approval_required_count=sum(1 for r in group if r.status == "APPROVAL_REQUIRED"),
                timeout_count=sum(1 for r in group if r.status == "TIMEOUT"),
                total_duration_ms=sum(r.duration_ms for r in group),
                known_cost_usd=round(t_known, 6) if t_known is not None else None,
                unknown_cost_calls=t_unknown,
            )
        )

    return ToolUsageSummary(
        task_id=task_id,
        run_id=run_id,
        actor_id=actor_id,
        total_calls=len(records),
        by_status=dict(sorted(by_status.items())),
        total_duration_ms=total_duration,
        known_cost_usd=round(known_cost_total, 6) if known_cost_total is not None else None,
        unknown_cost_calls=unknown_cost_calls,
        by_tool=buckets,
    )
