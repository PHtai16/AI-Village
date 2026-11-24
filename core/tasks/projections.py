"""Read-only Kanban, Checklist, and Timing projections driven solely by Core contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.contracts.event import Event
from core.contracts.identifiers import ChecklistItemId, RunId, TaskId, validate_aware_datetime
from core.contracts.references import ChecklistItem
from core.contracts.run import Run
from core.contracts.task import Task, TaskPriority, TaskStatus
from core.tasks.transitions import TERMINAL_TASK_STATUSES

KanbanColumnId = Literal[
    "queued",
    "in_progress",
    "waiting_approval",
    "blocked",
    "failed",
    "completed",
]

KANBAN_COLUMN_ORDER: tuple[KanbanColumnId, ...] = (
    "queued",
    "in_progress",
    "waiting_approval",
    "blocked",
    "failed",
    "completed",
)

KANBAN_COLUMN_LABELS: dict[KanbanColumnId, str] = {
    "queued": "Chờ",
    "in_progress": "Đang thực hiện",
    "waiting_approval": "Chờ phê duyệt",
    "blocked": "Bị chặn",
    "failed": "Thất bại",
    "completed": "Hoàn thành",
}

KANBAN_STATUS_MAP: dict[TaskStatus, KanbanColumnId] = {
    "QUEUED": "queued",
    "ASSIGNED": "queued",
    "RUNNING": "in_progress",
    "WAITING_INPUT": "in_progress",
    "WAITING_APPROVAL": "waiting_approval",
    "BLOCKED": "blocked",
    "FAILED": "failed",
    "CANCELLED": "failed",
    "COMPLETED": "completed",
}


class KanbanTaskCard(BaseModel):
    """Read-only Kanban card preserving exact TaskStatus without inventing Kanban state."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    project_id: str | None = None
    title: str
    task_status: TaskStatus
    column_id: KanbanColumnId
    priority: TaskPriority
    active_run_id: RunId | None = None


class KanbanColumnProjection(BaseModel):
    """Single Kanban column matching Mission Control UX Section 4.2."""

    model_config = ConfigDict(extra="forbid")

    column_id: KanbanColumnId
    label: str
    task_count: int = Field(ge=0)
    counts_by_status: dict[TaskStatus, int] = Field(default_factory=dict)
    cards: list[KanbanTaskCard] = Field(default_factory=list)


class KanbanBoardProjection(BaseModel):
    """Read-only Kanban board grouped strictly from Task contracts."""

    model_config = ConfigDict(extra="forbid")

    total_tasks: int = Field(ge=0)
    counts_by_status: dict[TaskStatus, int] = Field(default_factory=dict)
    columns: list[KanbanColumnProjection]


class ChecklistProjection(BaseModel):
    """Evidence-verified Checklist progress summary without synthetic progress percentages."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    total_items: int = Field(ge=0)
    verified_completed_items: int = Field(ge=0)
    pending_items: int = Field(ge=0)
    in_progress_items: int = Field(ge=0)
    waiting_approval_items: int = Field(ge=0)
    blocked_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    skipped_items: int = Field(ge=0)
    cancelled_items: int = Field(ge=0)
    verified_item_ids: list[ChecklistItemId] = Field(default_factory=list)
    ratio_display: str
    all_required_items_verified: bool
    implies_task_completion: Literal[False] = False


class TaskTimingProjection(BaseModel):
    """Truthful Task timing projection derived strictly from recorded timestamps with zero ETA."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    task_status: TaskStatus
    created_at: datetime | None = None
    first_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    observed_at: datetime | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)
    total_completed_duration_ms: int | None = Field(default=None, ge=0)
    waiting_approval_ms: int | None = Field(default=None, ge=0)
    waiting_input_ms: int | None = Field(default=None, ge=0)
    blocked_ms: int | None = Field(default=None, ge=0)
    estimated_completion_at: Literal[None] = None
    eta_available: Literal[False] = False


def project_kanban_column(status: TaskStatus) -> KanbanColumnId:
    """Map canonical ``TaskStatus`` to its fixed Mission Control Kanban column ID."""
    return KANBAN_STATUS_MAP[status]


def project_kanban_board(tasks: Sequence[Task]) -> KanbanBoardProjection:
    """Project a sequence of ``Task`` models into read-only Mission Control Kanban columns."""
    cards_by_col: dict[KanbanColumnId, list[KanbanTaskCard]] = {
        col: [] for col in KANBAN_COLUMN_ORDER
    }
    col_status_counts: dict[KanbanColumnId, dict[TaskStatus, int]] = {
        col: {} for col in KANBAN_COLUMN_ORDER
    }
    global_status_counts: dict[TaskStatus, int] = {}

    for task in tasks:
        col_id = project_kanban_column(task.status)
        cards_by_col[col_id].append(
            KanbanTaskCard(
                task_id=task.task_id,
                project_id=task.project_id,
                title=task.title,
                task_status=task.status,
                column_id=col_id,
                priority=task.priority,
                active_run_id=task.active_run_id,
            )
        )
        col_status_counts[col_id][task.status] = col_status_counts[col_id].get(task.status, 0) + 1
        global_status_counts[task.status] = global_status_counts.get(task.status, 0) + 1

    columns = [
        KanbanColumnProjection(
            column_id=col_id,
            label=KANBAN_COLUMN_LABELS[col_id],
            task_count=len(cards_by_col[col_id]),
            counts_by_status=col_status_counts[col_id],
            cards=cards_by_col[col_id],
        )
        for col_id in KANBAN_COLUMN_ORDER
    ]
    return KanbanBoardProjection(
        total_tasks=len(tasks),
        counts_by_status=global_status_counts,
        columns=columns,
    )


def project_checklist(task_id: str, items: Sequence[ChecklistItem]) -> ChecklistProjection:
    """Compute evidence-verified Checklist counts without inventing percentage guesses."""
    verified_ids: list[str] = []
    counts: dict[str, int] = {
        "PENDING": 0,
        "IN_PROGRESS": 0,
        "WAITING_APPROVAL": 0,
        "BLOCKED": 0,
        "COMPLETED": 0,
        "FAILED": 0,
        "SKIPPED": 0,
        "CANCELLED": 0,
    }
    for item in items:
        if item.task_id != task_id:
            raise ValueError(
                f"ChecklistItem '{item.checklist_item_id}' belongs to '{item.task_id}', not '{task_id}'."
            )
        if item.status == "COMPLETED":
            if item.evidence_reference is not None and item.completed_at is not None:
                counts["COMPLETED"] += 1
                verified_ids.append(item.checklist_item_id)
        else:
            counts[item.status] += 1

    required_remaining = (
        counts["PENDING"]
        + counts["IN_PROGRESS"]
        + counts["WAITING_APPROVAL"]
        + counts["BLOCKED"]
        + counts["FAILED"]
    )
    total = len(items)
    verified_done = counts["COMPLETED"]
    return ChecklistProjection(
        task_id=task_id,
        total_items=total,
        verified_completed_items=verified_done,
        pending_items=counts["PENDING"],
        in_progress_items=counts["IN_PROGRESS"],
        waiting_approval_items=counts["WAITING_APPROVAL"],
        blocked_items=counts["BLOCKED"],
        failed_items=counts["FAILED"],
        skipped_items=counts["SKIPPED"],
        cancelled_items=counts["CANCELLED"],
        verified_item_ids=verified_ids,
        ratio_display=f"{verified_done}/{total}",
        all_required_items_verified=(total > 0 and required_remaining == 0 and verified_done > 0),
    )


def _compute_state_durations_ms(
    task_id: str,
    events: Sequence[Event],
    *,
    end_cap: datetime | None,
) -> tuple[int | None, int | None, int | None]:
    """Compute accumulated wait/block durations purely from recorded ``task.status_changed`` events."""
    status_events = sorted(
        (ev for ev in events if ev.task_id == task_id and ev.event_type == "task.status_changed"),
        key=lambda ev: ev.timestamp,
    )
    if not status_events:
        return None, None, None

    totals_ms: dict[str, int] = {
        "WAITING_APPROVAL": 0,
        "WAITING_INPUT": 0,
        "BLOCKED": 0,
    }
    seen_any_wait = {key: False for key in totals_ms}

    for idx, ev in enumerate(status_events):
        entered = str(ev.redacted_payload.get("new_status", ""))
        if entered not in totals_ms:
            continue
        next_ts: datetime | None
        if idx + 1 < len(status_events):
            next_ts = status_events[idx + 1].timestamp
        else:
            next_ts = end_cap
        if next_ts is None or next_ts < ev.timestamp:
            continue
        seen_any_wait[entered] = True
        totals_ms[entered] += max(0, int((next_ts - ev.timestamp).total_seconds() * 1000))

    return (
        totals_ms["WAITING_APPROVAL"] if seen_any_wait["WAITING_APPROVAL"] else None,
        totals_ms["WAITING_INPUT"] if seen_any_wait["WAITING_INPUT"] else None,
        totals_ms["BLOCKED"] if seen_any_wait["BLOCKED"] else None,
    )


def project_task_timing(
    task: Task,
    *,
    runs: Sequence[Run] = (),
    events: Sequence[Event] = (),
    observed_at: datetime | None = None,
) -> TaskTimingProjection:
    """Calculate elapsed, wait, and completed durations solely from real timestamps."""
    if observed_at is not None:
        validate_aware_datetime(observed_at, "observed_at")

    for run in runs:
        if run.task_id != task.task_id:
            raise ValueError(
                f"Run '{run.run_id}' belongs to '{run.task_id}', not '{task.task_id}'."
            )
    for ev in events:
        if ev.task_id is not None and ev.task_id != task.task_id:
            raise ValueError(
                f"Event '{ev.event_id}' belongs to '{ev.task_id}', not '{task.task_id}'."
            )

    run_starts = [run.started_at for run in runs if run.started_at is not None]
    event_starts = [
        ev.timestamp
        for ev in events
        if ev.task_id == task.task_id
        and (
            ev.event_type == "run.started"
            or (ev.event_type == "task.status_changed" and ev.redacted_payload.get("new_status") == "RUNNING")
        )
    ]
    all_starts = [*run_starts, *event_starts]
    first_started_at = min(all_starts) if all_starts else None

    is_terminal = task.status in TERMINAL_TASK_STATUSES
    last_finished_at: datetime | None = None
    if is_terminal:
        run_finishes = [run.finished_at for run in runs if run.finished_at is not None]
        terminal_event_times = [
            ev.timestamp
            for ev in events
            if ev.task_id == task.task_id
            and ev.event_type == "task.status_changed"
            and ev.redacted_payload.get("new_status") in TERMINAL_TASK_STATUSES
        ]
        finish_candidates = [*run_finishes, *terminal_event_times]
        if task.updated_at is not None:
            finish_candidates.append(task.updated_at)
        last_finished_at = max(finish_candidates) if finish_candidates else None

    elapsed_ms: int | None = None
    total_completed_duration_ms: int | None = None
    if first_started_at is not None:
        if is_terminal and last_finished_at is not None and last_finished_at >= first_started_at:
            total_completed_duration_ms = max(
                0, int((last_finished_at - first_started_at).total_seconds() * 1000)
            )
            elapsed_ms = total_completed_duration_ms
        elif observed_at is not None and observed_at >= first_started_at:
            elapsed_ms = max(0, int((observed_at - first_started_at).total_seconds() * 1000))

    wait_cap = last_finished_at if is_terminal else observed_at
    wait_approval_ms, wait_input_ms, blocked_ms = _compute_state_durations_ms(
        task.task_id,
        events,
        end_cap=wait_cap,
    )

    return TaskTimingProjection(
        task_id=task.task_id,
        task_status=task.status,
        created_at=task.created_at,
        first_started_at=first_started_at,
        last_finished_at=last_finished_at,
        observed_at=observed_at,
        elapsed_ms=elapsed_ms,
        total_completed_duration_ms=total_completed_duration_ms,
        waiting_approval_ms=wait_approval_ms,
        waiting_input_ms=wait_input_ms,
        blocked_ms=blocked_ms,
    )
