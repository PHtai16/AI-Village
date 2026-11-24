"""Pure deterministic Event Replay and structural validation over append-only journal history."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.contracts.event import Event, EventSource, EventType
from core.contracts.identifiers import (
    EventId,
    RunId,
    TaskId,
    validate_aware_datetime,
    validate_safe_payload,
)
from core.contracts.run import Run, RunStatus
from core.contracts.task import Task, TaskStatus
from core.events.journal import JournalEntry
from core.tasks.transitions import (
    RUN_ALLOWED_TRANSITIONS,
    TASK_ALLOWED_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    VALID_RUN_STATUSES,
    VALID_TASK_STATUSES,
)
from engines.antigravity.diagnostics import redact_secrets

ReplayErrorCode = Literal[
    "DUPLICATE_SEQUENCE",
    "SEQUENCE_GAP",
    "NON_MONOTONIC_SEQUENCE",
    "DUPLICATE_EVENT_ID",
    "INVALID_TIMESTAMP",
    "TIMESTAMP_ORDER_ANOMALY",
    "INVALID_EVENT_SCHEMA",
    "TASK_ID_MISMATCH",
    "RUN_ID_MISMATCH",
    "MISSING_BASELINE",
    "INCOMPLETE_HISTORY",
    "PREVIOUS_STATUS_MISMATCH",
    "UNKNOWN_STATUS",
    "INVALID_TRANSITION",
    "TERMINAL_STATE_MUTATION",
    "UNVERIFIED_QA_PASS",
    "UNTRUSTED_SOURCE_OR_ACTOR",
    "UNSAFE_PAYLOAD",
]
ReplayStatus = Literal["VALID", "INCOMPLETE_HISTORY", "INVALID_HISTORY"]


class ReplayIssue(BaseModel):
    """Structured diagnostic explaining why history is incomplete or invalid."""

    model_config = ConfigDict(extra="forbid")

    error_code: ReplayErrorCode
    sequence_number: int | None = None
    event_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _scrub_message(cls, value: str) -> str:
        """Redact known secret patterns inside replay diagnostics."""
        return redact_secrets(value)


class StatusStepRecord(BaseModel):
    """Single verified status transition step observed during deterministic replay."""

    model_config = ConfigDict(extra="forbid")

    sequence_number: int = Field(ge=1)
    event_id: EventId
    timestamp: datetime
    event_type: EventType
    previous_status: str | None = None
    new_status: str
    actor_id: str | None = None
    source: EventSource
    evidence_id: str | None = None


class QaEventRecord(BaseModel):
    """Verification event tracked separately from worker execution or run completion."""

    model_config = ConfigDict(extra="forbid")

    sequence_number: int = Field(ge=1)
    event_id: EventId
    timestamp: datetime
    verification_status: str
    artifact_id: str | None = None
    evidence_id: str | None = None
    actor_id: str | None = None
    source: EventSource


class TaskReplayResult(BaseModel):
    """Deterministic Task replay report distinguishing timeline facts from full baseline recovery."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    status: ReplayStatus
    has_baseline: bool
    can_reconstruct_full_task: bool
    replayed_status: TaskStatus | None = None
    reconstructed_task: Task | None = None
    active_run_id: RunId | None = None
    status_history: list[StatusStepRecord] = Field(default_factory=list)
    run_statuses: dict[str, RunStatus] = Field(default_factory=dict)
    waiting_or_blocked_events: list[StatusStepRecord] = Field(default_factory=list)
    qa_records: list[QaEventRecord] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    observed_tool_names: list[str] = Field(default_factory=list)
    events_processed: int = Field(ge=0)
    issues: list[ReplayIssue] = Field(default_factory=list)


class RunReplayResult(BaseModel):
    """Deterministic Run replay report keeping Run lifecycle separate from Task completion."""

    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    task_id: TaskId | None = None
    status: ReplayStatus
    has_baseline: bool
    can_reconstruct_full_run: bool
    replayed_status: RunStatus | None = None
    reconstructed_run: Run | None = None
    status_history: list[StatusStepRecord] = Field(default_factory=list)
    qa_records: list[QaEventRecord] = Field(default_factory=list)
    observed_tool_names: list[str] = Field(default_factory=list)
    events_processed: int = Field(ge=0)
    issues: list[ReplayIssue] = Field(default_factory=list)


def _add_issue(
    issues: list[ReplayIssue],
    error_code: ReplayErrorCode,
    message: str,
    *,
    entry: JournalEntry | None = None,
    **details: Any,
) -> None:
    """Append a structured ``ReplayIssue`` without raising early."""
    issues.append(
        ReplayIssue(
            error_code=error_code,
            sequence_number=entry.sequence_number if entry is not None else None,
            event_id=entry.event.event_id if entry is not None else None,
            message=message,
            details=details,
        )
    )


def _extract_observed_tool(event: Event) -> str | None:
    """Extract tool name strictly from structured observation events, never AI prose."""
    if event.event_type not in {"tool.call_observed", "tool.call_completed", "engine.event_forwarded"}:
        return None
    if event.source not in {"provider", "tool_runtime", "agent_runtime", "engine_adapter"}:
        return None
    tool_name = event.redacted_payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        return tool_name.strip()
    return None


def validate_journal_entries(
    entries: Sequence[JournalEntry],
    *,
    require_contiguous_sequences: bool = True,
    expected_task_id: str | None = None,
    expected_run_id: str | None = None,
) -> list[ReplayIssue]:
    """Validate sequence integrity, event schemas, payload safety, QA trust, and ownership."""
    issues: list[ReplayIssue] = []
    seen_seqs: set[int] = set()
    seen_event_ids: set[str] = set()
    prev_seq: int | None = None
    prev_entity_ts: dict[str, datetime] = {}

    for entry in entries:
        seq = entry.sequence_number
        ev = entry.event

        if seq in seen_seqs:
            _add_issue(
                issues,
                "DUPLICATE_SEQUENCE",
                f"Duplicate sequence_number {seq} encountered during replay.",
                entry=entry,
            )
        seen_seqs.add(seq)

        if prev_seq is not None:
            if seq < prev_seq:
                _add_issue(
                    issues,
                    "NON_MONOTONIC_SEQUENCE",
                    f"Sequence decreased from {prev_seq} to {seq}.",
                    entry=entry,
                )
            elif require_contiguous_sequences and seq != prev_seq + 1:
                _add_issue(
                    issues,
                    "SEQUENCE_GAP",
                    f"Missing sequence between {prev_seq} and {seq}.",
                    entry=entry,
                    expected_sequence=prev_seq + 1,
                )
        elif require_contiguous_sequences and seq != 1:
            _add_issue(
                issues,
                "SEQUENCE_GAP",
                f"Contiguous replay expected sequence starting at 1, got {seq}.",
                entry=entry,
                expected_sequence=1,
            )
        prev_seq = seq

        if ev.event_id in seen_event_ids:
            _add_issue(
                issues,
                "DUPLICATE_EVENT_ID",
                f"Duplicate event_id '{ev.event_id}' encountered during replay.",
                entry=entry,
            )
        seen_event_ids.add(ev.event_id)

        try:
            validate_aware_datetime(ev.timestamp, "event.timestamp")
        except ValueError as exc:
            _add_issue(issues, "INVALID_TIMESTAMP", str(exc), entry=entry)
            continue

        try:
            validate_safe_payload(ev.redacted_payload, "event.redacted_payload")
        except ValueError as exc:
            _add_issue(issues, "UNSAFE_PAYLOAD", str(exc), entry=entry)

        try:
            Event.model_validate_json(ev.model_dump_json())
        except (ValidationError, ValueError) as exc:
            _add_issue(issues, "INVALID_EVENT_SCHEMA", str(exc), entry=entry)

        if expected_task_id is not None and ev.task_id != expected_task_id:
            _add_issue(
                issues,
                "TASK_ID_MISMATCH",
                f"Event task_id '{ev.task_id}' does not match expected '{expected_task_id}'.",
                entry=entry,
            )
        if expected_run_id is not None and ev.run_id != expected_run_id:
            _add_issue(
                issues,
                "RUN_ID_MISMATCH",
                f"Event run_id '{ev.run_id}' does not match expected '{expected_run_id}'.",
                entry=entry,
            )

        # Verify causal non-decreasing timestamp progression within identical Task/Run lifecycles
        for key_prefix, entity_ref in (("task", ev.task_id), ("run", ev.run_id)):
            if entity_ref is None:
                continue
            track_key = f"{key_prefix}:{entity_ref}"
            last_ts = prev_entity_ts.get(track_key)
            if last_ts is not None and ev.timestamp < last_ts:
                _add_issue(
                    issues,
                    "TIMESTAMP_ORDER_ANOMALY",
                    f"Event timestamp ({ev.timestamp.isoformat()}) precedes prior timestamp ({last_ts.isoformat()}) for {track_key}.",
                    entry=entry,
                )
            prev_entity_ts[track_key] = ev.timestamp

        # Independent QA authenticity check on qa.verification_recorded
        if ev.event_type == "qa.verification_recorded":
            ver_status = str(ev.redacted_payload.get("verification_status", "UNVERIFIED"))
            if ev.source != "qa_verifier":
                _add_issue(
                    issues,
                    "UNTRUSTED_SOURCE_OR_ACTOR",
                    f"qa.verification_recorded must originate from source='qa_verifier', got '{ev.source}'.",
                    entry=entry,
                )
            if ver_status == "VERIFIED_PASS" and ev.evidence_id is None:
                _add_issue(
                    issues,
                    "UNVERIFIED_QA_PASS",
                    "qa.verification_recorded claiming VERIFIED_PASS lacks required evidence_id.",
                    entry=entry,
                )

    return issues


def _target_run_status_from_event(event: Event) -> str | None:
    """Resolve target RunStatus implied by specialized or status_changed Run events."""
    mapping = {
        "run.started": "RUNNING",
        "run.completed": "SUCCEEDED",
        "run.failed": "FAILED",
        "run.cancelled": "CANCELLED",
        "run.timed_out": "TIMED_OUT",
    }
    if event.event_type in mapping:
        return mapping[event.event_type]
    if event.event_type == "run.status_changed":
        val = event.redacted_payload.get("new_status")
        return str(val) if val is not None else None
    return None


def _advance_run_state_for_event(
    issues: list[ReplayIssue],
    run_states: dict[str, str],
    entry: JournalEntry,
) -> str | None:
    """Validate one Run event against ``RUN_ALLOWED_TRANSITIONS`` and update state."""
    ev = entry.event
    run_id = ev.run_id
    target = _target_run_status_from_event(ev)
    if target is None or run_id is None:
        return None

    if target not in VALID_RUN_STATUSES:
        _add_issue(
            issues,
            "UNKNOWN_STATUS",
            f"Run event references unknown status '{target}'.",
            entry=entry,
        )
        return None

    current = run_states.get(run_id, "PENDING" if target == "RUNNING" else None)
    prev_payload = ev.redacted_payload.get("previous_status")
    if current is None and prev_payload is None:
        _add_issue(
            issues,
            "INCOMPLETE_HISTORY",
            f"Missing initial status or baseline before Run event '{ev.event_type}' ({target}).",
            entry=entry,
        )
        return None
    if current is None and isinstance(prev_payload, str):
        current = prev_payload
        if current not in VALID_RUN_STATUSES:
            _add_issue(
                issues,
                "UNKNOWN_STATUS",
                f"Run event previous_status '{current}' is unknown.",
                entry=entry,
            )
            return None

    if current is None:
        return None
    if prev_payload is not None and str(prev_payload) != current:
        _add_issue(
            issues,
            "PREVIOUS_STATUS_MISMATCH",
            f"Run '{run_id}' expected previous_status '{current}', got '{prev_payload}'.",
            entry=entry,
        )
        return None

    if current in TERMINAL_RUN_STATUSES:
        _add_issue(
            issues,
            "TERMINAL_STATE_MUTATION",
            f"Terminal Run '{run_id}' ({current}) cannot transition to '{target}'.",
            entry=entry,
        )
        return None

    typed_cur: RunStatus = current  # type: ignore[assignment]
    if target not in RUN_ALLOWED_TRANSITIONS[typed_cur]:
        _add_issue(
            issues,
            "INVALID_TRANSITION",
            f"Forbidden Run transition {current} -> {target} in history.",
            entry=entry,
        )
        return None

    run_states[run_id] = target
    return target


def replay_task_history(
    entries: Sequence[JournalEntry],
    *,
    task_id: str,
    baseline_task: Task | None = None,
    require_contiguous_sequences: bool = False,
) -> TaskReplayResult:
    """Deterministically replay Task status, Run links, QA records, and observed tools."""
    issues = validate_journal_entries(
        entries,
        require_contiguous_sequences=require_contiguous_sequences,
        expected_task_id=task_id,
    )
    has_baseline = baseline_task is not None and baseline_task.task_id == task_id
    if baseline_task is not None and baseline_task.task_id != task_id:
        _add_issue(
            issues,
            "TASK_ID_MISMATCH",
            f"Baseline Task '{baseline_task.task_id}' does not match requested '{task_id}'.",
        )

    current_task_status: str | None = baseline_task.status if has_baseline and baseline_task else None
    active_run_id: str | None = baseline_task.active_run_id if has_baseline and baseline_task else None
    artifact_ids: list[str] = list(baseline_task.artifact_ids) if has_baseline and baseline_task else []
    assigned_agents: list[str] = list(baseline_task.assigned_agent_ids) if has_baseline and baseline_task else []
    run_states: dict[str, str] = {}
    status_history: list[StatusStepRecord] = []
    waiting_or_blocked: list[StatusStepRecord] = []
    qa_records: list[QaEventRecord] = []
    observed_tools: list[str] = []
    latest_task_ts: datetime | None = baseline_task.created_at if has_baseline and baseline_task else None

    for entry in entries:
        ev = entry.event
        if latest_task_ts is not None and ev.timestamp.tzinfo is not None and ev.timestamp < latest_task_ts:
            _add_issue(
                issues,
                "TIMESTAMP_ORDER_ANOMALY",
                f"Event timestamp ({ev.timestamp.isoformat()}) precedes Task baseline created_at.",
                entry=entry,
            )

        if ev.artifact_id and ev.artifact_id not in artifact_ids:
            artifact_ids.append(ev.artifact_id)

        tool_name = _extract_observed_tool(ev)
        if tool_name and tool_name not in observed_tools:
            observed_tools.append(tool_name)

        if ev.event_type == "qa.verification_recorded":
            ver_status = str(ev.redacted_payload.get("verification_status", "UNVERIFIED"))
            qa_records.append(
                QaEventRecord(
                    sequence_number=entry.sequence_number,
                    event_id=ev.event_id,
                    timestamp=ev.timestamp,
                    verification_status=ver_status,
                    artifact_id=ev.artifact_id,
                    evidence_id=ev.evidence_id,
                    actor_id=ev.actor_id,
                    source=ev.source,
                )
            )
            continue

        if ev.event_type in {
            "run.started",
            "run.status_changed",
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.timed_out",
        }:
            new_r_status = _advance_run_state_for_event(issues, run_states, entry)
            if new_r_status == "RUNNING" and ev.run_id is not None:
                active_run_id = ev.run_id
            elif new_r_status in TERMINAL_RUN_STATUSES and ev.run_id == active_run_id:
                active_run_id = None
            continue

        if ev.event_type == "task.created":
            init_status = str(ev.redacted_payload.get("initial_status", "QUEUED"))
            if current_task_status is None:
                current_task_status = init_status
            latest_task_ts = ev.timestamp
            continue

        if ev.event_type == "task.status_changed":
            prev_s = ev.redacted_payload.get("previous_status")
            new_s = ev.redacted_payload.get("new_status")
            actor_kind = str(ev.redacted_payload.get("actor_kind", ""))
            if not isinstance(new_s, str) or new_s not in VALID_TASK_STATUSES:
                _add_issue(
                    issues,
                    "UNKNOWN_STATUS",
                    f"task.status_changed contains unknown new_status: {new_s!r}.",
                    entry=entry,
                )
                continue

            if current_task_status is None:
                if not isinstance(prev_s, str) or prev_s not in VALID_TASK_STATUSES:
                    _add_issue(
                        issues,
                        "INCOMPLETE_HISTORY",
                        "Missing baseline and previous_status on task.status_changed.",
                        entry=entry,
                    )
                    continue
                current_task_status = prev_s
            elif prev_s is not None and str(prev_s) != current_task_status:
                _add_issue(
                    issues,
                    "PREVIOUS_STATUS_MISMATCH",
                    f"Task expected previous_status '{current_task_status}', found '{prev_s}'.",
                    entry=entry,
                )
                continue

            if current_task_status in TERMINAL_TASK_STATUSES:
                _add_issue(
                    issues,
                    "TERMINAL_STATE_MUTATION",
                    f"Terminal Task ({current_task_status}) mutated to '{new_s}'.",
                    entry=entry,
                )
                continue

            typed_cur: TaskStatus = current_task_status  # type: ignore[assignment]
            if new_s not in TASK_ALLOWED_TRANSITIONS[typed_cur]:
                _add_issue(
                    issues,
                    "INVALID_TRANSITION",
                    f"Forbidden Task transition {current_task_status} -> {new_s}.",
                    entry=entry,
                )
                continue

            if new_s == "COMPLETED":
                if ev.source in {"provider", "agent_runtime"} or actor_kind in {"llm_self_report", "worker_agent"}:
                    _add_issue(
                        issues,
                        "UNTRUSTED_SOURCE_OR_ACTOR",
                        "Task COMPLETED event originated from untrusted provider or worker self-report.",
                        entry=entry,
                    )
                    continue
                if ev.evidence_id is None:
                    _add_issue(
                        issues,
                        "UNVERIFIED_QA_PASS",
                        "Task COMPLETED event is missing required independent QA evidence_id.",
                        entry=entry,
                    )
                    continue
                if ev.run_id is not None and ev.run_id in run_states and run_states[ev.run_id] != "SUCCEEDED":
                    _add_issue(
                        issues,
                        "INVALID_TRANSITION",
                        f"Task COMPLETED conflicts with linked Run '{ev.run_id}' status '{run_states[ev.run_id]}'.",
                        entry=entry,
                    )
                    continue

            current_task_status = new_s
            latest_task_ts = ev.timestamp
            if new_s == "RUNNING" and ev.run_id is not None:
                active_run_id = ev.run_id
            elif new_s in TERMINAL_TASK_STATUSES:
                active_run_id = None

            step = StatusStepRecord(
                sequence_number=entry.sequence_number,
                event_id=ev.event_id,
                timestamp=ev.timestamp,
                event_type=ev.event_type,
                previous_status=str(prev_s) if prev_s is not None else None,
                new_status=new_s,
                actor_id=ev.actor_id,
                source=ev.source,
                evidence_id=ev.evidence_id,
            )
            status_history.append(step)
            if new_s in {"WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED"}:
                waiting_or_blocked.append(step)

    if not entries and not has_baseline:
        _add_issue(
            issues,
            "INCOMPLETE_HISTORY",
            f"No journal events or baseline Task recorded for '{task_id}'.",
        )
    elif not has_baseline:
        _add_issue(
            issues,
            "MISSING_BASELINE",
            f"Cannot reconstruct full Task fields for '{task_id}' without baseline_task.",
        )

    fatal_codes = set(issues_codes := {item.error_code for item in issues}) - {"MISSING_BASELINE"}
    if fatal_codes:
        replay_status: ReplayStatus = "INVALID_HISTORY"
    elif not has_baseline:
        replay_status = "INCOMPLETE_HISTORY"
    else:
        replay_status = "VALID"

    reconstructed: Task | None = None
    can_reconstruct = False
    if replay_status == "VALID" and baseline_task is not None and current_task_status in VALID_TASK_STATUSES:
        updated_data = baseline_task.model_dump()
        updated_data.update(
            {
                "status": current_task_status,
                "active_run_id": None if current_task_status in TERMINAL_TASK_STATUSES else active_run_id,
                "artifact_ids": artifact_ids,
                "assigned_agent_ids": assigned_agents,
                "updated_at": (
                    latest_task_ts
                    if (baseline_task.created_at is not None and latest_task_ts is not None and latest_task_ts >= baseline_task.created_at)
                    else baseline_task.updated_at
                ),
            }
        )
        try:
            reconstructed = Task.model_validate(updated_data)
            can_reconstruct = True
        except ValidationError as exc:
            _add_issue(issues, "INVALID_EVENT_SCHEMA", f"Failed Task reconstruction: {exc}")
            replay_status = "INVALID_HISTORY"

    _ = issues_codes
    typed_run_statuses: dict[str, RunStatus] = {
        rid: rstat for rid, rstat in run_states.items() if rstat in VALID_RUN_STATUSES
    }  # type: ignore[misc]
    replayed_task_status: TaskStatus | None = (
        current_task_status if current_task_status in VALID_TASK_STATUSES else None
    )  # type: ignore[assignment]

    return TaskReplayResult(
        task_id=task_id,
        status=replay_status,
        has_baseline=has_baseline,
        can_reconstruct_full_task=can_reconstruct,
        replayed_status=replayed_task_status,
        reconstructed_task=reconstructed,
        active_run_id=reconstructed.active_run_id if reconstructed else active_run_id,
        status_history=status_history,
        run_statuses=typed_run_statuses,
        waiting_or_blocked_events=waiting_or_blocked,
        qa_records=qa_records,
        artifact_ids=artifact_ids,
        observed_tool_names=observed_tools,
        events_processed=len(entries),
        issues=issues,
    )


def replay_run_history(
    entries: Sequence[JournalEntry],
    *,
    run_id: str,
    baseline_run: Run | None = None,
    require_contiguous_sequences: bool = False,
) -> RunReplayResult:
    """Deterministically replay Run status history and verify state-machine consistency."""
    issues = validate_journal_entries(
        entries,
        require_contiguous_sequences=require_contiguous_sequences,
        expected_run_id=run_id,
    )
    has_baseline = baseline_run is not None and baseline_run.run_id == run_id
    if baseline_run is not None and baseline_run.run_id != run_id:
        _add_issue(
            issues,
            "RUN_ID_MISMATCH",
            f"Baseline Run '{baseline_run.run_id}' does not match requested '{run_id}'.",
        )

    run_states: dict[str, str] = {}
    if has_baseline and baseline_run is not None:
        run_states[run_id] = baseline_run.status

    task_id: str | None = baseline_run.task_id if has_baseline and baseline_run else None
    started_at: datetime | None = baseline_run.started_at if has_baseline and baseline_run else None
    finished_at: datetime | None = baseline_run.finished_at if has_baseline and baseline_run else None
    status_history: list[StatusStepRecord] = []
    qa_records: list[QaEventRecord] = []
    observed_tools: list[str] = []

    for entry in entries:
        ev = entry.event
        if ev.task_id is not None:
            if task_id is None:
                task_id = ev.task_id
            elif ev.task_id != task_id:
                _add_issue(
                    issues,
                    "TASK_ID_MISMATCH",
                    f"Run event task_id '{ev.task_id}' disagrees with '{task_id}'.",
                    entry=entry,
                )

        tool_name = _extract_observed_tool(ev)
        if tool_name and tool_name not in observed_tools:
            observed_tools.append(tool_name)

        if ev.event_type == "qa.verification_recorded":
            qa_records.append(
                QaEventRecord(
                    sequence_number=entry.sequence_number,
                    event_id=ev.event_id,
                    timestamp=ev.timestamp,
                    verification_status=str(ev.redacted_payload.get("verification_status", "UNVERIFIED")),
                    artifact_id=ev.artifact_id,
                    evidence_id=ev.evidence_id,
                    actor_id=ev.actor_id,
                    source=ev.source,
                )
            )
            continue

        if ev.event_type in {
            "run.started",
            "run.status_changed",
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.timed_out",
        }:
            prev_before = run_states.get(run_id)
            new_r_status = _advance_run_state_for_event(issues, run_states, entry)
            if new_r_status is not None:
                if ev.event_type == "run.started" and started_at is None:
                    started_at = ev.timestamp
                elif new_r_status in TERMINAL_RUN_STATUSES:
                    if started_at is None:
                        started_at = ev.timestamp
                    finished_at = ev.timestamp
                status_history.append(
                    StatusStepRecord(
                        sequence_number=entry.sequence_number,
                        event_id=ev.event_id,
                        timestamp=ev.timestamp,
                        event_type=ev.event_type,
                        previous_status=str(
                            ev.redacted_payload.get("previous_status", prev_before)
                        )
                        if ev.redacted_payload.get("previous_status") is not None or prev_before is not None
                        else None,
                        new_status=new_r_status,
                        actor_id=ev.actor_id,
                        source=ev.source,
                        evidence_id=ev.evidence_id,
                    )
                )

    if not entries and not has_baseline:
        _add_issue(
            issues,
            "INCOMPLETE_HISTORY",
            f"No journal events or baseline Run recorded for '{run_id}'.",
        )
    elif not has_baseline:
        _add_issue(
            issues,
            "MISSING_BASELINE",
            f"Cannot reconstruct full Run record for '{run_id}' without baseline_run.",
        )

    fatal_codes = {item.error_code for item in issues} - {"MISSING_BASELINE"}
    if fatal_codes:
        replay_status: ReplayStatus = "INVALID_HISTORY"
    elif not has_baseline:
        replay_status = "INCOMPLETE_HISTORY"
    else:
        replay_status = "VALID"

    current_status = run_states.get(run_id)
    reconstructed: Run | None = None
    can_reconstruct = False
    if replay_status == "VALID" and baseline_run is not None and current_status in VALID_RUN_STATUSES:
        updated = baseline_run.model_dump()
        updated.update(
            {
                "status": current_status,
                "started_at": started_at,
                "finished_at": finished_at if current_status in TERMINAL_RUN_STATUSES else None,
            }
        )
        try:
            reconstructed = Run.model_validate(updated)
            can_reconstruct = True
        except ValidationError as exc:
            _add_issue(issues, "INVALID_EVENT_SCHEMA", f"Failed Run reconstruction: {exc}")
            replay_status = "INVALID_HISTORY"

    replayed_run_status: RunStatus | None = (
        current_status if current_status in VALID_RUN_STATUSES else None
    )  # type: ignore[assignment]

    return RunReplayResult(
        run_id=run_id,
        task_id=task_id,
        status=replay_status,
        has_baseline=has_baseline,
        can_reconstruct_full_run=can_reconstruct,
        replayed_status=replayed_run_status,
        reconstructed_run=reconstructed,
        status_history=status_history,
        qa_records=qa_records,
        observed_tool_names=observed_tools,
        events_processed=len(entries),
        issues=issues,
    )
