"""Deterministic fail-closed transition guards for Task and Run state machines."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.contracts.identifiers import (
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_payload,
)
from core.contracts.run import Run, RunStatus
from core.contracts.task import Task, TaskStatus
from core.tasks.transitions import (
    RUN_ALLOWED_TRANSITIONS,
    TASK_ALLOWED_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    VALID_RUN_STATUSES,
    VALID_TASK_STATUSES,
    GuardDecision,
    TransitionContext,
    TransitionError,
    TransitionErrorCode,
)


def _fail(
    decisions: list[GuardDecision],
    guard_name: str,
    error_code: TransitionErrorCode,
    message: str,
    **details: Any,
) -> tuple[list[GuardDecision], TransitionError]:
    """Append a failing GuardDecision and return the structured TransitionError."""
    err = TransitionError(
        error_code=error_code,
        guard_name=guard_name,
        message=message,
        details=details,
    )
    decisions.append(GuardDecision(passed=False, guard_name=guard_name, error=err))
    return decisions, err


def _pass(decisions: list[GuardDecision], guard_name: str) -> None:
    """Append a passing GuardDecision record."""
    decisions.append(GuardDecision(passed=True, guard_name=guard_name))


def _check_timestamp_common(
    decisions: list[GuardDecision],
    timestamp: datetime,
    *,
    earlier_bounds: list[tuple[str, datetime | None]],
) -> TransitionError | None:
    """Reject naive timestamps and chronological regressions against existing record bounds."""
    try:
        validate_aware_datetime(timestamp, "context.timestamp")
    except ValueError as exc:
        return _fail(
            decisions,
            "timestamp_guard",
            "INVALID_TIMESTAMP",
            str(exc),
        )[1]

    for bound_name, bound_val in earlier_bounds:
        if bound_val is not None and timestamp < bound_val:
            return _fail(
                decisions,
                "timestamp_guard",
                "INVALID_TIMESTAMP_ORDER",
                f"Transition timestamp ({timestamp.isoformat()}) precedes {bound_name} ({bound_val.isoformat()}).",
                bound_name=bound_name,
            )[1]
    _pass(decisions, "timestamp_guard")
    return None


def _check_payload_and_reason(
    decisions: list[GuardDecision],
    context: TransitionContext,
) -> TransitionError | None:
    """Reject raw secrets or forbidden secret keys in payload, reason, or correlation_id."""
    try:
        ensure_not_secret(context.reason, "context.reason")
        if context.correlation_id is not None:
            ensure_not_secret(context.correlation_id, "context.correlation_id")
        validate_safe_payload(context.payload, "context.payload")
    except ValueError as exc:
        return _fail(
            decisions,
            "payload_safety_guard",
            "UNSAFE_EVENT_PAYLOAD",
            str(exc),
        )[1]
    _pass(decisions, "payload_safety_guard")
    return None


def evaluate_task_transition_guards(
    task: Task,
    target_status: str,
    context: TransitionContext,
) -> tuple[list[GuardDecision], TransitionError | None]:
    """Evaluate all fail-closed guards for a candidate Task status transition."""
    decisions: list[GuardDecision] = []

    err = _check_timestamp_common(
        decisions,
        context.timestamp,
        earlier_bounds=[("task.created_at", task.created_at), ("task.updated_at", task.updated_at)],
    )
    if err is not None:
        return decisions, err

    if context.qa_evidence is not None and task.created_at is not None:
        if context.qa_evidence.verified_at < task.created_at:
            return _fail(
                decisions,
                "timestamp_guard",
                "INVALID_TIMESTAMP_ORDER",
                "qa_evidence.verified_at precedes task.created_at.",
            )

    err = _check_payload_and_reason(decisions, context)
    if err is not None:
        return decisions, err

    if (
        context.expected_current_status is not None
        and context.expected_current_status != task.status
    ):
        return _fail(
            decisions,
            "stale_status_guard",
            "STALE_EXPECTED_STATUS",
            f"Expected current Task status '{context.expected_current_status}', found '{task.status}'.",
            expected=context.expected_current_status,
            actual=task.status,
        )
    _pass(decisions, "stale_status_guard")

    if target_status not in VALID_TASK_STATUSES:
        return _fail(
            decisions,
            "unknown_status_guard",
            "UNKNOWN_STATUS",
            f"Unknown Task status requested: {target_status!r}.",
            requested_status=target_status,
        )
    _pass(decisions, "unknown_status_guard")

    typed_target: TaskStatus = target_status  # type: ignore[assignment]

    if typed_target == task.status:
        return _fail(
            decisions,
            "duplicate_transition_guard",
            "DUPLICATE_TRANSITION",
            f"Task '{task.task_id}' is already in status '{task.status}'.",
            status=task.status,
        )
    _pass(decisions, "duplicate_transition_guard")

    if task.status in TERMINAL_TASK_STATUSES:
        return _fail(
            decisions,
            "terminal_state_guard",
            "TERMINAL_STATE_MUTATION",
            f"Terminal Task status '{task.status}' cannot transition to '{typed_target}'.",
            current_status=task.status,
            requested_status=typed_target,
        )
    _pass(decisions, "terminal_state_guard")

    if typed_target not in TASK_ALLOWED_TRANSITIONS[task.status]:
        return _fail(
            decisions,
            "transition_matrix_guard",
            "INVALID_TRANSITION",
            f"Task transition {task.status} -> {typed_target} is forbidden.",
            current_status=task.status,
            requested_status=typed_target,
        )
    _pass(decisions, "transition_matrix_guard")

    err = _check_task_identity_and_links(decisions, task, typed_target, context)
    if err is not None:
        return decisions, err

    if context.actor_kind == "llm_self_report":
        return _fail(
            decisions,
            "source_trust_guard",
            "UNTRUSTED_TRANSITION_SOURCE",
            "Unverified LLM self-report cannot mutate Task lifecycle state.",
            actor_id=context.actor_id,
        )
    if typed_target == "COMPLETED" and context.actor_kind == "worker_agent":
        return _fail(
            decisions,
            "source_trust_guard",
            "UNVERIFIED_COMPLETION",
            "Worker agent cannot mark its own Task COMPLETED without independent verification.",
            actor_id=context.actor_id,
        )
    _pass(decisions, "source_trust_guard")

    err = _check_task_target_evidence(decisions, task, typed_target, context)
    if err is not None:
        return decisions, err

    return decisions, None


def _check_task_identity_and_links(
    decisions: list[GuardDecision],
    task: Task,
    target_status: TaskStatus,
    context: TransitionContext,
) -> TransitionError | None:
    """Verify Run, ChecklistItem, and Artifact ownership match the target Task."""
    if context.run is not None:
        if context.run.task_id != task.task_id:
            return _fail(
                decisions,
                "identity_guard",
                "WRONG_TASK_RUN_RELATIONSHIP",
                f"Run '{context.run.run_id}' belongs to task '{context.run.task_id}', not '{task.task_id}'.",
            )[1]
        if (
            task.active_run_id is not None
            and context.run.run_id != task.active_run_id
            and (
                target_status != "RUNNING"
                or task.status in {"WAITING_INPUT", "WAITING_APPROVAL"}
            )
        ):
            return _fail(
                decisions,
                "identity_guard",
                "WRONG_TASK_RUN_RELATIONSHIP",
                f"Context Run '{context.run.run_id}' does not match active_run_id '{task.active_run_id}'.",
            )[1]
    if context.execution_evidence is not None and context.execution_evidence.run_id is not None:
        if (
            context.run is not None
            and context.execution_evidence.run_id != context.run.run_id
        ):
            return _fail(
                decisions,
                "identity_guard",
                "WRONG_TASK_RUN_RELATIONSHIP",
                "execution_evidence.run_id does not match context.run.run_id.",
            )[1]
    if context.checklist_items is not None:
        for item in context.checklist_items:
            if item.task_id != task.task_id:
                return _fail(
                    decisions,
                    "identity_guard",
                    "WRONG_TASK_RUN_RELATIONSHIP",
                    f"ChecklistItem '{item.checklist_item_id}' belongs to '{item.task_id}', not '{task.task_id}'.",
                )[1]
    if context.artifacts is not None:
        for art in context.artifacts:
            if art.task_id != task.task_id:
                return _fail(
                    decisions,
                    "identity_guard",
                    "WRONG_TASK_RUN_RELATIONSHIP",
                    f"Artifact '{art.artifact_id}' belongs to '{art.task_id}', not '{task.task_id}'.",
                )[1]
    _pass(decisions, "identity_guard")
    return None


def _check_task_target_evidence(
    decisions: list[GuardDecision],
    task: Task,
    target_status: TaskStatus,
    context: TransitionContext,
) -> TransitionError | None:
    """Enforce target-specific assignment, execution, QA, and cancellation guards."""
    if target_status == "ASSIGNED":
        effective_agents = context.assigned_agent_ids or task.assigned_agent_ids
        if not effective_agents:
            return _fail(
                decisions,
                "assignment_guard",
                "MISSING_ASSIGNMENT",
                "Task -> ASSIGNED requires at least one valid assigned agent ID.",
            )[1]
        _pass(decisions, "assignment_guard")
        return None

    if target_status == "RUNNING":
        if context.run is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_RUN",
                "Task -> RUNNING requires a linked Run in TransitionContext.",
            )[1]
        if context.run.status != "RUNNING" or context.run.started_at is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Task -> RUNNING requires an active Run with status='RUNNING' and started_at set.",
                run_status=context.run.status,
            )[1]
        if task.status == "BLOCKED" and context.execution_evidence is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Resuming Task from BLOCKED to RUNNING requires execution_evidence.",
            )[1]
        _pass(decisions, "execution_guard")
        return None

    if target_status == "QUEUED" and task.status == "BLOCKED":
        if context.execution_evidence is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Returning Task from BLOCKED to QUEUED requires execution_evidence.",
            )[1]
        _pass(decisions, "execution_guard")
        return None

    if target_status == "FAILED":
        if context.execution_evidence is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Task -> FAILED requires structured execution_evidence explaining the failure.",
            )[1]
        _pass(decisions, "execution_guard")
        return None

    if target_status == "CANCELLED":
        if (
            context.execution_evidence is None
            or not context.execution_evidence.confirmed_cancelled
            or context.execution_evidence.cancellation_source is None
        ):
            return _fail(
                decisions,
                "cancellation_guard",
                "CANCELLATION_NOT_CONFIRMED",
                "Task -> CANCELLED requires confirmed_cancelled=True and an explicit cancellation_source.",
            )[1]
        if context.run is not None and context.run.status not in TERMINAL_RUN_STATUSES:
            return _fail(
                decisions,
                "cancellation_guard",
                "CANCELLATION_NOT_CONFIRMED",
                f"Cannot cancel Task while linked Run '{context.run.run_id}' remains non-terminal ({context.run.status}).",
            )[1]
        _pass(decisions, "cancellation_guard")
        return None

    if target_status == "COMPLETED":
        return _check_task_qa_completion_guard(decisions, task, context)

    _pass(decisions, "execution_guard")
    return None


def _check_task_qa_completion_guard(
    decisions: list[GuardDecision],
    task: Task,
    context: TransitionContext,
) -> TransitionError | None:
    """Enforce independent QA evidence, Run state, Checklist completion, and Artifact QA."""
    qa = context.qa_evidence
    if qa is None:
        return _fail(
            decisions,
            "qa_guard",
            "MISSING_QA_EVIDENCE",
            "Task -> COMPLETED requires QaVerificationEvidence from an independent verifier.",
        )[1]
    if qa.verification_status != "VERIFIED_PASS":
        return _fail(
            decisions,
            "qa_guard",
            "UNVERIFIED_COMPLETION",
            f"QA verification status is '{qa.verification_status}', not 'VERIFIED_PASS'.",
        )[1]
    if context.run is None:
        if task.active_run_id is not None:
            return _fail(
                decisions,
                "qa_guard",
                "MISSING_RUN",
                "Task with active_run_id must provide the completed Run in TransitionContext before COMPLETED.",
            )[1]
    elif context.run.status != "SUCCEEDED":
        return _fail(
            decisions,
            "qa_guard",
            "MISSING_EXECUTION_EVIDENCE",
            f"Task -> COMPLETED cannot succeed while linked Run status is '{context.run.status}' (expected 'SUCCEEDED').",
        )[1]

    if context.checklist_items is not None:
        for item in context.checklist_items:
            if item.status in {"SKIPPED", "CANCELLED"}:
                continue
            if (
                item.status != "COMPLETED"
                or item.evidence_reference is None
                or item.completed_at is None
            ):
                return _fail(
                    decisions,
                    "qa_guard",
                    "UNVERIFIED_COMPLETION",
                    f"ChecklistItem '{item.checklist_item_id}' is not verified COMPLETED ({item.status}).",
                )[1]

    if context.artifacts is not None:
        for art in context.artifacts:
            if art.owner_agent_id == qa.verifier_actor_id:
                return _fail(
                    decisions,
                    "qa_guard",
                    "UNVERIFIED_COMPLETION",
                    f"Artifact producer '{art.owner_agent_id}' cannot verify its own artifact '{art.artifact_id}'.",
                )[1]
            if art.verification_status != "VERIFIED_PASS" or art.evidence_reference is None:
                return _fail(
                    decisions,
                    "qa_guard",
                    "UNVERIFIED_COMPLETION",
                    f"Artifact '{art.artifact_id}' has verification_status='{art.verification_status}', expected 'VERIFIED_PASS'.",
                )[1]

    _pass(decisions, "qa_guard")
    return None


def evaluate_run_transition_guards(
    run: Run,
    target_status: str,
    context: TransitionContext,
) -> tuple[list[GuardDecision], TransitionError | None]:
    """Evaluate all fail-closed guards for a candidate Run status transition."""
    decisions: list[GuardDecision] = []

    err = _check_timestamp_common(
        decisions,
        context.timestamp,
        earlier_bounds=[("run.started_at", run.started_at), ("run.finished_at", run.finished_at)],
    )
    if err is not None:
        return decisions, err

    err = _check_payload_and_reason(decisions, context)
    if err is not None:
        return decisions, err

    if (
        context.expected_current_status is not None
        and context.expected_current_status != run.status
    ):
        return _fail(
            decisions,
            "stale_status_guard",
            "STALE_EXPECTED_STATUS",
            f"Expected current Run status '{context.expected_current_status}', found '{run.status}'.",
            expected=context.expected_current_status,
            actual=run.status,
        )
    _pass(decisions, "stale_status_guard")

    if target_status not in VALID_RUN_STATUSES:
        return _fail(
            decisions,
            "unknown_status_guard",
            "UNKNOWN_STATUS",
            f"Unknown Run status requested: {target_status!r}.",
            requested_status=target_status,
        )
    _pass(decisions, "unknown_status_guard")

    typed_target: RunStatus = target_status  # type: ignore[assignment]

    if typed_target == run.status:
        return _fail(
            decisions,
            "duplicate_transition_guard",
            "DUPLICATE_TRANSITION",
            f"Run '{run.run_id}' is already in status '{run.status}'.",
        )
    _pass(decisions, "duplicate_transition_guard")

    if run.status in TERMINAL_RUN_STATUSES:
        return _fail(
            decisions,
            "terminal_state_guard",
            "TERMINAL_STATE_MUTATION",
            f"Terminal Run status '{run.status}' cannot transition to '{typed_target}'.",
            current_status=run.status,
            requested_status=typed_target,
        )
    _pass(decisions, "terminal_state_guard")

    if typed_target not in RUN_ALLOWED_TRANSITIONS[run.status]:
        return _fail(
            decisions,
            "transition_matrix_guard",
            "INVALID_TRANSITION",
            f"Run transition {run.status} -> {typed_target} is forbidden.",
            current_status=run.status,
            requested_status=typed_target,
        )
    _pass(decisions, "transition_matrix_guard")

    if (
        context.execution_evidence is not None
        and context.execution_evidence.run_id is not None
        and context.execution_evidence.run_id != run.run_id
    ):
        return _fail(
            decisions,
            "identity_guard",
            "WRONG_TASK_RUN_RELATIONSHIP",
            f"execution_evidence.run_id '{context.execution_evidence.run_id}' does not match '{run.run_id}'.",
        )
    _pass(decisions, "identity_guard")

    if context.actor_kind == "llm_self_report":
        return _fail(
            decisions,
            "source_trust_guard",
            "UNTRUSTED_TRANSITION_SOURCE",
            "Unverified LLM self-report cannot mutate Run lifecycle state.",
        )
    _pass(decisions, "source_trust_guard")

    if typed_target == "RUNNING" and context.execution_evidence is None:
        return _fail(
            decisions,
            "execution_guard",
            "MISSING_EXECUTION_EVIDENCE",
            "Run -> RUNNING requires execution_evidence.",
        )

    if typed_target == "SUCCEEDED":
        if context.execution_evidence is None or run.started_at is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Run -> SUCCEEDED requires started_at and execution_evidence.",
            )

    if typed_target == "FAILED":
        if (
            context.execution_evidence is None
            or context.execution_evidence.error_reference is None
        ):
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Run -> FAILED requires execution_evidence carrying a structured error_reference.",
            )

    if typed_target == "TIMED_OUT":
        if context.execution_evidence is None or run.started_at is None:
            return _fail(
                decisions,
                "execution_guard",
                "MISSING_EXECUTION_EVIDENCE",
                "Run -> TIMED_OUT requires started_at and execution_evidence.",
            )

    if typed_target == "CANCELLED":
        if (
            context.execution_evidence is None
            or not context.execution_evidence.confirmed_cancelled
            or context.execution_evidence.cancellation_source is None
        ):
            return _fail(
                decisions,
                "cancellation_guard",
                "CANCELLATION_NOT_CONFIRMED",
                "Run -> CANCELLED requires confirmed_cancelled=True and a cancellation_source.",
            )
        _pass(decisions, "cancellation_guard")
        return decisions, None

    _pass(decisions, "execution_guard")
    return decisions, None
