"""Guarded, immutable state machine transitions and in-memory Event emission."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import ValidationError

from core.contracts.event import Event, EventSource, EventType, sanitize_event_payload
from core.contracts.identifiers import (
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_payload,
)
from core.contracts.run import Run, RunStatus
from core.contracts.task import Task, TaskStatus
from core.runtime.contract import AgentRuntimeResult
from core.tasks.guards import (
    evaluate_run_transition_guards,
    evaluate_task_transition_guards,
)
from core.tasks.transitions import (
    TERMINAL_TASK_STATUSES,
    ActorTrustKind,
    PairedTransitionResult,
    QaVerificationEvidence,
    RunTransitionRequest,
    RunTransitionResult,
    TaskTransitionRequest,
    TaskTransitionResult,
    TransitionContext,
    TransitionError,
)
from engines.verification import EngineArtifactVerificationResult

_ACTOR_KIND_TO_EVENT_SOURCE: dict[ActorTrustKind, EventSource] = {
    "core_runtime": "core_runtime",
    "orchestrator": "orchestrator",
    "qa_verifier": "qa_verifier",
    "operator": "operator",
    "governance": "governance",
    "worker_agent": "agent_runtime",
    "llm_self_report": "provider",
}

_RUN_TRANSITION_EVENT_MAP: dict[RunStatus, EventType] = {
    "SUCCEEDED": "run.completed",
    "FAILED": "run.failed",
    "CANCELLED": "run.cancelled",
    "TIMED_OUT": "run.timed_out",
}


def _safe_aware_timestamp(ts: datetime) -> datetime | None:
    """Return ``ts`` only if timezone-aware, otherwise None for error responses."""
    return ts if (ts.tzinfo is not None and ts.utcoffset() is not None) else None


def _build_transition_payload(
    *,
    previous_status: str,
    new_status: str,
    context: TransitionContext,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build sanitized transition metadata payload without mutating caller inputs."""
    merged: dict[str, Any] = dict(context.payload)
    merged["previous_status"] = previous_status
    merged["new_status"] = new_status
    merged["actor_kind"] = context.actor_kind
    merged["reason"] = context.reason
    if extra:
        merged.update(extra)
    return sanitize_event_payload(merged)


def transition_task(request: TaskTransitionRequest) -> TaskTransitionResult:
    """Evaluate guards and return a new Task state plus in-memory Event if accepted."""
    task = request.task
    context = request.context
    decisions, error = evaluate_task_transition_guards(task, request.target_status, context)
    if error is not None:
        return TaskTransitionResult(
            entity_id=task.task_id,
            previous_status=task.status,
            requested_status=request.target_status,
            new_status=task.status,
            accepted=False,
            timestamp=_safe_aware_timestamp(context.timestamp),
            guard_decisions=decisions,
            task=None,
            event=None,
            error=error,
        )

    target_status: TaskStatus = request.target_status  # type: ignore[assignment]
    assigned_agents = (
        list(context.assigned_agent_ids)
        if target_status == "ASSIGNED" and context.assigned_agent_ids
        else list(task.assigned_agent_ids)
    )

    if target_status in TERMINAL_TASK_STATUSES:
        next_active_run_id = None
    elif target_status == "RUNNING" and context.run is not None:
        next_active_run_id = context.run.run_id
    else:
        next_active_run_id = task.active_run_id

    next_artifact_ids = list(task.artifact_ids)
    if target_status == "COMPLETED" and context.qa_evidence is not None:
        for art_id in context.qa_evidence.artifact_ids:
            if art_id not in next_artifact_ids:
                next_artifact_ids.append(art_id)

    # Preserve created_at=None if not yet created instead of fabricating an initial creation time.
    next_updated_at = context.timestamp if task.created_at is not None else None

    updated_data = task.model_dump()
    updated_data.update(
        {
            "status": target_status,
            "assigned_agent_ids": assigned_agents,
            "active_run_id": next_active_run_id,
            "artifact_ids": next_artifact_ids,
            "updated_at": next_updated_at,
        }
    )

    try:
        new_task = Task.model_validate(updated_data)
    except ValidationError as exc:
        err = TransitionError(
            error_code="CONTRACT_VALIDATION_ERROR",
            guard_name="contract_validation",
            message=f"Updated Task violated Core Contract validation: {exc}",
        )
        return TaskTransitionResult(
            entity_id=task.task_id,
            previous_status=task.status,
            requested_status=request.target_status,
            new_status=task.status,
            accepted=False,
            timestamp=context.timestamp,
            guard_decisions=decisions,
            task=None,
            event=None,
            error=err,
        )

    evidence_id = (
        context.qa_evidence.evidence_id
        if context.qa_evidence is not None
        else (context.execution_evidence.evidence_id if context.execution_evidence else None)
    )
    run_id = context.run.run_id if context.run is not None else task.active_run_id
    artifact_id = (
        context.qa_evidence.artifact_ids[0]
        if (target_status == "COMPLETED" and context.qa_evidence and context.qa_evidence.artifact_ids)
        else None
    )
    payload_extra = (
        {"updated_at_omitted": "task.created_at was None"}
        if next_updated_at is None
        else None
    )
    event = Event(
        timestamp=context.timestamp,
        event_type="task.status_changed",
        source=_ACTOR_KIND_TO_EVENT_SOURCE[context.actor_kind],
        task_id=new_task.task_id,
        run_id=run_id,
        actor_id=context.actor_id,
        provider_id=context.run.provider_id if context.run is not None else None,
        connection_id=context.run.connection_id if context.run is not None else None,
        model_id=context.run.model_id if context.run is not None else None,
        artifact_id=artifact_id,
        evidence_id=evidence_id,
        correlation_id=context.correlation_id or (context.run.correlation_id if context.run else None),
        redacted_payload=_build_transition_payload(
            previous_status=task.status,
            new_status=new_task.status,
            context=context,
            extra=payload_extra,
        ),
    )
    return TaskTransitionResult(
        entity_id=new_task.task_id,
        previous_status=task.status,
        requested_status=request.target_status,
        new_status=new_task.status,
        accepted=True,
        timestamp=context.timestamp,
        guard_decisions=decisions,
        task=new_task,
        event=event,
        error=None,
    )


def transition_run(request: RunTransitionRequest) -> RunTransitionResult:
    """Evaluate guards and return a new Run state plus canonical Event when defined."""
    run = request.run
    context = request.context
    decisions, error = evaluate_run_transition_guards(run, request.target_status, context)
    if error is not None:
        return RunTransitionResult(
            entity_id=run.run_id,
            task_id=run.task_id,
            previous_status=run.status,
            requested_status=request.target_status,
            new_status=run.status,
            accepted=False,
            timestamp=_safe_aware_timestamp(context.timestamp),
            guard_decisions=decisions,
            run=None,
            event=None,
            error=error,
        )

    target_status: RunStatus = request.target_status  # type: ignore[assignment]
    updated_data = run.model_dump()
    updated_data["status"] = target_status

    if target_status == "RUNNING":
        if run.status == "PENDING":
            updated_data["started_at"] = context.timestamp
        updated_data["finished_at"] = None
    elif target_status == "SUCCEEDED":
        updated_data["finished_at"] = context.timestamp
        updated_data["error_reference"] = None
    elif target_status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
        if run.started_at is None:
            updated_data["started_at"] = context.timestamp
        updated_data["finished_at"] = context.timestamp
        if context.execution_evidence is not None and context.execution_evidence.error_reference is not None:
            updated_data["error_reference"] = context.execution_evidence.error_reference.model_dump()

    if context.execution_evidence is not None and context.execution_evidence.telemetry is not None:
        updated_data["telemetry"] = context.execution_evidence.telemetry.model_dump()

    try:
        new_run = Run.model_validate(updated_data)
    except ValidationError as exc:
        err = TransitionError(
            error_code="CONTRACT_VALIDATION_ERROR",
            guard_name="contract_validation",
            message=f"Updated Run violated Core Contract validation: {exc}",
        )
        return RunTransitionResult(
            entity_id=run.run_id,
            task_id=run.task_id,
            previous_status=run.status,
            requested_status=request.target_status,
            new_status=run.status,
            accepted=False,
            timestamp=context.timestamp,
            guard_decisions=decisions,
            run=None,
            event=None,
            error=err,
        )

    event_type: EventType | None = None
    if run.status == "PENDING" and target_status == "RUNNING":
        event_type = "run.started"
    else:
        event_type = _RUN_TRANSITION_EVENT_MAP.get(target_status)

    event: Event | None = None
    omitted_reason: str | None = None
    if event_type is not None:
        evidence_id = context.execution_evidence.evidence_id if context.execution_evidence else None
        event = Event(
            timestamp=context.timestamp,
            event_type=event_type,
            source=_ACTOR_KIND_TO_EVENT_SOURCE[context.actor_kind],
            task_id=new_run.task_id,
            run_id=new_run.run_id,
            session_id=new_run.session_ids[0] if len(new_run.session_ids) == 1 else None,
            actor_id=context.actor_id,
            provider_id=new_run.provider_id,
            connection_id=new_run.connection_id,
            model_id=new_run.model_id,
            evidence_id=evidence_id,
            correlation_id=context.correlation_id or new_run.correlation_id,
            redacted_payload=_build_transition_payload(
                previous_status=run.status,
                new_status=new_run.status,
                context=context,
            ),
        )
    else:
        omitted_reason = (
            f"Run transition {run.status} -> {new_run.status} has no run.status_changed "
            "EventType in P04.01 Event contract; deferred to P04.03 Event Journal extension."
        )

    return RunTransitionResult(
        entity_id=new_run.run_id,
        task_id=new_run.task_id,
        previous_status=run.status,
        requested_status=request.target_status,
        new_status=new_run.status,
        accepted=True,
        timestamp=context.timestamp,
        guard_decisions=decisions,
        run=new_run,
        event=event,
        event_omitted_reason=omitted_reason,
        error=None,
    )


def _pair_rejection(
    *,
    run_res: RunTransitionResult | None = None,
    task_res: TaskTransitionResult | None = None,
    error: TransitionError,
) -> PairedTransitionResult:
    """Return an atomic fail-closed PairedTransitionResult without mutating Task or Run."""
    return PairedTransitionResult(
        accepted=False,
        task_result=task_res,
        run_result=run_res,
        task=None,
        run=None,
        events=[],
        error=error,
    )


def _preflight_pair_link(task: Task, run: Run) -> TransitionError | None:
    """Check Task/Run ownership before performing paired transitions."""
    if run.task_id != task.task_id:
        return TransitionError(
            error_code="WRONG_TASK_RUN_RELATIONSHIP",
            guard_name="paired_identity_guard",
            message=f"Run '{run.run_id}' belongs to task '{run.task_id}', not '{task.task_id}'.",
        )
    if task.active_run_id is not None and task.active_run_id != run.run_id:
        return TransitionError(
            error_code="WRONG_TASK_RUN_RELATIONSHIP",
            guard_name="paired_identity_guard",
            message=f"Task '{task.task_id}' already tracks active_run_id '{task.active_run_id}', not '{run.run_id}'.",
        )
    return None


def start_task_run(
    task: Task,
    run: Run,
    context: TransitionContext,
) -> PairedTransitionResult:
    """Start a PENDING Run and move its parent Task into RUNNING with active_run_id set."""
    if run.task_id != task.task_id:
        return _pair_rejection(
            error=TransitionError(
                error_code="WRONG_TASK_RUN_RELATIONSHIP",
                guard_name="paired_identity_guard",
                message=f"Run '{run.run_id}' belongs to task '{run.task_id}', not '{task.task_id}'.",
            )
        )

    run_res = transition_run(
        RunTransitionRequest(run=run, target_status="RUNNING", context=context)
    )
    if not run_res.accepted or run_res.run is None:
        assert run_res.error is not None
        return _pair_rejection(run_res=run_res, error=run_res.error)

    task_ctx = context.model_copy(update={"run": run_res.run})
    task_res = transition_task(
        TaskTransitionRequest(task=task, target_status="RUNNING", context=task_ctx)
    )
    if not task_res.accepted or task_res.task is None:
        assert task_res.error is not None
        return _pair_rejection(run_res=run_res, task_res=task_res, error=task_res.error)

    events = [ev for ev in (run_res.event, task_res.event) if ev is not None]
    return PairedTransitionResult(
        accepted=True,
        task_result=task_res,
        run_result=run_res,
        task=task_res.task,
        run=run_res.run,
        events=events,
    )


def complete_run_only(
    task: Task,
    run: Run,
    context: TransitionContext,
) -> PairedTransitionResult:
    """Finish a Run as SUCCEEDED while keeping Task non-terminal awaiting QA verification."""
    link_err = _preflight_pair_link(task, run)
    if link_err is not None:
        return _pair_rejection(error=link_err)
    if task.status in TERMINAL_TASK_STATUSES:
        return _pair_rejection(
            error=TransitionError(
                error_code="TERMINAL_STATE_MUTATION",
                guard_name="paired_terminal_guard",
                message=f"Terminal Task '{task.task_id}' ({task.status}) cannot accept Run completion.",
            )
        )

    try:
        validate_aware_datetime(context.timestamp, "context.timestamp")
        ensure_not_secret(context.reason, "context.reason")
        validate_safe_payload(context.payload, "context.payload")
    except ValueError as exc:
        code = "INVALID_TIMESTAMP" if context.timestamp.tzinfo is None else "UNSAFE_EVENT_PAYLOAD"
        return _pair_rejection(
            error=TransitionError(
                error_code=code,  # type: ignore[arg-type]
                guard_name="paired_context_guard",
                message=str(exc),
            )
        )

    if task.created_at is not None and context.timestamp < task.created_at:
        return _pair_rejection(
            error=TransitionError(
                error_code="INVALID_TIMESTAMP_ORDER",
                guard_name="paired_timestamp_guard",
                message="Transition timestamp precedes task.created_at.",
            )
        )
    if task.updated_at is not None and context.timestamp < task.updated_at:
        return _pair_rejection(
            error=TransitionError(
                error_code="INVALID_TIMESTAMP_ORDER",
                guard_name="paired_timestamp_guard",
                message="Transition timestamp precedes task.updated_at.",
            )
        )

    run_res = transition_run(
        RunTransitionRequest(run=run, target_status="SUCCEEDED", context=context)
    )
    if not run_res.accepted or run_res.run is None:
        assert run_res.error is not None
        return _pair_rejection(run_res=run_res, error=run_res.error)

    updated_data = task.model_dump()
    updated_data["active_run_id"] = None
    updated_data["updated_at"] = context.timestamp if task.created_at is not None else None
    new_task = Task.model_validate(updated_data)

    events = [run_res.event] if run_res.event is not None else []
    return PairedTransitionResult(
        accepted=True,
        task_result=None,
        run_result=run_res,
        task=new_task,
        run=run_res.run,
        events=events,
    )


def _end_task_and_run(
    task: Task,
    run: Run,
    context: TransitionContext,
    *,
    run_target: Literal["FAILED", "TIMED_OUT", "CANCELLED"],
    task_target: Literal["FAILED", "BLOCKED", "CANCELLED"],
) -> PairedTransitionResult:
    """Terminate a Run atomically with its parent Task without leaving a dangling active_run_id."""
    link_err = _preflight_pair_link(task, run)
    if link_err is not None:
        return _pair_rejection(error=link_err)

    run_res = transition_run(
        RunTransitionRequest(run=run, target_status=run_target, context=context)
    )
    if not run_res.accepted or run_res.run is None:
        assert run_res.error is not None
        return _pair_rejection(run_res=run_res, error=run_res.error)

    task_ctx = context.model_copy(update={"run": run_res.run})
    task_res = transition_task(
        TaskTransitionRequest(task=task, target_status=task_target, context=task_ctx)
    )
    if not task_res.accepted or task_res.task is None:
        assert task_res.error is not None
        return _pair_rejection(run_res=run_res, task_res=task_res, error=task_res.error)

    # Clear active_run_id cleanly when parent moves to BLOCKED after terminal Run failure/timeout.
    final_task = task_res.task
    if task_target == "BLOCKED" and final_task.active_run_id is not None:
        final_task = final_task.model_copy(update={"active_run_id": None})

    events = [ev for ev in (run_res.event, task_res.event) if ev is not None]
    return PairedTransitionResult(
        accepted=True,
        task_result=task_res.model_copy(update={"task": final_task}),
        run_result=run_res,
        task=final_task,
        run=run_res.run,
        events=events,
    )


def fail_task_run(
    task: Task,
    run: Run,
    context: TransitionContext,
    *,
    target_task_status: Literal["FAILED", "BLOCKED"] = "FAILED",
) -> PairedTransitionResult:
    """Record Run FAILED and update parent Task consistently to FAILED or BLOCKED."""
    return _end_task_and_run(
        task,
        run,
        context,
        run_target="FAILED",
        task_target=target_task_status,
    )


def timeout_task_run(
    task: Task,
    run: Run,
    context: TransitionContext,
    *,
    target_task_status: Literal["FAILED", "BLOCKED"] = "FAILED",
) -> PairedTransitionResult:
    """Record Run TIMED_OUT and update parent Task consistently to FAILED or BLOCKED."""
    return _end_task_and_run(
        task,
        run,
        context,
        run_target="TIMED_OUT",
        task_target=target_task_status,
    )


def cancel_task_run(
    task: Task,
    run: Run,
    context: TransitionContext,
) -> PairedTransitionResult:
    """Confirm Run cancellation and cancel parent Task atomically in memory."""
    return _end_task_and_run(
        task,
        run,
        context,
        run_target="CANCELLED",
        task_target="CANCELLED",
    )


def qa_evidence_from_engine_verification(
    verification: EngineArtifactVerificationResult,
    *,
    evidence_id: str,
    verifier_actor_id: str,
    verified_at: datetime,
    artifact_ids: Sequence[str] = (),
) -> QaVerificationEvidence:
    """Project an independent ``EngineArtifactVerificationResult`` into ``QaVerificationEvidence``."""
    return QaVerificationEvidence(
        evidence_id=evidence_id,
        verifier_source="engine_artifact_verifier",
        verifier_actor_id=verifier_actor_id,
        verification_status=verification.status,
        verified_at=verified_at,
        artifact_ids=list(artifact_ids),
        expected_sha256=verification.expected_sha256,
        actual_sha256=verification.actual_sha256,
        summary="; ".join(verification.failure_reasons) if verification.failure_reasons else None,
    )


def qa_evidence_from_runtime_result(
    result: AgentRuntimeResult,
    *,
    evidence_id: str,
    verifier_actor_id: str,
    verified_at: datetime,
    artifact_ids: Sequence[str] = (),
) -> QaVerificationEvidence:
    """Extract fail-closed ``QaVerificationEvidence`` from an ``AgentRuntimeResult``."""
    if result.qa_verification is None:
        raise ValueError(
            "AgentRuntimeResult lacks independent qa_verification; cannot build QaVerificationEvidence."
        )
    if result.qa_verification.status == "VERIFIED_PASS" and (
        result.status != "DELIVERED" or not result.business_verified
    ):
        raise ValueError(
            "AgentRuntimeResult is not DELIVERED with business_verified=True; refusing VERIFIED_PASS projection."
        )
    return qa_evidence_from_engine_verification(
        result.qa_verification,
        evidence_id=evidence_id,
        verifier_actor_id=verifier_actor_id,
        verified_at=verified_at,
        artifact_ids=artifact_ids,
    )
